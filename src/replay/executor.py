from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from playwright.async_api import BrowserContext

from src.escalation.operator_channel import OperatorChannel
from src.guardrails.policy import EscalationRequired, PolicyGate, PolicyViolation, RouteGuard
from src.models.artifact import (
    ActionType, Capability, CheckpointType, Locator,
    RiskTier, Sensitivity,
)
from src.models.policy import AllowlistPolicy, ExecutionMode
from src.models.result import (
    BusinessOutcome, Failure, FailureKind, InterventionRequest,
    ReplayResult, ReplayStatus,
)
from src.observability.logger import EventType, RunLogger
from src.replay.error import (
    OutcomeMatch, check_checkpoint, evaluate_outcome_detectors, matches_page_identity,
)
from src.replay.locator import LocatorResolutionError, resolve
from src.surface.web import WebAdapter

# Sentinel returned from _execute_step to signal the caller to re-run the same step.
_RETRY = "RETRY_STEP"


def _render(text: Optional[str], params: Dict[str, str]) -> Optional[str]:
    if text is None:
        return None
    for key, value in params.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def _render_locator(loc: Locator, params: Dict[str, str]) -> Locator:
    updates: Dict[str, Any] = {"value": _render(loc.value, params) or ""}
    if loc.anchor:
        anchor_updates: Dict[str, Any] = {}
        if loc.anchor.row_cell_text:
            anchor_updates["row_cell_text"] = _render(loc.anchor.row_cell_text, params)
        if anchor_updates:
            updates["anchor"] = loc.anchor.model_copy(update=anchor_updates)
    return loc.model_copy(update=updates)


# Sentinel return values from _poll_until_settled
_TIMEOUT = "timeout"


class ReplayExecutor:
    """Replay state machine — executes a Capability step-by-step without LLM involvement.

    Can be reused across sequential capability runs (e.g. login then lookup) on the
    same adapter. Each call to run() resets per-run state.

    Pass operator_channel to enable human handoff on NEEDS_HUMAN events.
    Without it, NEEDS_HUMAN events return FAILED immediately (stub behavior).
    """

    def __init__(
        self,
        adapter: WebAdapter,
        policy: AllowlistPolicy,
        logger: RunLogger,
        mode: ExecutionMode,
        operator_channel: Optional[OperatorChannel] = None,
        evidence_dir: Optional[Path] = None,
    ):
        self._adapter = adapter
        self._policy = policy
        self._policy_gate = PolicyGate(policy)
        self._route_guard = RouteGuard(policy)
        self._logger = logger
        self._mode = mode
        self._operator_channel = operator_channel
        self._evidence_dir = evidence_dir
        self._context: Optional[BrowserContext] = None
        self._outputs: Dict[str, Any] = {}
        self._steps_completed = 0
        self._intervention = None  # set when a human handoff occurs; attached to the final result

    async def setup(self, context: BrowserContext) -> None:
        self._context = context
        await self._route_guard.install(context)

    async def run(self, capability: Capability, params: Dict[str, str]) -> ReplayResult:
        # Reset per-run state so the executor can be reused across sequential capabilities
        self._outputs = {}
        self._steps_completed = 0
        self._intervention = None

        started_at = datetime.now(timezone.utc)
        run_id = self._logger._run_id

        schema_error = self._check_lifecycle(capability)
        if schema_error:
            return schema_error

        for param in capability.parameters:
            value = params.get(param.name)
            if param.required and value is None:
                return self._make_failure(
                    FailureKind.SCHEMA_VIOLATION, "pre_run",
                    f"Required parameter {param.name!r}",
                    "not provided",
                    capability, started_at,
                )
            if value and param.pattern:
                if not re.fullmatch(param.pattern, value):
                    return self._make_failure(
                        FailureKind.SCHEMA_VIOLATION, "pre_run",
                        f"Parameter {param.name!r} matches {param.pattern!r}",
                        f"got {value!r}",
                        capability, started_at,
                    )

        # Register ALL non-PUBLIC params as sensitive BEFORE first log write.
        # Covers INTERNAL (member_id), FINANCIAL, PII, and SECRET.
        for param in capability.parameters:
            if param.name in params and param.sensitivity != Sensitivity.PUBLIC:
                self._logger.register_sensitive(param.name, params[param.name])

        self._logger.log(EventType.RUN_START,
                         mode=self._mode.value,
                         lifecycle=capability.lifecycle_status.value,
                         llm_calls=0)

        for step in capability.steps:
            while True:
                result = await self._execute_step(step, params, capability, started_at)
                if result == _RETRY:
                    continue  # human signaled "retry" — re-execute the same step
                break
            if result is not None:
                return result

        for output in capability.outputs:
            if output.name not in self._outputs:
                return self._make_failure(
                    FailureKind.CHECKPOINT_TIMEOUT, "post_run",
                    f"Output {output.name!r} extracted",
                    "not found after all steps",
                    capability, started_at,
                )

        completed_at = datetime.now(timezone.utc)
        self._logger.log(EventType.RUN_COMPLETE, status="SUCCESS", llm_calls=0,
                         outputs=list(self._outputs.keys()))
        return ReplayResult(
            run_id=run_id,
            capability_id=capability.id,
            capability_version=capability.version,
            status=ReplayStatus.SUCCESS,
            outputs=dict(self._outputs),
            intervention=self._intervention,
            steps_completed=self._steps_completed,
            total_steps=len(capability.steps),
            started_at=started_at,
            completed_at=completed_at,
            llm_calls=0,
        )

    async def _capture_failure_screenshot(self, step_id: str) -> Optional[str]:
        if not self._evidence_dir:
            return None
        try:
            shots_dir = self._evidence_dir / "failures"
            shots_dir.mkdir(parents=True, exist_ok=True)
            path = shots_dir / f"{step_id}_failure.png"
            path.write_bytes(await self._adapter.screenshot())
            return str(path)
        except Exception:
            return None

    # ------------------------------------------------------------------ step execution

    async def _execute_step(
        self,
        step,
        params: Dict[str, str],
        capability: Capability,
        started_at: datetime,
    ) -> Optional[Union[ReplayResult, str]]:
        self._logger.log(EventType.STEP_START, step_id=step.step_id, action=step.action.value)

        rendered_value = _render(step.value, params)
        rendered_locators = [_render_locator(loc, params) for loc in step.locators]

        # Pre-action: page identity check
        if step.expected_page:
            on_right_page = await matches_page_identity(
                step.expected_page.url_pattern,
                step.expected_page.landmark_text,
                self._adapter,
            )
            if not on_right_page:
                return await self._handle_needs_human(
                    step,
                    "Page identity mismatch before step — unknown page state",
                    capability, params, started_at,
                )

        # Pre-action: policy gate
        try:
            if step.action == ActionType.NAVIGATE and rendered_value:
                self._policy_gate.check_navigate(rendered_value)
            elif step.action in (ActionType.CLICK, ActionType.FILL):
                self._policy_gate.check_step(step)
        except PolicyViolation as exc:
            self._logger.log(EventType.POLICY_DENIED, step_id=step.step_id, reason=exc.message)
            shot = await self._capture_failure_screenshot(step.step_id)
            return self._make_failure(
                exc.kind, step.step_id,
                "action allowed by policy",
                exc.message,
                capability, started_at,
                screenshot_path=shot,
            )
        except EscalationRequired as exc:
            return await self._handle_needs_human(step, exc.reason, capability, params, started_at)

        if (step.risk_tier == RiskTier.MUTATING_EXPECTED
                and step.expected_method and step.expected_route):
            self._route_guard.expect_mutation(step.expected_method, step.expected_route)

        # Execute action
        try:
            await self._run_action(step, rendered_locators, rendered_value)
        except LocatorResolutionError as exc:
            self._route_guard.clear_expected()
            return await self._handle_locator_error(exc, step, capability, params, started_at)
        except Exception as exc:
            self._route_guard.clear_expected()
            self._logger.log(EventType.STEP_FAILED, step_id=step.step_id, error=str(exc))
            shot = await self._capture_failure_screenshot(step.step_id)
            return self._make_failure(
                FailureKind.LOCATOR_NOT_FOUND, step.step_id,
                "action to complete", str(exc),
                capability, started_at,
                screenshot_path=shot,
            )

        self._route_guard.clear_expected()

        # For click/navigate: wait for the page to settle before polling
        if step.action in (ActionType.CLICK, ActionType.NAVIGATE):
            has_url_post = any(c.type == CheckpointType.URL_CONTAINS for c in step.postconditions)
            if has_url_post or step.risk_tier == RiskTier.MUTATING_EXPECTED:
                try:
                    await self._adapter.wait_for_navigation(step.timeout_ms)
                except Exception:
                    pass  # polling loop will handle it

        # Check for unexpected mutations aborted by route guard
        unexpected = self._route_guard.take_unexpected_mutations()
        if unexpected:
            self._logger.log(EventType.STEP_FAILED, step_id=step.step_id,
                             reason="unexpected_mutation", requests=unexpected)
            shot = await self._capture_failure_screenshot(step.step_id)
            return self._make_failure(
                FailureKind.UNEXPECTED_MUTATION, step.step_id,
                "no undeclared non-GET requests",
                f"{unexpected[0]['method']} {unexpected[0]['url']}",
                capability, started_at,
                screenshot_path=shot,
            )

        # Log any dialogs that were dismissed during the action
        for dialog in self._adapter.pop_dialogs():
            self._logger.log(EventType.DIALOG_CAPTURED, step_id=step.step_id,
                             dialog_type=dialog.dialog_type, message=dialog.message)

        # Unknown dialogs (did not match any registered known pattern) require human review
        unknown_dialogs = (
            self._adapter.pop_unknown_dialogs()
            if hasattr(self._adapter, "pop_unknown_dialogs")
            else []
        )
        if unknown_dialogs:
            return await self._handle_needs_human(
                step,
                f"Unknown dialog requires human review: {unknown_dialogs[0].message!r}",
                capability, params, started_at,
            )

        # Poll detectors + postconditions together until one wins or timeout.
        # This handles slow-loading pages and races between success/failure states.
        settled = await self._poll_until_settled(step, capability, started_at)

        if settled is None:
            pass  # postconditions satisfied
        elif isinstance(settled, OutcomeMatch):
            return self._dispatch_outcome(settled, step.step_id, capability, started_at)
        elif isinstance(settled, str) and settled == _TIMEOUT:
            for cond in step.postconditions:
                self._logger.log(EventType.CHECKPOINT_FAIL, step_id=step.step_id,
                                 cond_type=cond.type.value, cond_value=cond.value)

            # With an OperatorChannel, check if the unexpected page is still in-policy.
            # In-policy but unexpected → NEEDS_HUMAN instead of immediate CHECKPOINT_TIMEOUT.
            # Without an OperatorChannel the existing tests are unaffected.
            if self._operator_channel:
                current_url = await self._adapter.current_url()
                try:
                    self._policy_gate.check_navigate(current_url)
                    first_cond = step.postconditions[0] if step.postconditions else None
                    cond_desc = repr(first_cond.value) if first_cond else "'postcondition'"
                    reason = (
                        f"Checkpoint timeout: {cond_desc} not satisfied"
                        " -- unexpected page state (URL is in-policy)"
                    )
                    return await self._handle_needs_human(
                        step, reason, capability, params, started_at
                    )
                except PolicyViolation:
                    pass  # out-of-policy URL falls through to hard failure

            first_cond = step.postconditions[0] if step.postconditions else None
            shot = await self._capture_failure_screenshot(step.step_id)
            return self._make_failure(
                FailureKind.CHECKPOINT_TIMEOUT, step.step_id,
                first_cond.value if first_cond else "step postcondition",
                f"not satisfied within {step.timeout_ms}ms",
                capability, started_at,
                screenshot_path=shot,
            )
        elif isinstance(settled, str):
            # "needs_human:<reason>" — RECOVERABLE retries exhausted
            reason = settled.split(":", 1)[1] if ":" in settled else settled
            return await self._handle_needs_human(step, reason, capability, params, started_at)

        self._steps_completed += 1
        self._logger.log(EventType.STEP_COMPLETE, step_id=step.step_id)
        return None

    async def _poll_until_settled(
        self,
        step,
        capability: Capability,
        started_at: datetime,
    ) -> Union[None, OutcomeMatch, str]:
        """Poll outcome detectors and postconditions until one wins or timeout.

        Returns:
            None             — postconditions satisfied, proceed
            OutcomeMatch     — BUSINESS_OUTCOME or HARD_FAILURE detector fired
            "timeout"        — neither resolved within step.timeout_ms
            "needs_human:…"  — RECOVERABLE condition persisted past max retries
        """
        max_recoverable = self._policy.action.max_recovery_attempts
        recoverable_count = 0
        deadline = asyncio.get_event_loop().time() + step.timeout_ms / 1000

        while True:
            # Detectors first — a fired detector takes priority over postconditions.
            outcome = await evaluate_outcome_detectors(
                capability.sorted_outcome_detectors(), self._adapter
            )
            if outcome is not None:
                if outcome.detector.disposition == "RECOVERABLE":
                    recoverable_count += 1
                    self._logger.log(EventType.DIALOG_CAPTURED,
                                     step_id=step.step_id,
                                     cond_type="recoverable",
                                     cond_value=outcome.detector.code,
                                     attempt=recoverable_count)
                    if recoverable_count > max_recoverable:
                        return f"needs_human:RECOVERABLE {outcome.detector.code!r} persisted after {recoverable_count} attempts"
                    # Transient condition — continue polling; it should clear
                else:
                    return outcome  # BUSINESS_OUTCOME or HARD_FAILURE

            # Postconditions
            if not step.postconditions:
                return None  # no conditions to wait for

            all_pass = True
            for cond in step.postconditions:
                if not await check_checkpoint(cond.type, cond.value, self._adapter, self._outputs):
                    all_pass = False
                    break

            if all_pass:
                for cond in step.postconditions:
                    self._logger.log(EventType.CHECKPOINT_PASS,
                                     step_id=step.step_id,
                                     cond_type=cond.type.value)
                return None

            if asyncio.get_event_loop().time() >= deadline:
                # Final detector check before giving up — covers the case where the
                # page loaded an error after networkidle but before our last poll
                outcome = await evaluate_outcome_detectors(
                    capability.sorted_outcome_detectors(), self._adapter
                )
                if outcome is not None and outcome.detector.disposition != "RECOVERABLE":
                    return outcome
                return _TIMEOUT

            await asyncio.sleep(0.3)

    async def _run_action(self, step, rendered_locators: list, rendered_value: Optional[str]) -> None:
        action = step.action

        if action == ActionType.NAVIGATE:
            self._logger.log(EventType.ACTION_NAVIGATE, step_id=step.step_id, url=rendered_value)
            await self._adapter.navigate(rendered_value)

        elif action == ActionType.FILL:
            loc = rendered_locators[0]
            self._logger.log(EventType.ACTION_FILL, step_id=step.step_id,
                             field_ref=step.output_key or step.step_id)
            await self._adapter.fill(loc, rendered_value)

        elif action == ActionType.CLICK:
            loc = rendered_locators[0]
            self._logger.log(EventType.ACTION_CLICK, step_id=step.step_id,
                             risk_tier=step.risk_tier.value)
            await self._adapter.click(loc)

        elif action == ActionType.EXTRACT:
            loc = rendered_locators[0]
            value = await self._adapter.extract(loc)
            # Register as sensitive BEFORE logging so the log entry is already scrubbed
            if step.output_key:
                self._logger.register_sensitive(step.output_key, value)
                self._outputs[step.output_key] = value
            self._logger.log(EventType.ACTION_EXTRACT, step_id=step.step_id,
                             output_key=step.output_key, value=value)

        elif action == ActionType.ASSERT:
            pass  # handled via postconditions

    # ------------------------------------------------------------------ error classification

    async def _handle_locator_error(
        self,
        exc: LocatorResolutionError,
        step,
        capability: Capability,
        params: Dict[str, str],
        started_at: datetime,
    ) -> Optional[Union[ReplayResult, str]]:
        """Classify a locator failure as HARD_FAILURE (drift) or NEEDS_HUMAN (unknown state).

        Detectors run FIRST. If the page has changed to an error state (e.g. MEMBER_NOT_FOUND),
        a locator miss is not drift — it is a business outcome. Without this check, the
        not-found page would reach the page-identity test and become NEEDS_HUMAN.
        """
        self._logger.log(EventType.STEP_FAILED, step_id=step.step_id,
                         failure_kind=exc.kind.value, reason=exc.message)

        # 1. Check detectors first — covers the race between navigation and locator resolution
        outcome = await evaluate_outcome_detectors(
            capability.sorted_outcome_detectors(), self._adapter
        )
        if outcome is not None:
            return self._dispatch_outcome(outcome, step.step_id, capability, started_at)

        # 2. Page identity disambiguation:
        #    expected page + locator miss → drift (HARD_FAILURE: LOCATOR_NOT_FOUND)
        #    wrong/unknown page → NEEDS_HUMAN (session expired, interstitial, etc.)
        if step.expected_page:
            on_right_page = await matches_page_identity(
                step.expected_page.url_pattern,
                step.expected_page.landmark_text,
                self._adapter,
            )
            if not on_right_page:
                return await self._handle_needs_human(
                    step,
                    f"Locator failed and page identity is unknown: {exc.message}",
                    capability, params, started_at,
                )

        shot = await self._capture_failure_screenshot(step.step_id)
        return self._make_failure(
            exc.kind, step.step_id,
            "element found", exc.message,
            capability, started_at,
            screenshot_path=shot,
        )

    def _dispatch_outcome(
        self,
        match: OutcomeMatch,
        step_id: str,
        capability: Capability,
        started_at: datetime,
    ) -> ReplayResult:
        self._logger.log(EventType.OUTCOME_DETECTED, step_id=step_id,
                         code=match.detector.code, disposition=match.detector.disposition)

        if match.detector.disposition == "BUSINESS_OUTCOME":
            return ReplayResult(
                run_id=self._logger._run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=ReplayStatus.BUSINESS_OUTCOME,
                business_outcome=BusinessOutcome(
                    code=match.detector.code,
                    detail=match.detail,
                ),
                steps_completed=self._steps_completed,
                total_steps=len(capability.steps),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                llm_calls=0,
            )

        if match.detector.disposition == "RECOVERABLE":
            # Should not reach _dispatch_outcome for RECOVERABLE — polling loop handles it.
            # If it does (e.g. called from _handle_locator_error), treat as NEEDS_HUMAN.
            return self._make_needs_human(
                step_id,
                f"RECOVERABLE condition {match.detector.code!r} at locator error",
                capability, started_at,
            )

        # HARD_FAILURE disposition — map detector code to FailureKind where possible
        failure_kind = FailureKind.APP_ERROR
        try:
            failure_kind = FailureKind(match.detector.code)
        except ValueError:
            pass
        return self._make_failure(
            failure_kind, step_id,
            "healthy page", match.detail,
            capability, started_at,
        )

    # ------------------------------------------------------------------ result builders

    def _check_lifecycle(self, capability: Capability) -> Optional[ReplayResult]:
        if self._mode == ExecutionMode.VALIDATION and not capability.can_run_in_validation_mode():
            return self._schema_fail(
                f"Lifecycle {capability.lifecycle_status.value!r} cannot run in VALIDATION mode"
            )
        if self._mode == ExecutionMode.ATTENDED and not capability.can_run_attended():
            return self._schema_fail(
                f"Lifecycle {capability.lifecycle_status.value!r} requires VALIDATED/APPROVED for ATTENDED"
            )
        if self._mode == ExecutionMode.UNATTENDED and not capability.can_run_unattended():
            return self._schema_fail(
                f"Lifecycle {capability.lifecycle_status.value!r} requires APPROVED for UNATTENDED"
            )
        return None

    def _schema_fail(self, reason: str) -> ReplayResult:
        now = datetime.now(timezone.utc)
        return ReplayResult(
            run_id=self._logger._run_id,
            capability_id="unknown",
            capability_version="unknown",
            status=ReplayStatus.FAILED,
            failure=Failure(
                kind=FailureKind.SCHEMA_VIOLATION,
                step_id="pre_run",
                expected="valid lifecycle + mode combination",
                observed=reason,
            ),
            steps_completed=0,
            total_steps=0,
            started_at=now,
            completed_at=now,
            llm_calls=0,
        )

    def _make_failure(
        self,
        kind: FailureKind,
        step_id: str,
        expected: str,
        observed: str,
        capability: Capability,
        started_at: datetime,
        screenshot_path: Optional[str] = None,
    ) -> ReplayResult:
        self._logger.log(EventType.RUN_COMPLETE, status="FAILED", failure_kind=kind.value)
        return ReplayResult(
            run_id=self._logger._run_id,
            capability_id=capability.id,
            capability_version=capability.version,
            status=ReplayStatus.FAILED,
            failure=Failure(
                kind=kind,
                step_id=step_id,
                expected=expected,
                observed=observed,
                screenshot_path=screenshot_path,
            ),
            steps_completed=self._steps_completed,
            total_steps=len(capability.steps),
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            llm_calls=0,
        )

    async def _handle_needs_human(
        self,
        step,
        reason: str,
        capability: Capability,
        params: Dict[str, str],
        started_at: datetime,
    ) -> Optional[Union[ReplayResult, str]]:
        """Handle a NEEDS_HUMAN event.

        Without OperatorChannel: logs the event and returns FAILED immediately.
        With OperatorChannel: presents the handoff to a human and handles their signal:
          retry   → returns _RETRY sentinel (caller re-executes the step)
          done    → verifies postconditions, returns None (advance) or FAILED
          abort   → returns FAILED
          timeout → returns ESCALATION_TIMEOUT result
        """
        if not self._operator_channel or not self._context:
            # No channel configured — log and fail immediately
            self._logger.log(EventType.ESCALATION_START, step_id=step.step_id, reason=reason)
            self._logger.log(EventType.RUN_COMPLETE, status="NEEDS_HUMAN")
            return ReplayResult(
                run_id=self._logger._run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=ReplayStatus.FAILED,
                failure=Failure(
                    kind=FailureKind.ESCALATION_REQUIRED,
                    step_id=step.step_id,
                    expected="OperatorChannel configured",
                    observed=f"NEEDS_HUMAN: {reason}",
                ),
                steps_completed=self._steps_completed,
                total_steps=len(capability.steps),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                llm_calls=0,
            )

        # Build the intervention request
        current_url = await self._adapter.current_url()
        request = InterventionRequest(
            run_id=self._logger._run_id,
            capability_id=capability.id,
            step_id=step.step_id,
            reason=reason,
            screenshot_path=str(
                self._operator_channel._evidence_dir / "screenshots"
                / f"{step.step_id}_escalation.png"
            ),
            current_url=current_url,
        )

        # Suspend RouteGuard so the human can operate freely
        self._route_guard.set_human_mode(True)
        try:
            signal, human_actions = await self._operator_channel.request_handoff(
                request, self._context, self._adapter._page
            )
        finally:
            self._route_guard.set_human_mode(False)

        # Build the InterventionRecord for logging regardless of signal
        postconds_verified = False

        if signal == "timeout":
            self._logger.log(EventType.RUN_COMPLETE, status="ESCALATION_TIMEOUT")
            return ReplayResult(
                run_id=self._logger._run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=ReplayStatus.ESCALATION_TIMEOUT,
                intervention=OperatorChannel.build_intervention_record(
                    request, signal, human_actions, postconds_verified
                ),
                steps_completed=self._steps_completed,
                total_steps=len(capability.steps),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                llm_calls=0,
            )

        if signal == "abort":
            self._logger.log(EventType.RUN_COMPLETE, status="FAILED", reason="operator_abort")
            return ReplayResult(
                run_id=self._logger._run_id,
                capability_id=capability.id,
                capability_version=capability.version,
                status=ReplayStatus.FAILED,
                failure=Failure(
                    kind=FailureKind.OPERATOR_ABORT,
                    step_id=step.step_id,
                    expected="operator resolved issue",
                    observed="operator signaled abort",
                ),
                intervention=OperatorChannel.build_intervention_record(
                    request, signal, human_actions, postconds_verified
                ),
                steps_completed=self._steps_completed,
                total_steps=len(capability.steps),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                llm_calls=0,
            )

        if signal == "done":
            # Verify postconditions — did the human achieve the step's goal?
            for cond in step.postconditions:
                if not await check_checkpoint(cond.type, cond.value, self._adapter, self._outputs):
                    self._logger.log(EventType.CHECKPOINT_FAIL,
                                     step_id=step.step_id,
                                     cond_type=cond.type.value, cond_value=cond.value)
                    self._logger.log(EventType.RUN_COMPLETE, status="FAILED",
                                     reason="postcondition_not_met_after_handoff")
                    return ReplayResult(
                        run_id=self._logger._run_id,
                        capability_id=capability.id,
                        capability_version=capability.version,
                        status=ReplayStatus.FAILED,
                        failure=Failure(
                            kind=FailureKind.CHECKPOINT_TIMEOUT,
                            step_id=step.step_id,
                            expected=cond.value,
                            observed="postcondition not satisfied after human handoff",
                        ),
                        intervention=OperatorChannel.build_intervention_record(
                            request, signal, human_actions, False
                        ),
                        steps_completed=self._steps_completed,
                        total_steps=len(capability.steps),
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        llm_calls=0,
                    )
            postconds_verified = True
            self._intervention = OperatorChannel.build_intervention_record(
                request, signal, human_actions, True
            )
            self._steps_completed += 1
            self._logger.log(EventType.STEP_COMPLETE, step_id=step.step_id,
                             via="human_handoff")
            return None  # advance to next step

        if signal == "retry":
            # Verify page identity — is the page ready to retry the step?
            if step.expected_page:
                on_right_page = await matches_page_identity(
                    step.expected_page.url_pattern,
                    step.expected_page.landmark_text,
                    self._adapter,
                )
                if not on_right_page:
                    self._logger.log(EventType.RUN_COMPLETE, status="FAILED",
                                     reason="page_not_ready_for_retry")
                    return ReplayResult(
                        run_id=self._logger._run_id,
                        capability_id=capability.id,
                        capability_version=capability.version,
                        status=ReplayStatus.FAILED,
                        failure=Failure(
                            kind=FailureKind.ESCALATION_REQUIRED,
                            step_id=step.step_id,
                            expected=f"page matching {step.expected_page.url_pattern!r}",
                            observed="page identity mismatch after retry signal",
                        ),
                        intervention=OperatorChannel.build_intervention_record(
                            request, signal, human_actions, False
                        ),
                        steps_completed=self._steps_completed,
                        total_steps=len(capability.steps),
                        started_at=started_at,
                        completed_at=datetime.now(timezone.utc),
                        llm_calls=0,
                    )
            self._logger.log(EventType.STEP_START, step_id=step.step_id,
                             action=step.action.value, via="retry_after_handoff")
            return _RETRY  # caller loops back to re-execute this step

        # Unknown signal — treat as abort
        return self._make_failure(
            FailureKind.OPERATOR_ABORT, step.step_id,
            "valid signal (R/D/A)", f"unknown signal: {signal!r}",
            capability, started_at,
        )
