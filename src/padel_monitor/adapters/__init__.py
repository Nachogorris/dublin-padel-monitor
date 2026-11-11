"""Platform collector, probe, and zero-API court measurement adapters."""
from .base import AdapterCapabilities, BasePlatformAdapter
from .browser_snapshot import (
    BrowserSnapshotDropzoneAdapter,
    ParsedInboxDescriptor,
    parse_inbox_filename,
    resolve_snapshot_timestamp_and_bucket,
)
from .feed_csv import CsvObservationAdapter
from .platform_probes import GenericPortalProbeAdapter
from .playtomic_official_api import PlaytomicOfficialApiAdapter
from .playtomic_public import PlaytomicPublicClubAdapter
from .spot_check import (
    StratifiedSpotEstimate,
    build_spot_check_record,
    classify_spot_stratum,
    estimate_stratified_occupancy,
)

__all__ = [
    "AdapterCapabilities",
    "BasePlatformAdapter",
    "BrowserSnapshotDropzoneAdapter",
    "ParsedInboxDescriptor",
    "parse_inbox_filename",
    "resolve_snapshot_timestamp_and_bucket",
    "CsvObservationAdapter",
    "GenericPortalProbeAdapter",
    "PlaytomicOfficialApiAdapter",
    "PlaytomicPublicClubAdapter",
    "StratifiedSpotEstimate",
    "build_spot_check_record",
    "classify_spot_stratum",
    "estimate_stratified_occupancy",
]


