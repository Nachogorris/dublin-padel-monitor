"""Compliance gatekeeper: robots.txt enforcement, rate-limiting, and access-wall detection.

Ensures every automated HTTP request respects:
1. RFC 9309 robots.txt rules for both '*' and our User-Agent.
2. Polite per-host rate limiting (default >= 1.5s between requests).
3. Zero tolerance for bypassing authentication walls, Cloudflare Turnstile / CAPTCHA
   challenges, or undocumented disallowed internal APIs.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

DEFAULT_USER_AGENT = "DublinPadelMarketIntelligence/1.0 (+research; robots-compliant)"


class Rfc9309RobotsPolicy:
    """RFC 9309-compliant robots.txt parser supporting `*` wildcards and `$` end anchors.

    Note: Python's stdlib `urllib.robotparser.RobotFileParser` treats `*` as a literal
    character and therefore misses modern rules like `Disallow: /*?*date=` on Playtomic
    or `Disallow: /reservations/*` on Playbypoint.
    """

    def __init__(self, robots_txt: str) -> None:
        self._groups: list[tuple[list[str], list[tuple[bool, str, re.Pattern[str], int]]]] = []
        current_agents: list[str] = []
        current_rules: list[tuple[bool, str, re.Pattern[str], int]] = []

        for raw_line in robots_txt.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = [p.strip() for p in line.split(":", 1)]
            f_low = field.lower()
            if f_low == "user-agent":
                if current_rules:
                    self._groups.append((current_agents, current_rules))
                    current_agents, current_rules = [], []
                current_agents.append(value.lower())
            elif f_low in ("allow", "disallow") and value:
                if not current_agents:
                    current_agents = ["*"]
                regex = self._compile_pattern(value)
                current_rules.append((f_low == "allow", value, regex, len(value)))

        if current_agents or current_rules:
            self._groups.append((current_agents, current_rules))

    @staticmethod
    def _compile_pattern(pattern: str) -> re.Pattern[str]:
        anchored_end = pattern.endswith("$")
        core = pattern[:-1] if anchored_end else pattern
        parts = [re.escape(seg) for seg in core.split("*")]
        expr = "^" + ".*".join(parts) + ("$" if anchored_end else "")
        return re.compile(expr)

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parsed = urlparse(url)
        path_and_query = parsed.path or "/"
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"

        ua_low = user_agent.lower()
        applicable_rules: list[tuple[bool, str, re.Pattern[str], int]] = []
        for agents, rules in self._groups:
            if any(a == "*" or a in ua_low for a in agents):
                applicable_rules.extend(rules)

        matched = [r for r in applicable_rules if r[2].search(path_and_query)]
        if not matched:
            return True
        # RFC 9309 Sec 2.2.2: longest matching pattern wins; Allow wins ties
        best = max(matched, key=lambda r: (r[3], 1 if r[0] else 0))
        return best[0]


class ComplianceBlockedError(RuntimeError):
    """Raised when robots.txt or explicit platform policy forbids automated access."""


class AntiBotChallengeError(RuntimeError):
    """Raised when a target returns a Cloudflare / CAPTCHA / anti-bot challenge."""


class AuthenticationWallError(RuntimeError):
    """Raised when unauthenticated access redirects to a login gate."""


@dataclass(frozen=True)
class ProbeOutcome:
    url: str
    final_url: Optional[str]
    http_status: Optional[int]
    robots_allowed: bool
    auth_required: bool
    antibot_detected: bool
    outcome_code: str  # success | blocked_compliance | blocked_auth | blocked_antibot | http_error
    detail: str
    body: str = ""


class CompliantHttpClient:
    """HTTP client wrapper that checks robots.txt before every request and rate-limits per host."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_delay_seconds: float = 1.5,
        timeout_seconds: float = 20.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.user_agent = user_agent
        self.min_delay_seconds = min_delay_seconds
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en-IE,en;q=0.9"},
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._robots_cache: dict[str, Optional[Rfc9309RobotsPolicy]] = {}
        self._last_request_ts: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CompliantHttpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        last = self._last_request_ts.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
        self._last_request_ts[host] = time.monotonic()

    def _get_robots_parser(self, origin: str, host: str) -> Optional[Rfc9309RobotsPolicy]:
        if origin in self._robots_cache:
            return self._robots_cache[origin]
        robots_url = f"{origin}/robots.txt"
        self._throttle(host)
        try:
            resp = self._client.get(robots_url)
            ctype = resp.headers.get("content-type", "").lower()
            # Guard against SPA catch-all routes returning HTML index.html as 200 for /robots.txt
            if resp.status_code == 200 and "html" not in ctype and not resp.text.lstrip().startswith("<!DOCTYPE"):
                rp = Rfc9309RobotsPolicy(resp.text)
                self._robots_cache[origin] = rp
                return rp
        except httpx.HTTPError:
            pass
        self._robots_cache[origin] = None
        return None

    def is_robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._get_robots_parser(origin, parsed.netloc)
        if rp is None:
            return True
        return rp.can_fetch("*", url) and rp.can_fetch(self.user_agent, url)

    @staticmethod
    def detect_antibot(status_code: int, text: str, headers: httpx.Headers) -> bool:
        lower_head = text[:4000].lower()
        if status_code in (403, 429, 503):
            if any(
                marker in lower_head
                for marker in (
                    "challenges.cloudflare.com",
                    "just a moment...",
                    "cf-chl-",
                    "turnstile",
                    "g-recaptcha",
                    "hcaptcha",
                    "verify you are human",
                )
            ):
                return True
        if headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        return False

    @staticmethod
    def detect_auth_wall(final_url: str, text: str) -> bool:
        low_url = final_url.lower()
        if any(p in low_url for p in ("/login", "login.aspx", "/signin", "/sign-in", "autentificarusuario")):
            return True
        low_txt = text[:6000].lower()
        if "<title>login</title>" in low_txt or "<title>sign in" in low_txt:
            return True
        return False

    def fetch_compliant(self, url: str) -> ProbeOutcome:
        """Fetch a public URL only if allowed by robots.txt; classify auth/anti-bot walls cleanly."""
        parsed = urlparse(url)
        if not self.is_robots_allowed(url):
            return ProbeOutcome(
                url=url,
                final_url=None,
                http_status=None,
                robots_allowed=False,
                auth_required=False,
                antibot_detected=False,
                outcome_code="blocked_compliance",
                detail=f"Blocked by {parsed.scheme}://{parsed.netloc}/robots.txt Disallow rule",
            )

        self._throttle(parsed.netloc)
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            return ProbeOutcome(
                url=url,
                final_url=None,
                http_status=None,
                robots_allowed=True,
                auth_required=False,
                antibot_detected=False,
                outcome_code="http_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

        final_url = str(resp.url)
        body = resp.text

        if self.detect_antibot(resp.status_code, body, resp.headers):
            return ProbeOutcome(
                url=url,
                final_url=final_url,
                http_status=resp.status_code,
                robots_allowed=True,
                auth_required=False,
                antibot_detected=True,
                outcome_code="blocked_antibot",
                detail=f"HTTP {resp.status_code} anti-bot / Cloudflare challenge detected; stopping per policy",
                body=body,
            )

        if self.detect_auth_wall(final_url, body):
            return ProbeOutcome(
                url=url,
                final_url=final_url,
                http_status=resp.status_code,
                robots_allowed=True,
                auth_required=True,
                antibot_detected=False,
                outcome_code="blocked_auth",
                detail=f"Redirected to authentication wall ({final_url})",
                body=body,
            )

        if resp.status_code >= 400:
            return ProbeOutcome(
                url=url,
                final_url=final_url,
                http_status=resp.status_code,
                robots_allowed=True,
                auth_required=False,
                antibot_detected=False,
                outcome_code="http_error",
                detail=f"HTTP {resp.status_code}",
                body=body,
            )

        return ProbeOutcome(
            url=url,
            final_url=final_url,
            http_status=resp.status_code,
            robots_allowed=True,
            auth_required=False,
            antibot_detected=False,
            outcome_code="success",
            detail="OK",
            body=body,
        )
