from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.agent.llm import LLMClient, ProposedAction
from src.guardrails.policy import PolicyGate, PolicyViolation
from src.models.artifact import (
    AnchorRelativeSpec,
    Capability,
    Locator,
    LocatorStrategy,
    OutcomeDetector,
    Output,
    Parameter,
)
from src.models.policy import AllowlistPolicy
from src.observability.logger import EventType, RunLogger
from src.recorder.recorder import Recorder
from src.replay.error import evaluate_outcome_detectors
from src.replay.locator import LocatorResolutionError
from src.surface.web import WebAdapter


@dataclass
class DiscoveryResult:
    success: bool
    capability: Optional[Capability]
    capability_path: Optional[Path]
    llm_calls: int
    steps_recorded: int
    duration_seconds: float
    failure_reason: Optional[str]
    screenshot_paths: Dict[str, Path] = field(default_factory=dict)


def _render(text: Optional[str], params: Dict[str, str]) -> Optional[str]:
    """Substitute {{param_name}} references with actual values for browser execution."""
    if text is None:
        return None
    for key, value in params.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def _format_page_state(obs) -> str:
    """Format an ObservationResult into a human-readable string for the LLM."""
    lines = [
        f"URL: {obs.url}",
        f"Title: {obs.title}",
        "",
        "--- ARIA snapshot (use this to find elements) ---",
        obs.main_snapshot or "(empty)",
    ]
    for fs in obs.frame_snapshots:
        lines += ["", f"--- Frame: {fs.frame_name} ---", fs.yaml]
    return "\n".join(lines)


def _obs_to_content(obs, text_prefix: str = "") -> List[Dict]:
    """Build a message content list that includes a screenshot + ARIA text.

    Including the screenshot lets the LLM visually confirm which elements are
    present (especially links in table rows) rather than guessing from text alone.
    """
    b64 = base64.b64encode(obs.screenshot_png).decode()
    content = []
    if text_prefix:
        content.append({"type": "text", "text": text_prefix})
    content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    })
    content.append({
        "type": "text",
        "text": _format_page_state(obs),
    })
    return content


def _content_to_dicts(content) -> list:
    """Convert Anthropic SDK content blocks to plain dicts for the messages list."""
    result = []
    for block in content:
        if hasattr(block, "type"):
            if block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
    return result


def _build_exec_locator(action: str, inputs: Dict[str, Any], params: Dict[str, str]) -> Optional[Locator]:
    """Build a Locator for browser execution using actual (rendered) parameter values."""
    if action == "fill":
        row_context = _render(inputs.get("row_context"), params)
        label = inputs.get("label")
        if row_context:
            return Locator(
                strategy=LocatorStrategy.ANCHOR_RELATIVE,
                value=row_context,
                role="textbox",
                anchor=AnchorRelativeSpec(target_role="textbox", row_cell_text=row_context),
            )
        if label:
            return Locator(strategy=LocatorStrategy.LABEL_ASSOCIATION, value=label, role="textbox")

    elif action == "click":
        role = inputs["role"]
        row_context = _render(inputs.get("row_context"), params)
        accessible_name = _render(inputs["accessible_name"], params)
        if row_context:
            return Locator(
                strategy=LocatorStrategy.ANCHOR_RELATIVE,
                value=accessible_name or row_context,
                role=role,
                anchor=AnchorRelativeSpec(target_role=role, row_cell_text=row_context),
            )
        return Locator(strategy=LocatorStrategy.ARIA_ROLE_NAME, value=accessible_name, role=role)

    elif action == "extract":
        row_context = _render(inputs["row_context"], params)
        column_header = inputs.get("column_header")
        frame_name = inputs.get("frame_name")
        return Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value=f"cell in {row_context!r} row",
            role="cell",
            frame_path=[frame_name] if frame_name else [],
            anchor=AnchorRelativeSpec(
                target_role="cell",
                row_cell_text=row_context,
                column_header=column_header,
            ),
        )

    return None


class DiscoveryLoop:
    """LLM-driven browser discovery loop.

    Records each successful action as a Step in a Capability artifact.
    The LLM proposes actions using template syntax ({{param_name}}).
    The recorder converts these into typed, replayable locators.
    No RouteGuard installed: the PolicyGate handles pre-action URL checks,
    and the loop captures actual network requests to infer form-submit metadata.
    """

    def __init__(
        self,
        adapter: WebAdapter,
        policy: AllowlistPolicy,
        logger: RunLogger,
        llm: Optional[LLMClient] = None,
        max_steps: int = 25,
        wall_clock_seconds: float = 300,
        evidence_dir: Optional[Path] = None,
    ):
        self._adapter = adapter
        self._policy = policy
        self._policy_gate = PolicyGate(policy)
        self._logger = logger
        self._llm = llm or LLMClient()
        self._max_steps = max_steps
        self._wall_clock_seconds = wall_clock_seconds
        self._evidence_dir = evidence_dir

    async def run(
        self,
        capability_id: str,
        capability_name: str,
        goal: str,
        target_url: str,
        params: Dict[str, str],
        param_specs: List[Parameter],
        output_specs: List[Output],
        outcome_detectors: List[OutcomeDetector],
    ) -> DiscoveryResult:
        started = time.time()
        llm_calls = 0
        extracted: Dict[str, str] = {}

        recorder = Recorder(params, capability_id, capability_name, target_url)
        system = self._llm.build_system_prompt(
            goal=goal,
            params=params,
            outputs=[{"name": o.name, "type": o.type} for o in output_specs],
            allowed_domains=self._policy.allowed_domains,
        )

        # Navigate to the target URL and take the initial observation
        await self._adapter.navigate(target_url)
        obs = await self._adapter.observe()
        pre_url = obs.url

        # Record the initial navigate step
        recorder.record_navigate(target_url, "", target_url)
        recorder.add_screenshot(recorder.last_step_id(), obs.screenshot_png)

        messages: List[Dict] = [{
            "role": "user",
            "content": _obs_to_content(
                obs,
                "Starting discovery. Here is the current page. "
                "Begin the workflow — remember to use template syntax for all parameter values.",
            ),
        }]

        self._logger.log(EventType.RUN_START, mode="discovery", capability_id=capability_id)

        for step_num in range(1, self._max_steps + 1):
            if time.time() - started > self._wall_clock_seconds:
                self._logger.log(EventType.RUN_COMPLETE, status="TIMEOUT",
                                 llm_calls=llm_calls)
                return DiscoveryResult(
                    success=False, capability=None, capability_path=None,
                    llm_calls=llm_calls, steps_recorded=len(recorder._steps),
                    duration_seconds=time.time() - started,
                    failure_reason=f"wall_clock exceeded {self._wall_clock_seconds}s",
                )

            # --- Propose ---
            try:
                proposed = self._llm.propose_action(messages, system)
                llm_calls += 1
                self._logger.log(EventType.LLM_CALL, step_num=step_num,
                                 action=proposed.action)
            except Exception as exc:
                self._logger.log(EventType.RUN_COMPLETE, status="FAILED", reason=str(exc))
                return DiscoveryResult(
                    success=False, capability=None, capability_path=None,
                    llm_calls=llm_calls, steps_recorded=len(recorder._steps),
                    duration_seconds=time.time() - started,
                    failure_reason=f"LLM error: {exc}",
                )

            # Append assistant response (all content blocks, not just the tool_use)
            messages.append({
                "role": "assistant",
                "content": _content_to_dicts(proposed.response_content),
            })

            # --- Terminal: goal achieved ---
            if proposed.action == "goal_complete":
                missing = [o.name for o in output_specs if o.name not in extracted]
                if missing:
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": proposed.tool_use_id,
                            "content": (
                                f"Not complete yet — these outputs are still missing: {missing}. "
                                f"Continue extracting them."
                            ),
                        }],
                    })
                    continue
                return await self._finalize(
                    recorder, param_specs, output_specs, outcome_detectors,
                    llm_calls, started, proposed.tool_use_id,
                )

            # --- Terminal: business outcome ---
            if proposed.action == "goal_failed":
                code = proposed.inputs.get("outcome_code", "UNKNOWN")
                self._logger.log(EventType.OUTCOME_DETECTED, code=code,
                                 detail=proposed.inputs.get("detail", ""))
                self._logger.log(EventType.RUN_COMPLETE, status="BUSINESS_OUTCOME", code=code)
                return DiscoveryResult(
                    success=False, capability=None, capability_path=None,
                    llm_calls=llm_calls, steps_recorded=len(recorder._steps),
                    duration_seconds=time.time() - started,
                    failure_reason=f"BUSINESS_OUTCOME: {code}",
                )

            # --- Policy gate (navigate URL check) ---
            if proposed.action == "navigate":
                try:
                    self._policy_gate.check_navigate(proposed.inputs["url"])
                except PolicyViolation as exc:
                    self._logger.log(EventType.POLICY_DENIED, reason=exc.message,
                                     step_num=step_num)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": proposed.tool_use_id,
                            "is_error": True,
                            "content": f"POLICY_DENIED: {exc.message}. Choose a different action.",
                        }],
                    })
                    continue

            # --- Observe pre-action state ---
            pre_obs = await self._adapter.observe()
            pre_url = pre_obs.url
            landmark = pre_obs.title
            screenshot = pre_obs.screenshot_png

            # --- Execute action (with request capture for click) ---
            captured_requests: List[Dict] = []
            exec_error: Optional[Exception] = None

            try:
                if proposed.action == "click":
                    captured_requests = await self._click_capture_requests(
                        proposed, params
                    )
                else:
                    await self._execute_action(proposed, params, extracted)
            except (LocatorResolutionError, Exception) as exc:
                exec_error = exc

            if exec_error is not None:
                # Check detectors — page may have changed to an error/outcome state
                outcome = await evaluate_outcome_detectors(outcome_detectors, self._adapter)
                if outcome:
                    self._logger.log(EventType.OUTCOME_DETECTED, code=outcome.detector.code)
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": proposed.tool_use_id,
                            "is_error": True,
                            "content": (
                                f"The action failed and the page shows outcome "
                                f"{outcome.detector.code!r}: {outcome.detail}. "
                                f"Call goal_failed with this code."
                            ),
                        }],
                    })
                else:
                    self._logger.log(EventType.STEP_FAILED, action=proposed.action,
                                     error=str(exec_error))
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": proposed.tool_use_id,
                            "is_error": True,
                            "content": (
                                f"Action failed: {exec_error}. "
                                f"Check the ARIA snapshot and try a different approach."
                            ),
                        }],
                    })
                continue

            # --- Wait for navigation to settle ---
            if proposed.action in ("navigate", "click"):
                try:
                    await self._adapter.wait_for_navigation(10000)
                except Exception:
                    pass

            # --- Observe post-action state ---
            post_obs = await self._adapter.observe()
            post_url = post_obs.url

            # --- Determine if click was a form submit (from captured requests) ---
            is_submit = len(captured_requests) > 0
            expected_route = captured_requests[0]["path"] if captured_requests else None

            # --- Record the step ---
            step_id = self._record_step(
                recorder, proposed, params,
                pre_url, landmark, post_url,
                is_submit, expected_route,
                extracted,
            )
            recorder.add_screenshot(step_id, screenshot)
            self._logger.log(EventType.STEP_COMPLETE, step_id=step_id,
                             action=proposed.action)

            # --- Append tool result + new page state (with screenshot) ---
            extracted_note = (
                f"Outputs extracted so far: {list(extracted.keys())}"
                if extracted else "No outputs extracted yet."
            )
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": proposed.tool_use_id,
                        "content": f"Step {step_num} completed. {extracted_note}",
                    },
                    *_obs_to_content(post_obs),
                ],
            })

        # max_steps reached without goal_complete
        self._logger.log(EventType.RUN_COMPLETE, status="TIMEOUT", llm_calls=llm_calls)
        return DiscoveryResult(
            success=False, capability=None, capability_path=None,
            llm_calls=llm_calls, steps_recorded=len(recorder._steps),
            duration_seconds=time.time() - started,
            failure_reason=f"max_steps ({self._max_steps}) reached without goal_complete",
        )

    # ------------------------------------------------------------------ action execution

    async def _execute_action(
        self,
        proposed: ProposedAction,
        params: Dict[str, str],
        extracted: Dict[str, str],
    ) -> None:
        action = proposed.action
        inputs = proposed.inputs

        if action == "navigate":
            await self._adapter.navigate(inputs["url"])

        elif action == "fill":
            locator = _build_exec_locator(action, inputs, params)
            value = _render(inputs["value"], params)
            await self._adapter.fill(locator, value)

        elif action == "extract":
            locator = _build_exec_locator(action, inputs, params)
            value = await self._adapter.extract(locator)
            extracted[inputs["output_key"]] = value

    async def _click_capture_requests(
        self,
        proposed: ProposedAction,
        params: Dict[str, str],
    ) -> List[Dict]:
        """Execute click and capture any non-GET requests that fire during it.

        The captured request metadata is used by the recorder to infer whether
        the click was a form submit (risk_tier=MUTATING_EXPECTED) without asking
        the LLM to declare HTTP method and route.
        """
        captured: List[Dict] = []

        def on_request(req) -> None:
            if req.method != "GET":
                path = urlparse(req.url).path
                captured.append({"method": req.method, "path": path})

        self._adapter._page.on("request", on_request)
        try:
            locator = _build_exec_locator("click", proposed.inputs, params)
            await self._adapter.click(locator)
        finally:
            self._adapter._page.remove_listener("request", on_request)

        return captured

    # ------------------------------------------------------------------ step recording

    def _record_step(
        self,
        recorder: Recorder,
        proposed: ProposedAction,
        params: Dict[str, str],
        pre_url: str,
        landmark: Optional[str],
        post_url: str,
        is_submit: bool,
        expected_route: Optional[str],
        extracted: Dict[str, str],
    ) -> str:
        action = proposed.action
        inputs = proposed.inputs

        if action == "navigate":
            return recorder.record_navigate(inputs["url"], pre_url, post_url)

        if action == "fill":
            return recorder.record_fill(
                value=inputs["value"],
                row_context=inputs.get("row_context"),
                label=inputs.get("label"),
                pre_url=pre_url,
                landmark=landmark,
            )

        if action == "click":
            return recorder.record_click(
                accessible_name=inputs["accessible_name"],
                role=inputs["role"],
                row_context=inputs.get("row_context"),
                is_submit=is_submit,
                expected_route=expected_route,
                pre_url=pre_url,
                landmark=landmark,
                post_url=post_url,
            )

        if action == "extract":
            return recorder.record_extract(
                output_key=inputs["output_key"],
                row_context=inputs["row_context"],
                column_header=inputs.get("column_header"),
                frame_name=inputs.get("frame_name"),
                pre_url=pre_url,
                landmark=landmark,
            )

        return f"step_{recorder._step_count:02d}_unknown"

    # ------------------------------------------------------------------ finalize

    async def _finalize(
        self,
        recorder: Recorder,
        param_specs: List[Parameter],
        output_specs: List[Output],
        outcome_detectors: List[OutcomeDetector],
        llm_calls: int,
        started: float,
        final_tool_use_id: str,
    ) -> DiscoveryResult:
        capability = recorder.finalize(param_specs, output_specs, outcome_detectors)
        capability_path: Optional[Path] = None
        screenshot_paths: Dict[str, Path] = {}

        if self._evidence_dir:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            capability_path = self._evidence_dir / "capability.json"
            capability_path.write_text(capability.model_dump_json(indent=2))
            screenshot_paths = recorder.save_screenshots(self._evidence_dir)

        self._logger.log(EventType.RUN_COMPLETE, status="SUCCESS",
                         llm_calls=llm_calls, steps=len(capability.steps))

        return DiscoveryResult(
            success=True,
            capability=capability,
            capability_path=capability_path,
            llm_calls=llm_calls,
            steps_recorded=len(capability.steps),
            duration_seconds=time.time() - started,
            failure_reason=None,
            screenshot_paths=screenshot_paths,
        )
