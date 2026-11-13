#!/usr/bin/env python3
"""
Generate the five named evidence folders for the submission.

evidence/
  discovery_run/     LLM drive: 7 calls, 6 steps, artifact produced, screenshots
  replay_success/    Zero llm_call events in JSONL, SUCCESS, correct balances
  replay_not_found/  BUSINESS_OUTCOME: MEMBER_NOT_FOUND
  policy_block/      HARD_FAILURE: POLICY_DENIED
  human_handoff/     escalation_start → escalation_end (done) → SUCCESS, screenshots

Requires ANTHROPIC_API_KEY (for discovery_run only).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

from src.escalation.operator_channel import OperatorChannel
from src.models.artifact import (
    Capability, CheckpointType, OutcomeDetector, Output, Parameter, Sensitivity,
)
from src.models.policy import ActionPolicy, AllowlistPolicy, ExecutionMode
from src.models.result import FailureKind, ReplayStatus
from src.observability.logger import RunLogger
from src.replay.executor import ReplayExecutor
from src.surface.web import WebAdapter

FIXTURES      = Path("artifacts/fixtures")
EVIDENCE      = Path("evidence")
FIXTURE_CAP   = FIXTURES / "member_lookup_balance_from_dashboard.json"
DISCOVERED_CAP = EVIDENCE / "discovery_run" / "artifact.json"

POLICY = AllowlistPolicy(
    allowed_domains=["localhost:8080"],
    allowed_routes=[
        "/", "/login", "/dashboard", "/member/search",
        "/account", "/account-frame", "/interstitial",
        "/supervisor/authorize",
    ],
    blocked_routes=[],
    action=ActionPolicy(),
)

PARAM_SPECS = [
    Parameter(name="member_id", type="string", required=True,
              pattern=r"^\d{5}$", sensitivity=Sensitivity.INTERNAL),
]
OUTPUT_SPECS = [
    Output(name="savings_balance",  type="string", sensitivity=Sensitivity.FINANCIAL),
    Output(name="checking_balance", type="string", sensitivity=Sensitivity.FINANCIAL),
]
OUTCOME_DETECTORS = [
    OutcomeDetector(code="SESSION_EXPIRED", detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
                    value="Your session has expired", disposition="HARD_FAILURE", priority=2),
    OutcomeDetector(code="APP_ERROR", detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
                    value="An unexpected server error occurred", disposition="HARD_FAILURE", priority=3),
    OutcomeDetector(code="MEMBER_NOT_FOUND", detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
                    value="No member found with ID", disposition="BUSINESS_OUTCOME", priority=10),
    OutcomeDetector(code="ACCOUNT_FROZEN", detector_type=CheckpointType.PAGE_CONTAINS_TEXT,
                    value="Frozen", disposition="BUSINESS_OUTCOME", priority=10),
]

LOGIN_CAP    = FIXTURES / "login.json"
LOGIN_PARAMS = {"username": "operator", "password": "demo123"}


def _ok(label: str)   -> None: print(f"  OK    {label}")
def _fail(label: str, detail: str) -> None: print(f"  FAIL  {label}: {detail}")


async def _login(executor: ReplayExecutor) -> bool:
    cap = Capability.model_validate_json(LOGIN_CAP.read_text())
    r = await executor.run(cap, LOGIN_PARAMS)
    return r.status == ReplayStatus.SUCCESS


# ---------------------------------------------------------------------------
# 1. replay_success
# ---------------------------------------------------------------------------

async def run_replay_success(browser, lookup_cap_path: Path) -> int:
    dest = EVIDENCE / "replay_success"
    dest.mkdir(parents=True, exist_ok=True)
    log = dest / "run_log.jsonl"

    ctx  = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx,
                         known_dialog_patterns=POLICY.action.known_dialog_patterns)

    failures = 0
    with RunLogger("replay-success", "member.lookup_balance", log) as logger:
        cap = Capability.model_validate_json(lookup_cap_path.read_text())
        executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION,
                                  evidence_dir=dest)
        await executor.setup(ctx)
        if not await _login(executor):
            _fail("replay_success login", "login failed"); failures += 1
        else:
            r = await executor.run(cap, {"member_id": "10001"})
            if r.status == ReplayStatus.SUCCESS:
                _ok(f"replay_success: SUCCESS, savings={r.outputs.get('savings_balance')}")
            else:
                _fail("replay_success", f"{r.status}: {r.failure}"); failures += 1

            raw = log.read_text()
            if "10001" not in raw and "operator" not in raw:
                _ok("replay_success: sensitive params scrubbed")
            else:
                _fail("replay_success scrubbing", "params leaked"); failures += 1

            if '"event": "llm_call"' not in raw:
                _ok("replay_success: zero LLM calls proven from log")
            else:
                _fail("replay_success", "llm_call event found"); failures += 1

    await ctx.close()
    return failures


# ---------------------------------------------------------------------------
# 2. replay_not_found
# ---------------------------------------------------------------------------

async def run_replay_not_found(browser, lookup_cap_path: Path) -> int:
    dest = EVIDENCE / "replay_not_found"
    dest.mkdir(parents=True, exist_ok=True)
    log = dest / "run_log.jsonl"

    ctx  = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx,
                         known_dialog_patterns=POLICY.action.known_dialog_patterns)

    failures = 0
    with RunLogger("replay-not-found", "member.lookup_balance", log) as logger:
        cap = Capability.model_validate_json(lookup_cap_path.read_text())
        executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION,
                                  evidence_dir=dest)
        await executor.setup(ctx)
        if not await _login(executor):
            _fail("replay_not_found login", "login failed"); failures += 1
        else:
            r = await executor.run(cap, {"member_id": "99999"})
            if r.status == ReplayStatus.BUSINESS_OUTCOME and r.business_outcome.code == "MEMBER_NOT_FOUND":
                _ok(f"replay_not_found: BUSINESS_OUTCOME MEMBER_NOT_FOUND")
            else:
                _fail("replay_not_found", f"{r.status} {r.business_outcome}"); failures += 1

    await ctx.close()
    return failures


# ---------------------------------------------------------------------------
# 3. policy_block
# ---------------------------------------------------------------------------

async def run_policy_block(browser, lookup_cap_path: Path) -> int:
    dest = EVIDENCE / "policy_block"
    dest.mkdir(parents=True, exist_ok=True)
    log = dest / "run_log.jsonl"

    failures = 0
    # Inject a blocked navigate as the first step
    base = Capability.model_validate_json(lookup_cap_path.read_text())
    blocked_step = base.steps[0].model_copy(update={"value": "http://evil-bank.com/steal"})
    blocked_cap = base.model_copy(update={"steps": [blocked_step]})

    ctx  = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx,
                         known_dialog_patterns=POLICY.action.known_dialog_patterns)

    with RunLogger("policy-block", "member.lookup_balance", log) as logger:
        executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION,
                                  evidence_dir=dest)
        await executor.setup(ctx)
        # Login in this context first (blocked step is navigate, not login)
        if not await _login(executor):
            _fail("policy_block login", "login failed"); failures += 1
        else:
            r = await executor.run(blocked_cap, {"member_id": "10001"})
            if r.status == ReplayStatus.FAILED and r.failure.kind == FailureKind.POLICY_DENIED:
                _ok(f"policy_block: FAILED POLICY_DENIED")
            else:
                _fail("policy_block", f"{r.status} {r.failure}"); failures += 1

    await ctx.close()
    return failures


# ---------------------------------------------------------------------------
# 4. human_handoff
# ---------------------------------------------------------------------------

async def run_human_handoff(browser, lookup_cap_path: Path) -> int:
    dest = EVIDENCE / "human_handoff"
    dest.mkdir(parents=True, exist_ok=True)
    log = dest / "run_log.jsonl"

    failures = 0
    ctx  = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx,
                         known_dialog_patterns=POLICY.action.known_dialog_patterns)

    with RunLogger("human-handoff", "member.lookup_balance", log) as logger:
        async def signal_provider() -> str:
            try:
                await page.locator("input[name='supervisor_code']").fill("SUPER-DEMO")
                await page.locator("input[name='auth_reason']").fill("Account balance inquiry")
                await page.locator("input[type='submit']").click()
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            return "D"

        channel = OperatorChannel(
            evidence_dir=dest,
            logger=logger,
            lease_seconds=30,
            signal_provider=signal_provider,
        )

        cap = Capability.model_validate_json(lookup_cap_path.read_text())
        executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION,
                                  operator_channel=channel,
                                  evidence_dir=dest)
        await executor.setup(ctx)
        if not await _login(executor):
            _fail("human_handoff login", "login failed"); failures += 1
        else:
            r = await executor.run(cap, {"member_id": "99998"})
            if r.status == ReplayStatus.SUCCESS:
                _ok(f"human_handoff: SUCCESS after escalation, "
                    f"savings={r.outputs.get('savings_balance')}")
            else:
                _fail("human_handoff", f"{r.status}: {r.failure}"); failures += 1

            if r.intervention and r.intervention.signal == "done":
                _ok("human_handoff: InterventionRecord signal=done")
            else:
                _fail("human_handoff", f"intervention={r.intervention}"); failures += 1

            raw = log.read_text()
            if '"event": "escalation_start"' in raw and '"event": "escalation_end"' in raw:
                _ok("human_handoff: escalation_start + escalation_end in log")
            else:
                _fail("human_handoff log events", "missing"); failures += 1

    await ctx.close()
    return failures


# ---------------------------------------------------------------------------
# 5. discovery_run
# ---------------------------------------------------------------------------

async def run_discovery(browser) -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("  SKIP  discovery_run (ANTHROPIC_API_KEY not set)")
        return 0

    dest = EVIDENCE / "discovery_run"
    dest.mkdir(parents=True, exist_ok=True)
    log = dest / "run_log.jsonl"

    failures = 0
    from src.agent.loop import DiscoveryLoop
    from src.agent.llm import LLMClient

    # Login in one context, copy cookies to discovery context
    ctx  = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx,
                         known_dialog_patterns=POLICY.action.known_dialog_patterns)

    with RunLogger("discovery-evidence", "member.lookup_balance", log) as logger:
        login_cap = Capability.model_validate_json(LOGIN_CAP.read_text())
        exec_login = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION,
                                    evidence_dir=dest)
        await exec_login.setup(ctx)
        login_r = await exec_login.run(login_cap, LOGIN_PARAMS)
        if login_r.status != ReplayStatus.SUCCESS:
            _fail("discovery_run login", str(login_r.status)); failures += 1
            await ctx.close()
            return failures

        session_cookies = await ctx.cookies()
        ctx_disc = await browser.new_context()
        await ctx_disc.add_cookies(session_cookies)
        page_disc = await ctx_disc.new_page()
        adapter_disc = WebAdapter(page_disc, ctx_disc)

        loop = DiscoveryLoop(
            adapter=adapter_disc,
            policy=POLICY,
            logger=logger,
            max_steps=25,
            wall_clock_seconds=300,
            evidence_dir=dest,
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

    if result.success and result.capability_path:
        # Copy produced artifact to evidence/discovery_run/artifact.json
        shutil.copy(result.capability_path, dest / "artifact.json")
        _ok(f"discovery_run: SUCCESS, {result.llm_calls} LLM calls, "
            f"{result.steps_recorded} steps, {result.duration_seconds:.1f}s")
        _ok(f"discovery_run: {len(result.screenshot_paths)} screenshots in evidence/discovery_run/screenshots/")
    else:
        _fail("discovery_run", result.failure_reason or "unknown"); failures += 1

    await ctx.close()
    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main() -> int:
    proc = subprocess.Popen(
        ["python3", "target_app/server.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)

    total_failures = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Discovery runs first so the produced artifact can be used for all replay scenarios
        print("\n=== 1/5  discovery_run ===")
        total_failures += await run_discovery(browser)

        # Use the discovery-produced artifact if available; fall back to hand-written fixture
        lookup_cap_path = DISCOVERED_CAP if DISCOVERED_CAP.exists() else FIXTURE_CAP
        if lookup_cap_path == FIXTURE_CAP:
            print(f"  NOTE  Using fixture (discovery skipped or failed)")

        print("\n=== 2/5  replay_success ===")
        total_failures += await run_replay_success(browser, lookup_cap_path)

        print("\n=== 3/5  replay_not_found ===")
        total_failures += await run_replay_not_found(browser, lookup_cap_path)

        print("\n=== 4/5  policy_block ===")
        total_failures += await run_policy_block(browser, lookup_cap_path)

        print("\n=== 5/5  human_handoff ===")
        total_failures += await run_human_handoff(browser, lookup_cap_path)

        await browser.close()

    proc.terminate()

    print("\n" + "=" * 50)
    print(f"Evidence written to evidence/")
    for d in sorted(EVIDENCE.iterdir()):
        if d.is_dir() and d.name in (
            "replay_success", "replay_not_found", "policy_block",
            "human_handoff", "discovery_run"
        ):
            files = list(d.rglob("*"))
            print(f"  {d.name}/  ({len(files)} file(s))")

    print()
    if total_failures == 0:
        print("All evidence runs passed.")
    else:
        print(f"{total_failures} failure(s).")
    return total_failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
