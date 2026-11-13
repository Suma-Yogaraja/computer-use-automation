from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.models.artifact import (
    ActionType,
    AnchorRelativeSpec,
    Capability,
    CompatibilityInfo,
    CheckpointType,
    Checkpoint,
    LifecycleStatus,
    Locator,
    LocatorStrategy,
    OutcomeDetector,
    Output,
    PageIdentity,
    Parameter,
    RiskTier,
    Step,
)


class Recorder:
    """Builds a Capability artifact by accumulating steps during a discovery run.

    Template substitution: concrete parameter values are replaced with {{param_name}}
    references so the recorded artifact works for any input, not just the discovery values.
    """

    def __init__(
        self,
        params: Dict[str, str],
        capability_id: str,
        capability_name: str,
        target_url: str,
    ):
        self._params = params
        self._capability_id = capability_id
        self._capability_name = capability_name
        self._target_url = target_url
        self._steps: List[Step] = []
        self._step_count = 0
        self._screenshots: Dict[str, bytes] = {}

    def _template(self, value: Optional[str]) -> Optional[str]:
        """Replace concrete parameter values with {{param_name}} references."""
        if not value:
            return value
        for name, concrete in self._params.items():
            if concrete and len(concrete) >= 3:
                value = value.replace(concrete, f"{{{{{name}}}}}")
        return value

    def _make_anchor_locator(
        self,
        row_context: str,
        target_role: str,
        frame_name: Optional[str] = None,
        column_header: Optional[str] = None,
    ) -> Locator:
        templated_row = self._template(row_context)
        return Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value=f"{target_role} in {templated_row!r} row",
            role=target_role,
            frame_path=[frame_name] if frame_name else [],
            anchor=AnchorRelativeSpec(
                target_role=target_role,
                row_cell_text=templated_row,
                column_header=column_header,
            ),
        )

    def _make_aria_role_locator(self, role: str, name: str) -> Locator:
        return Locator(
            strategy=LocatorStrategy.ARIA_ROLE_NAME,
            value=name,
            role=role,
        )

    def _url_pattern(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/") or "/"
        return f"*{path}*"

    def _url_postcond(self, pre_url: str, post_url: str) -> Optional[Checkpoint]:
        pre_path = urlparse(pre_url).path
        post_path = urlparse(post_url).path
        if pre_path != post_path and post_path:
            return Checkpoint(type=CheckpointType.URL_CONTAINS, value=post_path)
        return None

    def last_step_id(self) -> str:
        return self._steps[-1].step_id if self._steps else ""

    # ------------------------------------------------------------------ record methods

    def record_navigate(self, url: str, pre_url: str, post_url: str) -> str:
        self._step_count += 1
        slug = urlparse(url).path.strip("/").replace("/", "_") or "home"
        step_id = f"step_{self._step_count:02d}_navigate_{slug[:20]}"
        postcond = self._url_postcond(pre_url, post_url)
        templated_url = self._template(url)
        self._steps.append(Step(
            step_id=step_id,
            action=ActionType.NAVIGATE,
            value=templated_url,
            locators=[],
            risk_tier=RiskTier.READ,
            postconditions=[postcond] if postcond else [],
            timeout_ms=10000,
            retry_safe=True,
        ))
        return step_id

    def record_fill(
        self,
        value: str,
        row_context: Optional[str],
        label: Optional[str],
        pre_url: str,
        landmark: Optional[str],
    ) -> str:
        self._step_count += 1
        templated_value = self._template(value)

        if row_context:
            locator = self._make_anchor_locator(row_context, "textbox")
            templated_row = self._template(row_context)
            step_id = f"step_{self._step_count:02d}_fill_{_slug(templated_row or row_context)}"
        elif label:
            locator = Locator(
                strategy=LocatorStrategy.LABEL_ASSOCIATION,
                value=label,
                role="textbox",
            )
            step_id = f"step_{self._step_count:02d}_fill_{_slug(label)}"
        else:
            raise ValueError("record_fill requires row_context or label")

        expected_page = PageIdentity(
            url_pattern=self._url_pattern(pre_url),
            landmark_text=landmark,
        )
        self._steps.append(Step(
            step_id=step_id,
            action=ActionType.FILL,
            value=templated_value,
            locators=[locator],
            expected_page=expected_page,
            risk_tier=RiskTier.READ,
            timeout_ms=10000,
            retry_safe=True,
        ))
        return step_id

    def record_click(
        self,
        accessible_name: str,
        role: str,
        row_context: Optional[str],
        is_submit: bool,
        expected_route: Optional[str],
        pre_url: str,
        landmark: Optional[str],
        post_url: str,
    ) -> str:
        self._step_count += 1
        templated_name = self._template(accessible_name)

        if row_context:
            locator = self._make_anchor_locator(row_context, role)
            step_id = f"step_{self._step_count:02d}_click_{_slug(templated_name or accessible_name)}_row"
        else:
            locator = self._make_aria_role_locator(role, templated_name)
            step_id = f"step_{self._step_count:02d}_click_{_slug(accessible_name)}"

        expected_page = PageIdentity(
            url_pattern=self._url_pattern(pre_url),
            landmark_text=landmark,
        )
        postcond = self._url_postcond(pre_url, post_url)
        risk_tier = RiskTier.MUTATING_EXPECTED if is_submit else RiskTier.READ

        step = Step(
            step_id=step_id,
            action=ActionType.CLICK,
            locators=[locator],
            expected_page=expected_page,
            risk_tier=risk_tier,
            postconditions=[postcond] if postcond else [],
            timeout_ms=15000,
            retry_safe=not is_submit,
            expected_method="POST" if is_submit else None,
            expected_route=expected_route if is_submit else None,
        )
        self._steps.append(step)
        return step_id

    def record_extract(
        self,
        output_key: str,
        row_context: str,
        column_header: Optional[str],
        frame_name: Optional[str],
        pre_url: str,
        landmark: Optional[str],
    ) -> str:
        self._step_count += 1
        step_id = f"step_{self._step_count:02d}_extract_{_slug(output_key)}"
        locator = self._make_anchor_locator(
            row_context, "cell",
            frame_name=frame_name,
            column_header=column_header,
        )
        expected_page = PageIdentity(
            url_pattern=self._url_pattern(pre_url),
            landmark_text=landmark,
        )
        self._steps.append(Step(
            step_id=step_id,
            action=ActionType.EXTRACT,
            output_key=output_key,
            locators=[locator],
            expected_page=expected_page,
            risk_tier=RiskTier.READ,
            postconditions=[Checkpoint(type=CheckpointType.OUTPUT_EXTRACTED, value=output_key)],
            timeout_ms=10000,
            retry_safe=True,
        ))
        return step_id

    def add_screenshot(self, step_id: str, png: bytes) -> None:
        self._screenshots[step_id] = png

    def save_screenshots(self, evidence_dir: Path) -> Dict[str, Path]:
        shots_dir = evidence_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        result: Dict[str, Path] = {}
        for step_id, png in self._screenshots.items():
            path = shots_dir / f"{step_id}.png"
            path.write_bytes(png)
            result[step_id] = path
        return result

    def finalize(
        self,
        param_specs: List[Parameter],
        output_specs: List[Output],
        outcome_detectors: List[OutcomeDetector],
    ) -> Capability:
        return Capability(
            id=self._capability_id,
            version="1.0.0",
            name=self._capability_name,
            description=(
                f"Recorded by discovery agent on "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            ),
            target=self._target_url,
            compatibility=CompatibilityInfo(vendor="demo-bank", app_version="1.0"),
            parameters=param_specs,
            outputs=output_specs,
            outcome_detectors=outcome_detectors,
            lifecycle_status=LifecycleStatus.DRAFT,
            human_assisted=False,
            recorded_by="discovery-agent",
            recorded_at=datetime.now(timezone.utc),
            steps=self._steps,
        )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30]
