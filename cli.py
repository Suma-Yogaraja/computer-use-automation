#!/usr/bin/env python3
"""
cli.py — Computer-Use Automation System

Subcommands:
  discover   Run the LLM discovery agent to record a replayable capability artifact
  replay     Replay a capability artifact (zero LLM calls)
  approve    Promote a VALIDATED artifact to APPROVED
  list       List capability artifacts
  schema     Print the LLM tool schemas used by the discovery agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_kvs(kvs: Optional[List[str]]) -> Dict[str, str]:
    """Parse ['key=value', ...] into {key: value}."""
    result: Dict[str, str] = {}
    for kv in (kvs or []):
        if "=" not in kv:
            _die(f"Expected key=value, got {kv!r}")
        k, _, v = kv.partition("=")
        result[k.strip()] = v.strip()
    return result


def _load_policy(policy_path: str):
    from src.models.policy import AllowlistPolicy
    return AllowlistPolicy.model_validate_json(Path(policy_path).read_text())


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _run_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

async def _cmd_discover_async(args) -> int:
    if not os.getenv("ANTHROPIC_API_KEY"):
        _die("ANTHROPIC_API_KEY is not set — discovery requires an Anthropic API key")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        _die(f"Spec file not found: {spec_path}")

    spec = json.loads(spec_path.read_text())

    capability_id: str = spec["capability_id"]
    capability_name: str = spec["capability_name"]
    goal: str = spec["goal"]
    target_url: str = args.target or spec["target_url"]

    from src.models.artifact import OutcomeDetector, Output, Parameter
    param_specs = [Parameter.model_validate(p) for p in spec.get("parameters", [])]
    output_specs = [Output.model_validate(o) for o in spec.get("outputs", [])]
    outcome_detectors = [OutcomeDetector.model_validate(d) for d in spec.get("outcome_detectors", [])]

    params = _parse_kvs(args.param)
    for ps in param_specs:
        if ps.required and ps.name not in params:
            _die(f"Required parameter --param {ps.name}=<value> not provided")

    login_params = _parse_kvs(args.login_param)

    policy = _load_policy(args.policy)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = _run_id("discover")
    evidence_dir = Path(args.evidence_dir) / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    param_summary = ", ".join(f"{k}={v!r}" for k, v in params.items())
    print(f"\nDiscovering {capability_id!r}")
    print(f"  Spec:       {spec_path}")
    print(f"  Target:     {target_url}")
    print(f"  Params:     {param_summary}")
    print(f"  Policy:     {args.policy} ({len(policy.allowed_domains)} domain(s), "
          f"{len(policy.allowed_routes)} route(s))")
    print(f"  Model:      {args.model or os.getenv('DISCOVERY_MODEL', 'claude-sonnet-4-6')}")
    print(f"  Max steps:  {args.max_steps}  Wall clock: {args.wall_clock}s")
    print(f"  Evidence:   {evidence_dir}/")

    from playwright.async_api import async_playwright
    from src.agent.llm import LLMClient
    from src.agent.loop import DiscoveryLoop
    from src.models.policy import ExecutionMode
    from src.models.result import ReplayStatus
    from src.observability.logger import RunLogger
    from src.replay.executor import ReplayExecutor
    from src.surface.web import WebAdapter

    log_path = evidence_dir / "run_log.jsonl"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)

        with RunLogger(run_id, capability_id, log_path) as logger:
            # Optional login pre-condition
            if args.login:
                print("\n  Login pre-condition...")
                from src.models.artifact import Capability
                login_cap = Capability.model_validate_json(Path(args.login).read_text())
                ctx = await browser.new_context()
                page = await ctx.new_page()
                adapter = WebAdapter(page, ctx,
                                     known_dialog_patterns=policy.action.known_dialog_patterns)
                executor = ReplayExecutor(adapter, policy, logger, ExecutionMode.VALIDATION)
                await executor.setup(ctx)
                login_result = await executor.run(login_cap, login_params)
                if login_result.status != ReplayStatus.SUCCESS:
                    print(f"  Login FAILED: {login_result.status} — {login_result.failure}")
                    await browser.close()
                    return 1
                print(f"  Login OK")

                # Copy session cookies into a fresh context for discovery
                session_cookies = await ctx.cookies()
                ctx_disc = await browser.new_context()
                await ctx_disc.add_cookies(session_cookies)
                page_disc = await ctx_disc.new_page()
                adapter_disc = WebAdapter(page_disc, ctx_disc,
                                          known_dialog_patterns=policy.action.known_dialog_patterns)
            else:
                ctx_disc = await browser.new_context()
                page_disc = await ctx_disc.new_page()
                adapter_disc = WebAdapter(page_disc, ctx_disc,
                                          known_dialog_patterns=policy.action.known_dialog_patterns)

            print(f"\n  Running discovery...")
            llm = LLMClient(model=args.model) if args.model else None
            loop = DiscoveryLoop(
                adapter=adapter_disc,
                policy=policy,
                logger=logger,
                llm=llm,
                max_steps=args.max_steps,
                wall_clock_seconds=args.wall_clock,
                evidence_dir=evidence_dir,
            )
            result = await loop.run(
                capability_id=capability_id,
                capability_name=capability_name,
                goal=goal,
                target_url=target_url,
                params=params,
                param_specs=param_specs,
                output_specs=output_specs,
                outcome_detectors=outcome_detectors,
            )
            await ctx_disc.close()

        await browser.close()

    print(f"\n  LLM calls: {result.llm_calls}  "
          f"Steps: {result.steps_recorded}  "
          f"Duration: {result.duration_seconds:.1f}s")

    if not result.success:
        print(f"\n  FAILED: {result.failure_reason}")
        return 1

    # Save artifact to output_dir
    artifact_filename = f"{capability_id.replace('.', '_')}.{int(time.time())}.json"
    artifact_path = output_dir / artifact_filename
    if result.capability_path:
        artifact_path.write_text(result.capability_path.read_text())
    elif result.capability:
        artifact_path.write_text(result.capability.model_dump_json(indent=2))

    print(f"\n  SUCCESS")
    print(f"  Artifact:  {artifact_path}")
    print(f"  Evidence:  {evidence_dir}/")
    print(f"  ({len(result.screenshot_paths)} step screenshot(s) saved)")
    return 0


def cmd_discover(args) -> int:
    return asyncio.run(_cmd_discover_async(args))


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

async def _cmd_replay_async(args) -> int:
    artifact_path = Path(args.artifact)
    if not artifact_path.exists():
        _die(f"Artifact not found: {artifact_path}")

    from src.models.artifact import Capability
    from src.models.policy import ExecutionMode

    cap = Capability.model_validate_json(artifact_path.read_text())

    mode_map = {
        "validation": ExecutionMode.VALIDATION,
        "attended":   ExecutionMode.ATTENDED,
        "unattended": ExecutionMode.UNATTENDED,
    }
    mode = mode_map.get(args.mode, ExecutionMode.VALIDATION)

    # Lifecycle check before even starting the browser
    if mode == ExecutionMode.VALIDATION and not cap.can_run_in_validation_mode():
        _die(f"Artifact lifecycle {cap.lifecycle_status.value!r} cannot run in validation mode")
    if mode == ExecutionMode.ATTENDED and not cap.can_run_attended():
        _die(f"Artifact lifecycle {cap.lifecycle_status.value!r} requires VALIDATED/APPROVED for attended mode")
    if mode == ExecutionMode.UNATTENDED and not cap.can_run_unattended():
        _die(f"Artifact lifecycle {cap.lifecycle_status.value!r} requires APPROVED for unattended mode")

    params = _parse_kvs(args.param)
    login_params = _parse_kvs(args.login_param)
    policy = _load_policy(args.policy)

    run_id = _run_id("replay")
    evidence_dir = Path(args.evidence_dir) / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nReplaying {cap.id!r} v{cap.version} [{cap.lifecycle_status.value}/{args.mode}]")
    param_summary = ", ".join(f"{k}={v!r}" for k, v in params.items())
    print(f"  Params:    {param_summary}")
    print(f"  Artifact:  {artifact_path}")
    print(f"  Evidence:  {evidence_dir}/")

    from playwright.async_api import async_playwright
    from src.escalation.operator_channel import OperatorChannel
    from src.models.result import ReplayStatus
    from src.observability.logger import RunLogger
    from src.replay.executor import ReplayExecutor
    from src.surface.web import WebAdapter

    log_path = evidence_dir / "run_log.jsonl"
    channel: Optional[OperatorChannel] = None
    if args.operator_channel:
        with RunLogger(run_id, cap.id, log_path) as _logger_tmp:
            channel = OperatorChannel(
                evidence_dir=evidence_dir,
                logger=_logger_tmp,
                lease_seconds=policy.operator_lease_seconds,
            )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        adapter = WebAdapter(page, ctx,
                             known_dialog_patterns=policy.action.known_dialog_patterns)

        with RunLogger(run_id, cap.id, log_path) as logger:
            if args.operator_channel:
                channel = OperatorChannel(
                    evidence_dir=evidence_dir,
                    logger=logger,
                    lease_seconds=policy.operator_lease_seconds,
                )

            executor = ReplayExecutor(adapter, policy, logger, mode,
                                      operator_channel=channel,
                                      evidence_dir=evidence_dir)
            await executor.setup(ctx)

            # Optional login pre-condition
            if args.login:
                print("\n  Login pre-condition...")
                login_cap = Capability.model_validate_json(Path(args.login).read_text())
                login_result = await executor.run(login_cap, login_params)
                if login_result.status != ReplayStatus.SUCCESS:
                    print(f"  Login FAILED: {login_result.status} — {login_result.failure}")
                    await browser.close()
                    return 1
                print(f"  Login OK")

            result = await executor.run(cap, params)

        await browser.close()

    # Print result
    print(f"\n  Status:  {result.status.value}")
    if result.outputs:
        for k, v in result.outputs.items():
            print(f"  {k}: {v}")
    if result.business_outcome:
        print(f"  Outcome: {result.business_outcome.code} — {result.business_outcome.detail}")
    if result.failure:
        print(f"  Failure: [{result.failure.kind.value}] {result.failure.observed}")
    if result.intervention:
        print(f"  Handoff: signal={result.intervention.signal!r}, "
              f"postconditions_verified={result.intervention.postconditions_verified}")
    print(f"  Steps:   {result.steps_completed}/{result.total_steps}")
    print(f"  LLM calls: {result.llm_calls}")
    print(f"  Log:     {log_path}")

    if result.status == ReplayStatus.FAILED:
        return 1
    if result.status == ReplayStatus.ESCALATION_TIMEOUT:
        return 2
    return 0


def cmd_replay(args) -> int:
    return asyncio.run(_cmd_replay_async(args))


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def cmd_approve(args) -> int:
    artifact_path = Path(args.artifact)
    if not artifact_path.exists():
        _die(f"Artifact not found: {artifact_path}")

    from src.models.artifact import Capability, LifecycleStatus

    cap = Capability.model_validate_json(artifact_path.read_text())

    if cap.lifecycle_status == LifecycleStatus.APPROVED:
        print(f"{cap.id} v{cap.version} is already APPROVED.")
        return 0

    if cap.lifecycle_status != LifecycleStatus.VALIDATED:
        _die(
            f"Cannot approve: artifact is {cap.lifecycle_status.value!r}. "
            f"Only VALIDATED artifacts can be approved. "
            f"Run a validation-mode replay first to promote it."
        )

    updated = cap.model_copy(update={"lifecycle_status": LifecycleStatus.APPROVED})
    artifact_path.write_text(updated.model_dump_json(indent=2))
    print(f"Approved: {cap.id} v{cap.version}")
    print(f"  {artifact_path}")
    print(f"  VALIDATED → APPROVED")
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

async def _cmd_validate_async(args) -> int:
    artifact_path = Path(args.artifact)
    if not artifact_path.exists():
        _die(f"Artifact not found: {artifact_path}")

    from src.models.artifact import Capability, LifecycleStatus
    from src.models.policy import ExecutionMode

    cap = Capability.model_validate_json(artifact_path.read_text())

    if cap.lifecycle_status == LifecycleStatus.VALIDATED:
        print(f"{cap.id} v{cap.version} is already VALIDATED.")
        return 0
    if cap.lifecycle_status != LifecycleStatus.DRAFT:
        _die(f"Cannot validate: artifact is {cap.lifecycle_status.value!r}. Only DRAFT artifacts can be validated.")

    params = _parse_kvs(args.param)
    login_params = _parse_kvs(args.login_param)
    policy = _load_policy(args.policy)

    run_id = _run_id("validate")
    evidence_dir = Path(args.evidence_dir) / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nValidating {cap.id!r} v{cap.version}")
    print(f"  Artifact:  {artifact_path}")
    print(f"  Evidence:  {evidence_dir}/")

    from playwright.async_api import async_playwright
    from src.models.result import ReplayStatus
    from src.observability.logger import RunLogger
    from src.replay.executor import ReplayExecutor
    from src.surface.web import WebAdapter

    log_path = evidence_dir / "run_log.jsonl"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        adapter = WebAdapter(page, ctx,
                             known_dialog_patterns=policy.action.known_dialog_patterns)

        with RunLogger(run_id, cap.id, log_path) as logger:
            executor = ReplayExecutor(adapter, policy, logger, ExecutionMode.VALIDATION,
                                      evidence_dir=evidence_dir)
            await executor.setup(ctx)

            if args.login:
                login_cap = Capability.model_validate_json(Path(args.login).read_text())
                login_result = await executor.run(login_cap, login_params)
                if login_result.status != ReplayStatus.SUCCESS:
                    print(f"  Login FAILED: {login_result.status}")
                    await browser.close()
                    return 1

            result = await executor.run(cap, params)

        await browser.close()

    print(f"\n  Status:  {result.status.value}")
    if result.failure:
        print(f"  Failure: [{result.failure.kind.value}] {result.failure.observed}")
        return 1

    # Promote DRAFT → VALIDATED
    updated = cap.model_copy(update={"lifecycle_status": LifecycleStatus.VALIDATED})
    artifact_path.write_text(updated.model_dump_json(indent=2))
    print(f"  Lifecycle: DRAFT → VALIDATED")
    print(f"  Artifact updated: {artifact_path}")
    return 0


def cmd_validate(args) -> int:
    return asyncio.run(_cmd_validate_async(args))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(args) -> int:
    from src.models.artifact import Capability

    search_dir = Path(args.dir)
    if not search_dir.exists():
        print(f"Directory not found: {search_dir}")
        return 0

    artifacts = []
    for path in sorted(search_dir.rglob("*.json")):
        try:
            cap = Capability.model_validate_json(path.read_text())
            artifacts.append((path, cap))
        except Exception:
            pass  # skip non-capability files

    if not artifacts:
        print(f"No capability artifacts found in {search_dir}/")
        return 0

    # Filter by status if requested
    if args.status:
        status_filter = args.status.upper()
        artifacts = [(p, c) for (p, c) in artifacts
                     if c.lifecycle_status.value == status_filter]
        if not artifacts:
            print(f"No {status_filter} artifacts found.")
            return 0

    # Print table
    id_w   = max(len(c.id) for _, c in artifacts) + 2
    ver_w  = 9
    stat_w = 11

    header = f"{'ID':<{id_w}}  {'VERSION':<{ver_w}}  {'STATUS':<{stat_w}}  PATH"
    print(header)
    print("-" * len(header))
    for path, cap in artifacts:
        print(f"{cap.id:<{id_w}}  {cap.version:<{ver_w}}  "
              f"{cap.lifecycle_status.value:<{stat_w}}  {path}")
    print(f"\n{len(artifacts)} artifact(s)")
    return 0


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def cmd_schema(args) -> int:
    """Print the LLM tool schemas used by the discovery agent."""
    from src.agent.llm import TOOLS
    print(json.dumps(TOOLS, indent=2))
    return 0


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Computer-Use Automation System — record and replay bank back-office workflows",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- discover ----
    disc = sub.add_parser("discover", help="Run the LLM discovery agent")
    disc.add_argument("--spec",       required=True,
                      help="Discovery spec JSON (capability_id, goal, parameters, outputs, detectors)")
    disc.add_argument("--param",      action="append", metavar="key=value",
                      help="Runtime parameter value (e.g. --param member_id=10001); repeatable")
    disc.add_argument("--target",     metavar="URL",
                      help="Override target_url from spec")
    disc.add_argument("--login",      metavar="ARTIFACT",
                      help="Login capability artifact to run before discovery")
    disc.add_argument("--login-param", dest="login_param", action="append", metavar="key=value",
                      help="Login parameter (e.g. --login-param username=operator); repeatable")
    disc.add_argument("--policy",     default="config/policy.json",
                      help="Policy JSON file (default: config/policy.json)")
    disc.add_argument("--model",      metavar="MODEL_ID",
                      help="Override LLM model (default: DISCOVERY_MODEL env or claude-sonnet-4-6)")
    disc.add_argument("--max-steps",  dest="max_steps", type=int, default=25,
                      help="Max LLM steps before timeout (default: 25)")
    disc.add_argument("--wall-clock", dest="wall_clock", type=float, default=300,
                      help="Wall-clock timeout in seconds (default: 300)")
    disc.add_argument("--output-dir", dest="output_dir", default="artifacts",
                      help="Directory to write the produced artifact (default: artifacts/)")
    disc.add_argument("--evidence-dir", dest="evidence_dir", default="evidence",
                      help="Directory to write run logs and screenshots (default: evidence/)")
    disc.add_argument("--headed",     action="store_true",
                      help="Launch browser in headed (visible) mode")

    # ---- replay ----
    rep = sub.add_parser("replay", help="Replay a capability artifact")
    rep.add_argument("artifact",      help="Path to the capability JSON artifact")
    rep.add_argument("--param",       action="append", metavar="key=value",
                     help="Runtime parameter value; repeatable")
    rep.add_argument("--mode",        choices=["validation", "attended", "unattended"],
                     default="validation",
                     help="Execution mode (default: validation)")
    rep.add_argument("--login",       metavar="ARTIFACT",
                     help="Login capability artifact to run before replay")
    rep.add_argument("--login-param", dest="login_param", action="append", metavar="key=value",
                     help="Login parameter; repeatable")
    rep.add_argument("--policy",      default="config/policy.json",
                     help="Policy JSON file (default: config/policy.json)")
    rep.add_argument("--evidence-dir", dest="evidence_dir", default="evidence",
                     help="Directory to write run logs (default: evidence/)")
    rep.add_argument("--headed",      action="store_true",
                     help="Launch browser in headed mode")
    rep.add_argument("--operator-channel", dest="operator_channel", action="store_true",
                     help="Enable human handoff on NEEDS_HUMAN events (requires headed mode)")

    # ---- validate ----
    val = sub.add_parser("validate", help="Run a validation replay and promote DRAFT → VALIDATED on success")
    val.add_argument("artifact",      help="Path to the capability JSON artifact")
    val.add_argument("--param",       action="append", metavar="key=value",
                     help="Runtime parameter value; repeatable")
    val.add_argument("--login",       metavar="ARTIFACT",
                     help="Login capability artifact to run before validation")
    val.add_argument("--login-param", dest="login_param", action="append", metavar="key=value",
                     help="Login parameter; repeatable")
    val.add_argument("--policy",      default="config/policy.json",
                     help="Policy JSON file (default: config/policy.json)")
    val.add_argument("--evidence-dir", dest="evidence_dir", default="evidence",
                     help="Directory to write run logs (default: evidence/)")
    val.add_argument("--headed",      action="store_true",
                     help="Launch browser in headed mode")

    # ---- approve ----
    appr = sub.add_parser("approve", help="Promote a VALIDATED artifact to APPROVED")
    appr.add_argument("artifact",     help="Path to the capability JSON artifact")

    # ---- list ----
    lst = sub.add_parser("list", help="List capability artifacts")
    lst.add_argument("--dir",    default="artifacts",
                     help="Directory to search (default: artifacts/)")
    lst.add_argument("--status", choices=["DRAFT", "VALIDATED", "APPROVED", "DEPRECATED"],
                     help="Filter by lifecycle status")

    # ---- schema ----
    sub.add_parser("schema", help="Print LLM tool schemas used by the discovery agent")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "discover": cmd_discover,
        "replay":   cmd_replay,
        "validate": cmd_validate,
        "approve":  cmd_approve,
        "list":     cmd_list,
        "schema":   cmd_schema,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
