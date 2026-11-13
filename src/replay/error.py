from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.models.artifact import Capability, CheckpointType, OutcomeDetector
from src.models.result import BusinessOutcome
from src.surface.base import SurfaceAdapter


@dataclass
class OutcomeMatch:
    detector: OutcomeDetector
    detail: str


async def evaluate_outcome_detectors(
    detectors: List[OutcomeDetector],
    adapter: SurfaceAdapter,
) -> Optional[OutcomeMatch]:
    """Check all detectors in priority order. Returns the first match, or None."""
    for detector in sorted(detectors, key=lambda d: d.priority):
        matched = await _check_detector(detector, adapter)
        if matched:
            return OutcomeMatch(detector=detector, detail=detector.value)
    return None


async def check_checkpoint(
    checkpoint_type: CheckpointType,
    value: str,
    adapter: SurfaceAdapter,
    outputs: Dict[str, Any],
) -> bool:
    if checkpoint_type == CheckpointType.URL_CONTAINS:
        url = await adapter.current_url()
        return value in url

    if checkpoint_type == CheckpointType.PAGE_CONTAINS_TEXT:
        return await adapter.page_contains_text(value)

    if checkpoint_type == CheckpointType.OUTPUT_EXTRACTED:
        return outputs.get(value) is not None

    if checkpoint_type == CheckpointType.ELEMENT_VISIBLE:
        from src.surface.web import WebAdapter
        if isinstance(adapter, WebAdapter):
            count = await adapter._page.locator(value).count()
            return count > 0
        return False

    return False


async def matches_page_identity(expected_url_pattern: Optional[str],
                                 expected_landmark: Optional[str],
                                 adapter: SurfaceAdapter) -> bool:
    """True if the current page matches the declared page identity."""
    if expected_url_pattern:
        import fnmatch
        url = await adapter.current_url()
        if not fnmatch.fnmatch(url, expected_url_pattern):
            return False

    if expected_landmark:
        if not await adapter.page_contains_text(expected_landmark):
            return False

    return True


async def _check_detector(detector: OutcomeDetector, adapter: SurfaceAdapter) -> bool:
    if detector.detector_type == CheckpointType.PAGE_CONTAINS_TEXT:
        return await adapter.page_contains_text(detector.value)

    if detector.detector_type == CheckpointType.URL_CONTAINS:
        url = await adapter.current_url()
        return detector.value in url

    return False
