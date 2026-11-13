#!/usr/bin/env python3
"""
Step 6 smoke test: hand-written artifacts replayed in validation mode.

Cases:
  1.  Happy path   member 10001 → SUCCESS, correct balances
  2.  Happy path 2 member 10002 → SUCCESS, different balances
  3.  Not found    member 99999 → BUSINESS_OUTCOME: MEMBER_NOT_FOUND
  4.  Frozen       member 99997 → BUSINESS_OUTCOME: ACCOUNT_FROZEN
  5.  Policy block navigate to evil.com injected → HARD_FAILURE: POLICY_DENIED
  6.  Slow-load    member 99991 (1.2s delay) → SUCCESS, polling loop survives
  7.  APP_ERROR    member 99993 (HTTP 500)   → HARD_FAILURE: APP_ERROR
  8.  Chained run  login.json → member_lookup_balance_from_dashboard.json → SUCCESS
  9.  Import proof executor imports no anthropic / src.agent deps
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

from src.models.artifact import Capability
from src.models.policy import AllowlistPolicy, ActionPolicy, ExecutionMode
from src.models.result import FailureKind, ReplayStatus
from src.observability.logger import RunLogger
from src.replay.executor import ReplayExecutor
from src.surface.web import WebAdapter

# Fixtures: hand-written artifacts that validate the schema and executor
FIXTURES_DIR = Path("artifacts/fixtures")
ARTIFACT_PATH = FIXTURES_DIR / "member_lookup_balance_with_login.json"
LOGIN_ARTIFACT_PATH = FIXTURES_DIR / "login.json"
DASHBOARD_LOOKUP_PATH = FIXTURES_DIR / "member_lookup_balance_from_dashboard.json"

SERVER_URL = "http://localhost:8080"

POLICY = AllowlistPolicy(
    allowed_domains=["localhost:8080"],
    allowed_routes=[
        "/", "/login", "/dashboard", "/member/search",
        "/account", "/account-frame", "/interstitial",
    ],
    blocked_routes=[],
    action=ActionPolicy(),
)


def _pass(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


async def run_case(label: str, member_id: str, browser,
                   artifact_path: Path = None) -> dict:
    path = artifact_path or ARTIFACT_PATH
    ctx = await browser.new_context()
    page = await ctx.new_page()
    adapter = WebAdapter(page, ctx)

    run_id = f"test-{member_id}-{int(time.time())}"
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / run_id / "run_log.jsonl"
        with RunLogger(run_id, "member.lookup_balance", log_path) as logger:
            capability = Capability.model_validate_json(path.read_text())
            executor = ReplayExecutor(adapter, POLICY, logger, ExecutionMode.VALIDATION)
            await executor.setup(ctx)
            result = await executor.run(capability, {
                "member_id": member_id,
                "username": "operator",
                "password": "demo123",
            })

        log_lines = log_path.read_text().splitlines() if log_path.exists() else []
        raw_log = log_path.read_text() if log_path.exists() else ""

    await ctx.close()
    return {"result": result, "log_lines": log_lines, "raw_log": raw_log}


async def main() -> int:
    capability = Capability.model_validate_json(ARTIFACT_PATH.read_text())
    failures = 0

    # Quick schema check before starting the browser
    if capability.can_run_in_validation_mode():
        _pass("Artifact schema: DRAFT + can_run_in_validation_mode")
    else:
        _fail("Artifact schema", "can_run_in_validation_mode() returned False")
        failures += 1

    # ------------------------------------------------------------------ Case 9: import proof (no browser needed)
    print("\n--- Case 9: import proof (no anthropic / src.agent deps) ---")
    import_check = subprocess.run(
        [
            "python3", "-c",
            "import sys; "
            "from src.replay.executor import ReplayExecutor; "
            "mods = set(sys.modules.keys()); "
            "leaked = [m for m in mods if m == 'anthropic' or m.startswith('src.agent')]; "
            "assert not leaked, f'leaked: {leaked}'"
        ],
        capture_output=True, text=True,
    )
    if import_check.returncode == 0:
        _pass("ReplayExecutor imports cleanly: no anthropic or src.agent in sys.modules")
    else:
        _fail("import proof", import_check.stderr.strip() or import_check.stdout.strip())
        failures += 1

    proc = subprocess.Popen(
        ["python3", "target_app/server.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ------------------------------------------------------------------ Case 1: happy path 10001
        print("\n--- Case 1: happy path member 10001 ---")
        d = await run_case("10001", "10001", browser)
        r = d["result"]
        raw = d["raw_log"]

        if r.status == ReplayStatus.SUCCESS:
            _pass(f"status = SUCCESS")
        else:
            _fail("status", f"expected SUCCESS got {r.status} | {r.failure}")
            failures += 1

        if r.outputs and r.outputs.get("savings_balance") == "$1,234.56":
            _pass(f"savings_balance = {r.outputs['savings_balance']!r}")
        else:
            _fail("savings_balance", f"got {r.outputs}")
            failures += 1

        if r.outputs and r.outputs.get("checking_balance") == "$456.78":
            _pass(f"checking_balance = {r.outputs['checking_balance']!r}")
        else:
            _fail("checking_balance", f"got {r.outputs}")
            failures += 1

        if r.llm_calls == 0:
            _pass("llm_calls = 0")
        else:
            _fail("llm_calls", f"expected 0 got {r.llm_calls}")
            failures += 1

        # Sensitive values must not appear in log file
        if "10001" not in raw and "operator" not in raw and "demo123" not in raw:
            _pass("Log: sensitive params scrubbed from JSONL")
        else:
            leaks = [v for v in ["10001", "operator", "demo123"] if v in raw]
            _fail("Log: sensitive params NOT scrubbed", f"leaked: {leaks}")
            failures += 1

        if "$1,234.56" not in raw:
            _pass("Log: financial output scrubbed from JSONL")
        else:
            _fail("Log: financial output NOT scrubbed", "raw value in log")
            failures += 1

        # Zero LLM calls provable from log (no llm_call events)
        if not any('"event": "llm_call"' in line for line in d["log_lines"]):
            _pass("Log: no llm_call events (provably zero LLM calls)")
        else:
            _fail("Log: unexpected llm_call event", "")
            failures += 1

        # ------------------------------------------------------------------ Case 2: happy path 10002
        print("\n--- Case 2: happy path member 10002 ---")
        d2 = await run_case("10002", "10002", browser)
        r2 = d2["result"]

        if r2.status == ReplayStatus.SUCCESS:
            _pass(f"status = SUCCESS")
        else:
            _fail("status", f"expected SUCCESS got {r2.status}")
            failures += 1

        if r2.outputs and r2.outputs.get("savings_balance") == "$5,678.90":
            _pass(f"savings_balance = {r2.outputs['savings_balance']!r}")
        else:
            _fail("savings_balance for 10002", f"got {r2.outputs}")
            failures += 1

        # ------------------------------------------------------------------ Case 3: member not found
        print("\n--- Case 3: MEMBER_NOT_FOUND (member 99999) ---")
        d3 = await run_case("99999", "99999", browser)
        r3 = d3["result"]

        if r3.status == ReplayStatus.BUSINESS_OUTCOME:
            _pass("status = BUSINESS_OUTCOME")
        else:
            _fail("status", f"expected BUSINESS_OUTCOME got {r3.status}")
            failures += 1

        if r3.business_outcome and r3.business_outcome.code == "MEMBER_NOT_FOUND":
            _pass(f"business_outcome.code = MEMBER_NOT_FOUND")
        else:
            _fail("business_outcome.code", f"got {r3.business_outcome}")
            failures += 1

        if not any('"event": "llm_call"' in line for line in d3["log_lines"]):
            _pass("Log: zero LLM calls on BUSINESS_OUTCOME path")
        else:
            _fail("Log: unexpected llm_call in BUSINESS_OUTCOME path", "")
            failures += 1

        # ------------------------------------------------------------------ Case 4: account frozen
        print("\n--- Case 4: ACCOUNT_FROZEN (member 99997) ---")
        d4 = await run_case("99997", "99997", browser)
        r4 = d4["result"]

        if r4.status == ReplayStatus.BUSINESS_OUTCOME:
            _pass("status = BUSINESS_OUTCOME")
        else:
            _fail("status", f"expected BUSINESS_OUTCOME got {r4.status}")
            failures += 1

        if r4.business_outcome and r4.business_outcome.code == "ACCOUNT_FROZEN":
            _pass(f"business_outcome.code = ACCOUNT_FROZEN")
        else:
            _fail("business_outcome.code", f"got {r4.business_outcome}")
            failures += 1

        # ------------------------------------------------------------------ Case 5: policy block
        print("\n--- Case 5: POLICY_DENIED (blocked navigate) ---")
        cap_blocked = Capability.model_validate_json(ARTIFACT_PATH.read_text())
        cap_blocked = cap_blocked.model_copy(update={
            "steps": [cap_blocked.steps[0].model_copy(update={
                "value": "http://evil-bank.com/steal"
            })]
        })

        ctx5 = await browser.new_context()
        page5 = await ctx5.new_page()
        adapter5 = WebAdapter(page5, ctx5)

        with tempfile.TemporaryDirectory() as tmpdir5:
            log5 = Path(tmpdir5) / "log.jsonl"
            with RunLogger("run-policy-block", "member.lookup_balance", log5) as logger5:
                executor5 = ReplayExecutor(adapter5, POLICY, logger5, ExecutionMode.VALIDATION)
                await executor5.setup(ctx5)
                result5 = await executor5.run(cap_blocked, {
                    "member_id": "10001", "username": "operator", "password": "demo123",
                })
        await ctx5.close()

        if result5.status == ReplayStatus.FAILED:
            _pass("status = FAILED")
        else:
            _fail("status", f"expected FAILED got {result5.status}")
            failures += 1

        if result5.failure and result5.failure.kind == FailureKind.POLICY_DENIED:
            _pass(f"failure.kind = POLICY_DENIED")
        else:
            _fail("failure.kind", f"expected POLICY_DENIED got {result5.failure}")
            failures += 1

        # ------------------------------------------------------------------ Case 6: slow-load (99991)
        print("\n--- Case 6: slow-load member 99991 (1.2s server delay) ---")
        d6 = await run_case("99991", "99991", browser)
        r6 = d6["result"]

        if r6.status == ReplayStatus.SUCCESS:
            _pass("status = SUCCESS (polling loop handled slow page)")
        else:
            _fail("status", f"expected SUCCESS got {r6.status} | {r6.failure}")
            failures += 1

        if r6.outputs and r6.outputs.get("savings_balance") == "$2,345.67":
            _pass(f"savings_balance = {r6.outputs['savings_balance']!r}")
        else:
            _fail("savings_balance for 99991", f"got {r6.outputs}")
            failures += 1

        # ------------------------------------------------------------------ Case 7: APP_ERROR (99993)
        print("\n--- Case 7: APP_ERROR (member 99993, HTTP 500) ---")
        d7 = await run_case("99993", "99993", browser)
        r7 = d7["result"]

        if r7.status == ReplayStatus.FAILED:
            _pass("status = FAILED")
        else:
            _fail("status", f"expected FAILED got {r7.status}")
            failures += 1

        if r7.failure and r7.failure.kind == FailureKind.APP_ERROR:
            _pass(f"failure.kind = APP_ERROR")
        else:
            _fail("failure.kind", f"expected APP_ERROR got {r7.failure}")
            failures += 1

        # ------------------------------------------------------------------ Case 8: chained login + lookup
        print("\n--- Case 8: chained login.json → member_lookup_balance_from_dashboard.json ---")
        ctx8 = await browser.new_context()
        page8 = await ctx8.new_page()
        adapter8 = WebAdapter(page8, ctx8)

        with tempfile.TemporaryDirectory() as tmpdir8:
            log8 = Path(tmpdir8) / "chained.jsonl"
            with RunLogger("run-chained", "member.login+lookup", log8) as logger8:
                login_cap = Capability.model_validate_json(LOGIN_ARTIFACT_PATH.read_text())
                lookup_cap = Capability.model_validate_json(DASHBOARD_LOOKUP_PATH.read_text())

                executor8 = ReplayExecutor(adapter8, POLICY, logger8, ExecutionMode.VALIDATION)
                await executor8.setup(ctx8)

                # Step A: login
                login_result = await executor8.run(login_cap, {
                    "username": "operator",
                    "password": "demo123",
                })

                # Step B: lookup (reuses same adapter; browser is now on /dashboard)
                lookup_result = await executor8.run(lookup_cap, {
                    "member_id": "10001",
                })

        await ctx8.close()

        if login_result.status == ReplayStatus.SUCCESS:
            _pass("login capability status = SUCCESS")
        else:
            _fail("login status", f"expected SUCCESS got {login_result.status} | {login_result.failure}")
            failures += 1

        if lookup_result.status == ReplayStatus.SUCCESS:
            _pass("lookup capability status = SUCCESS")
        else:
            _fail("lookup status", f"expected SUCCESS got {lookup_result.status} | {lookup_result.failure}")
            failures += 1

        if lookup_result.outputs and lookup_result.outputs.get("savings_balance") == "$1,234.56":
            _pass(f"chained savings_balance = {lookup_result.outputs['savings_balance']!r}")
        else:
            _fail("chained savings_balance", f"got {lookup_result.outputs}")
            failures += 1

        await browser.close()

    proc.terminate()

    print()
    if failures == 0:
        print("All tests passed.")
    else:
        print(f"{failures} test(s) FAILED.")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
