"""Map detections to severity labels and compute lost volume."""

from __future__ import annotations

from . import config
from .detect import Detection


def classify(detection: Detection, country_share_pct: float) -> str:
    if detection.direction == "spike":
        return "P2: Spike"

    is_country_slice = detection.slice_key.issuing_country is not None
    is_primary = country_share_pct >= config.PRIMARY_SHARE_PCT

    if is_country_slice and is_primary:
        return "P1: Critical - Country Outage"
    if not is_country_slice:
        return "P1: Critical - Global Merchant Outage"
    return "P2: Country-level drop"


def lost_volume_amount(detection: Detection) -> float:
    """USD revenue lost (or, for spikes, gained) due to the AR move.

    (expected_ar - observed_ar) * requested_volume * atv. Negative for spikes.
    """
    diff = detection.expected_ar - detection.observed_ar
    return round(diff * detection.requested_volume * detection.atv, 2)


def lost_payment_count(detection: Detection) -> int:
    diff = detection.expected_ar - detection.observed_ar
    return int(round(diff * detection.requested_volume))
