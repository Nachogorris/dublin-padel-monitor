"""Abstract base adapter contract for Dublin padel booking platforms."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..models import CollectionRunResult


@dataclass(frozen=True)
class AdapterCapabilities:
    platform_id: str
    supports_live_public_metadata: bool
    supports_live_public_slots: bool
    supports_authorized_api_slots: bool
    requires_credentials_for_slots: bool
    compliance_notes: str


class BasePlatformAdapter(ABC):
    """Reusable interface implemented by every platform collector or probe."""

    @property
    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        raise NotImplementedError

    @abstractmethod
    def collect_club_availability(
        self,
        club_id: str,
        booking_url: str,
        target_date: date,
    ) -> CollectionRunResult:
        raise NotImplementedError

