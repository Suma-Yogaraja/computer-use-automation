#!/usr/bin/env python3
"""
Step 7 test: discovery agent produces a replayable artifact.

Requires ANTHROPIC_API_KEY.  Set DISCOVERY_MODEL to override the model
(e.g. claude-haiku-4-5-20251001 for a cheaper test run).

Pass:
  1. Discovery run succeeds within bounds
  2. Produced artifact has the right structure (steps, outputs, templates)
  3. No concrete parameter values leak into the artifact
  4. Artifact replays correctly and produces correct output values
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from src.agent.loop import DiscoveryLoop
from src.models.artifact import (
    Capability,
    CheckpointType,
    OutcomeDetector,
    Output,
    Parameter,
    Sensitivity,
)
from src.models.policy import ActionPolicy, AllowlistPolicy, ExecutionMode
from src.models.result import ReplayStatus
from src.observability.logger import RunLogger
from src.replay.executor import ReplayExecutor
from src.surface.web import WebAdapter

FIXTURES = Path("artifacts/fixtures")
EVIDENCE_DIR = Path("evidence")

POLICY = AllowlistPolicy(
    allowed_domains=["localhost:8080"],
    allowed_routes=[
        "/", "/login", "/dashboard", "/member/search",
        "/account", "/account-frame", "/interstitial",
    ],
    blocked_routes=[],
    action=ActionPolicy(),
)

PARAM_SPECS = [
    Parameter(
        name="member_id",
        type="string",
        required=True,
        pattern=r"^\d{5}$",
        sensitivity=Sensitivity.INTERNAL,
    ),
]

OUTPUT_SPECS = [
    Output(name="savings_balance", type="string", sensitivity=Sensitivity.FINANCIAL),
    Output(name="checking_balance", type="string", sensitivity=Sensitivity.FINANCIAL),
]

OUTCOME_DETECTORS = [
    OutcomeDetector(
        code="SESSION_EXPIRED",
        detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
        value="Your session has expired",
        disposition="HARD_FAILURE",
        priority=2,
    ),
    OutcomeDetector(
        code="APP_ERROR",
        detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
        value="An unexpected server error occurred",
        disposition="HARD_FAILURE",
        priority=3,
    ),
    OutcomeDetector(
        code="MEMBER_NOT_FOUND",
        detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
        value="No member found with ID",
        disposition="BUSINESS_OUTCOME",
        priority=10,
    ),
    OutcomeDetector(
        code="ACCOUNT_FROZEN",
        detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
        value="Frozen",
        disposition="BUSINESS_OUTCOME",
        priority=10,
    ),
]


def _pass(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


async def main() -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("SKIP: ANTHROPIC_API_KEY not set — skipping discovery test")
        return 0

    failures = 0
    model = os.getenv("DISCOVERY_MODEL", "claude-sonnet-4-6")
    print(f"Discovery model: {model}")

    proc = subprocess.Popen(
        ["python3", "target_app/server.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)

    run_id = f"discovery-{int(time.time())}"
    evidence_dir = EVIDENCE_DIR / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        adapter = WebAdapter(page, ctx)

        log_path = evidence_dir / "run_log.jsonl"

        with RunLogger(run_id, "member.lookup_balance", log_path) as logger:

            # ---------------------------------------------------------------- Login pre-condition
            # Run in its own context so the RouteGuard it installs doesn't
            # interfere with the undeclared POSTs that discovery generates.
            print("\n--- Login pre-condition ---")
            login_cap = Capability.model_validate_json(
                (FIXTURES / "login.json").read_text()
            )
            login_executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION)
            await login_executor.setup(ctx)
            login_result = await login_executor.run(
                login_cap, {"username": "operator", "password": "demo123"}
            )

            if login_result.status == ReplayStatus.SUCCESS:
                _pass("Login pre-condition succeeded")
            else:
                _fail("Login pre-condition", f"{login_result.status}: {login_result.failure}")
                failures += 1
                await browser.close()
                proc.terminate()
                return failures

            # Copy the authenticated session cookies into a fresh context so
            # discovery runs without the RouteGuard from the login executor.
            session_cookies = await ctx.cookies()
            ctx_disc = await browser.new_context()
            await ctx_disc.add_cookies(session_cookies)
            page_disc = await ctx_disc.new_page()
            adapter_disc = WebAdapter(page_disc, ctx_disc)

            # ---------------------------------------------------------------- Discovery run
            print("\n--- Discovery run (member 10001, happy path) ---")
            loop = DiscoveryLoop(
                adapter=adapter_disc,
                policy=POLICY,
                logger=logger,
                max_steps=25,
                wall_clock_seconds=300,
                evidence_dir=evidence_dir,
            )

            result = await loop.run(
                capability_id="member.lookup_balance",
                capability_name="Member Balance Lookup",
                goal=(
                    "Look up savings and checking account balances for a member "
                    "using member_id, starting from the authenticated dashboard."
                ),
                target_url="http://localhost:8080/dashboard",
                params={"member_id": "10001"},
                param_specs=PARAM_SPECS,
                output_specs=OUTPUT_SPECS,
                outcome_detectors=OUTCOME_DETECTORS,
            )
            await ctx_disc.close()

        # ---------------------------------------------------------------- Assert discovery result
        print(f"\n  Discovery: {result.llm_calls} LLM calls, "
              f"{result.steps_recorded} steps, {result.duration_seconds:.1f}s")

        if result.success:
            _pass(f"Discovery succeeded in {result.llm_calls} LLM calls")
        else:
            _fail("Discovery", result.failure_reason or "unknown reason")
            failures += 1

        cap = result.capability

        if cap is not None:
            if cap.id == "member.lookup_balance":
                _pass(f"capability.id = {cap.id!r}")
            else:
                _fail("capability.id", f"got {cap.id!r}")
                failures += 1

            if len(cap.steps) >= 4:
                _pass(f"capability.steps = {len(cap.steps)} (≥ 4 required)")
            else:
                _fail("capability.steps", f"only {len(cap.steps)} steps")
                failures += 1

            has_savings = any(s.output_key == "savings_balance" for s in cap.steps)
            has_checking = any(s.output_key == "checking_balance" for s in cap.steps)
            if has_savings and has_checking:
                _pass("capability includes savings_balance and checking_balance extract steps")
            else:
                _fail("capability outputs",
                      f"savings_balance={has_savings}, checking_balance={has_checking}")
                failures += 1

            cap_json = cap.model_dump_json()
            if "10001" not in cap_json:
                _pass("No concrete member_id in artifact (properly templated to {{member_id}})")
            else:
                _fail("Templating", "'10001' leaked into artifact JSON")
                failures += 1

            if result.screenshot_paths:
                _pass(f"{len(result.screenshot_paths)} step screenshots saved to evidence/")
            else:
                _fail("Screenshots", "no screenshots saved")
                failures += 1

        # ---------------------------------------------------------------- Replay the produced artifact
        if result.success and cap is not None and result.capability_path:
            print("\n--- Replaying produced artifact (member 10001) ---")
            ctx2 = await browser.new_context()
            page2 = await ctx2.new_page()
            adapter2 = WebAdapter(page2, ctx2)

            replay_log = evidence_dir / "replay_log.jsonl"
            with RunLogger(f"replay-{run_id}", cap.id, replay_log) as replay_logger:
                replay_cap = Capability.model_validate_json(
                    result.capability_path.read_text()
                )
                replay_executor = ReplayExecutor(
                    adapter2, POLICY, replay_logger, ExecutionMode.VALIDATION
                )
                await replay_executor.setup(ctx2)

                # Login first
                login_cap2 = Capability.model_validate_json(
                    (FIXTURES / "login.json").read_text()
                )
                login_r2 = await replay_executor.run(
                    login_cap2, {"username": "operator", "password": "demo123"}
                )
                if login_r2.status != ReplayStatus.SUCCESS:
                    _fail("Replay login", str(login_r2.failure))
                    failures += 1
                else:
                    replay_result = await replay_executor.run(
                        replay_cap, {"member_id": "10001"}
                    )
                    if replay_result.status == ReplayStatus.SUCCESS:
                        _pass("Replay of produced artifact succeeded")
                    else:
                        _fail("Replay", f"{replay_result.status}: {replay_result.failure}")
                        failures += 1

                    if (replay_result.outputs
                            and replay_result.outputs.get("savings_balance") == "$1,234.56"):
                        _pass(f"Replayed savings_balance = "
                              f"{replay_result.outputs['savings_balance']!r}")
                    else:
                        _fail("Replayed savings_balance",
                              f"got {replay_result.outputs}")
                        failures += 1

                    if (replay_result.outputs
                            and replay_result.outputs.get("checking_balance") == "$456.78"):
                        _pass(f"Replayed checking_balance = "
                              f"{replay_result.outputs['checking_balance']!r}")
                    else:
                        _fail("Replayed checking_balance",
                              f"got {replay_result.outputs}")
                        failures += 1

                    # Also test replay with member 10002 to prove the template works
                    print("\n--- Replay with member 10002 (template test) ---")
                    ctx3 = await browser.new_context()
                    page3 = await ctx3.new_page()
                    adapter3 = WebAdapter(page3, ctx3)
                    with RunLogger(f"replay2-{run_id}", cap.id, evidence_dir / "replay2_log.jsonl") as rl3:
                        exec3 = ReplayExecutor(adapter3, POLICY, rl3, ExecutionMode.VALIDATION)
                        await exec3.setup(ctx3)
                        login_cap3 = Capability.model_validate_json(
                            (FIXTURES / "login.json").read_text()
                        )
                        await exec3.run(login_cap3, {"username": "operator", "password": "demo123"})
                        r3 = await exec3.run(replay_cap, {"member_id": "10002"})
                    await ctx3.close()

                    if r3.status == ReplayStatus.SUCCESS:
                        _pass("Template test: member 10002 replay succeeded")
                    else:
                        _fail("Template test 10002", f"{r3.status}: {r3.failure}")
                        failures += 1

                    if r3.outputs and r3.outputs.get("savings_balance") == "$5,678.90":
                        _pass(f"Template test: 10002 savings_balance = "
                              f"{r3.outputs['savings_balance']!r}")
                    else:
                        _fail("Template test: 10002 savings_balance",
                              f"got {r3.outputs}")
                        failures += 1

            await ctx2.close()

        await browser.close()

    proc.terminate()

    # Save a copy of the produced artifact alongside the hand-written fixtures for diffing
    if result.success and result.capability_path:
        dest = FIXTURES / "member_lookup_balance_discovery.json"
        dest.write_text(result.capability_path.read_text())
        print(f"\n  Artifact saved → {dest}")
        print(f"  Evidence       → {evidence_dir}/")

    print()
    if failures == 0:
        print("All tests passed.")
    else:
        print(f"{failures} test(s) FAILED.")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
