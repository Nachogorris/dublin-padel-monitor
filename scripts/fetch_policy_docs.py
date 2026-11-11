"""Fetch robots.txt, sitemaps, legal pages, and robots-allowed public landing pages.

Strictly enforces robots.txt via urllib.robotparser before fetching any path other
than /robots.txt itself. Never bypasses authentication, CAPTCHA, or anti-bot gates.

Saves each response verbatim under data/raw/policy/<host>__<path>.txt together with
an audit log (data/raw/policy/index.csv) recording URL, HTTP status, robots check,
and UTC fetch timestamp.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "policy"
UA = "DublinPadelMarketResearch/0.2 (academic/market research; robots-compliant)"


def slug(url: str) -> str:
    p = urlparse(url)
    s = f"{p.netloc}__{p.path.strip('/') or 'root'}"
    if p.query:
        s += "__" + p.query
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:150]


def load_robots(client: httpx.Client, origin: str, cache: dict[str, RobotFileParser | None]) -> RobotFileParser | None:
    if origin in cache:
        return cache[origin]
    robots_url = f"{origin}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        r = client.get(robots_url)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            cache[origin] = rp
            return rp
    except httpx.HTTPError:
        pass
    cache[origin] = None
    return None


def main(argv: list[str]) -> int:
    if argv[:1] == ["--file"]:
        urls = [
            u.strip()
            for u in Path(argv[1]).read_text(encoding="utf-8").splitlines()
            if u.strip() and not u.strip().startswith("#")
        ]
    else:
        urls = argv

    OUT.mkdir(parents=True, exist_ok=True)
    index = OUT / "index.csv"
    new = not index.exists()
    robots_cache: dict[str, RobotFileParser | None] = {}

    with (
        httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True) as client,
        index.open("a", newline="", encoding="utf-8") as fh,
    ):
        w = csv.writer(fh)
        if new:
            w.writerow(["fetched_at_utc", "url", "final_url", "status", "bytes", "file"])
        for url in urls:
            p = urlparse(url)
            origin = f"{p.scheme}://{p.netloc}"
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

            if p.path != "/robots.txt":
                rp = load_robots(client, origin, robots_cache)
                if rp is not None and (not rp.can_fetch("*", url) or not rp.can_fetch(UA, url)):
                    w.writerow([ts, url, "", "SKIPPED_ROBOTS_DISALLOW", 0, ""])
                    print(f"SKIP (robots.txt disallow) {url}")
                    continue

            try:
                r = client.get(url)
                fname = slug(url) + ".txt"
                (OUT / fname).write_text(r.text, encoding="utf-8", errors="replace")
                w.writerow([ts, url, str(r.url), r.status_code, len(r.content), fname])
                print(f"{r.status_code} {len(r.content):>8}B {url} -> {r.url}")
                if p.path == "/robots.txt" and r.status_code == 200:
                    rp = RobotFileParser()
                    rp.set_url(url)
                    rp.parse(r.text.splitlines())
                    robots_cache[origin] = rp
            except httpx.HTTPError as exc:
                w.writerow([ts, url, "", f"ERROR {type(exc).__name__}", 0, ""])
                print(f"ERR {url}: {exc}")
            time.sleep(1.5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
