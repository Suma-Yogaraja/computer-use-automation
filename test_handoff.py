#!/usr/bin/env python3
"""
Step 8 acceptance test: OperatorChannel human handoff.

Scenario: member 99998 → supervisor verification interstitial → NEEDS_HUMAN.
The OperatorChannel is invoked; a programmatic signal_provider simulates a human
who fills the supervisor form and signals "done". Automation resumes and extracts
the member's balances.

Pass:
  1. Replay reaches NEEDS_HUMAN (not FAILED before it) and escalates
  2. OperatorChannel prints intervention prompt and logs ESCALATION_START
  3. Human signal "done" is accepted
  4. Post-handoff postconditions verified (URL contains /member/search)
  5. Replay resumes and succeeds: status=SUCCESS, correct balances
  6. InterventionRecord logged with signal="done", postconditions_verified=True
  7. ESCALATION_END event present in log
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

from src.escalation.operator_channel import OperatorChannel
from src.models.artifact import Capability
from src.models.policy import AllowlistPolicy, ActionPolicy, ExecutionMode
from src.models.result import ReplayStatus
from src.observability.logger import RunLogger
from src.replay.executor import ReplayExecutor
from src.surface.web import WebAdapter

FIXTURES = Path("artifacts/fixtures")
EVIDENCE_DIR = Path("evidence")

# /supervisor/authorize added so the human can submit the supervisor form.
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


def _pass(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


async def main() -> int:
    failures = 0

    proc = subprocess.Popen(
        ["python3", "target_app/server.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)

    run_id = f"handoff-{int(time.time())}"
    evidence_dir = EVIDENCE_DIR / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        adapter = WebAdapter(page, ctx)

        log_path = evidence_dir / "run_log.jsonl"
        with RunLogger(run_id, "member.lookup_balance", log_path) as logger:

            # ---------------------------------------------------------------- Build signal_provider
            # Simulate the human: fill the supervisor form and submit.
            # RouteGuard is in human_mode during handoff, so the POST to /supervisor/authorize
            # goes through. Server redirects to GET /member/search showing the results.
            async def signal_provider() -> str:
                try:
                    sup_code = page.locator("input[name='supervisor_code']")
                    auth_reason = page.locator("input[name='auth_reason']")
                    submit = page.locator("input[type='submit']")
                    await sup_code.fill("SUPER-DEMO")
                    await auth_reason.fill("Account balance inquiry")
                    await submit.click()
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception as exc:
                    print(f"  [signal_provider error: {exc}]")
                return "D"

            channel = OperatorChannel(
                evidence_dir=evidence_dir,
                logger=logger,
                lease_seconds=30,
                signal_provider=signal_provider,
            )

            # ---------------------------------------------------------------- Login
            print("\n--- Login ---")
            login_cap = Capability.model_validate_json(
                (FIXTURES / "login.json").read_text()
            )
            executor = ReplayExecutor(
                adapter, POLICY, logger, ExecutionMode.VALIDATION,
                operator_channel=channel,
            )
            await executor.setup(ctx)
            login_result = await executor.run(
                login_cap, {"username": "operator", "password": "demo123"}
            )

            if login_result.status == ReplayStatus.SUCCESS:
                _pass("Login succeeded")
            else:
                _fail("Login", f"{login_result.status}: {login_result.failure}")
                failures += 1
                await browser.close()
                proc.terminate()
                return failures

            # ---------------------------------------------------------------- Lookup member 99998
            print("\n--- Lookup member 99998 (interstitial → handoff → resume) ---")
            lookup_cap = Capability.model_validate_json(
                (FIXTURES / "member_lookup_balance_from_dashboard.json").read_text()
            )
            result = await executor.run(lookup_cap, {"member_id": "99998"})

        # ---------------------------------------------------------------- Assertions
        print(f"\n  Result: {result.status}, outputs: {result.outputs}")

        if result.status == ReplayStatus.SUCCESS:
            _pass(f"status = SUCCESS (handoff + resume worked)")
        else:
            _fail("status", f"expected SUCCESS, got {result.status}: {result.failure}")
            failures += 1

        if result.outputs and result.outputs.get("savings_balance") == "$9,999.00":
            _pass(f"savings_balance = {result.outputs['savings_balance']!r}")
        else:
            _fail("savings_balance", f"got {result.outputs}")
            failures += 1

        if result.outputs and result.outputs.get("checking_balance") == "$888.00":
            _pass(f"checking_balance = {result.outputs['checking_balance']!r}")
        else:
            _fail("checking_balance", f"got {result.outputs}")
            failures += 1

        if result.intervention:
            _pass("InterventionRecord attached to result")
            if result.intervention.signal == "done":
                _pass("InterventionRecord.signal = 'done'")
            else:
                _fail("InterventionRecord.signal", f"got {result.intervention.signal!r}")
                failures += 1
            if result.intervention.postconditions_verified:
                _pass("InterventionRecord.postconditions_verified = True")
            else:
                _fail("InterventionRecord.postconditions_verified", "False")
                failures += 1
        else:
            _fail("InterventionRecord", "not attached to result")
            failures += 1

        # Verify log events
        raw_log = log_path.read_text() if log_path.exists() else ""
        if '"event": "escalation_start"' in raw_log:
            _pass("Log: escalation_start event present")
        else:
            _fail("Log: escalation_start", "event not found")
            failures += 1

        if '"event": "escalation_end"' in raw_log:
            _pass("Log: escalation_end event present")
        else:
            _fail("Log: escalation_end", "event not found")
            failures += 1

        if '"signal": "done"' in raw_log:
            _pass("Log: signal=done recorded")
        else:
            _fail("Log: signal", "done not found in log")
            failures += 1

        await browser.close()

    proc.terminate()

    print(f"\n  Evidence → {evidence_dir}/")
    print()
    if failures == 0:
        print("All tests passed.")
    else:
        print(f"{failures} test(s) FAILED.")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
