# Computer-Use Automation System

A record-once, replay-many automation engine for bank back-office UIs that have no API.
Built as a take-home assignment for interface.ai (Software Engineer II).

**Core idea:** An LLM drives a real browser once to discover a workflow and records it as a
typed, versioned JSON artifact. Every subsequent run replays that artifact deterministically —
zero LLM calls at replay time, proven from structured logs.

---

## Requirements

- Python 3.9+
- An Anthropic API key (only needed for `discover` and `test_discovery.py`)

```bash
pip install -r requirements.txt
playwright install chromium
```

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Quick Start

**1. Start the mock banking app**

```bash
python3 target_app/server.py
# Banking app running at http://localhost:8080
# Login: operator / demo123
```

**2. Replay a hand-written artifact (no API key needed)**

```bash
python3 cli.py replay artifacts/fixtures/member_lookup_balance_from_dashboard.json \
  --param member_id=10001 \
  --login artifacts/fixtures/login.json \
  --login-param username=operator \
  --login-param password=demo123
```

Expected output:
```
Replaying 'member.lookup_balance' v1.0.0 [DRAFT/validation]
  Status:  SUCCESS
  savings_balance: $1,234.56
  checking_balance: $456.78
  LLM calls: 0
  Steps:   6/6
```

**3. Run the LLM discovery agent (produces a new artifact)**

```bash
python3 cli.py discover \
  --spec config/member_lookup_spec.json \
  --param member_id=10001 \
  --login artifacts/fixtures/login.json \
  --login-param username=operator \
  --login-param password=demo123
```

The agent drives the browser, records each step, and writes a replayable artifact to `artifacts/`
(e.g. `artifacts/member_lookup_balance.1731234567.json`).

**3b. Replay the discovered artifact for a different member (zero LLM calls)**

```bash
python3 cli.py replay evidence/discovery_run/artifact.json \
  --param member_id=10002 \
  --login artifacts/fixtures/login.json \
  --login-param username=operator \
  --login-param password=demo123
```

Expected output:
```
Replaying 'member.lookup_balance' ...
  Status:  SUCCESS
  savings_balance: $5,678.90
  checking_balance: $1,200.00
  LLM calls: 0
  Steps:   6/6
```

**4. Promote DRAFT → VALIDATED → APPROVED**

`replay` never modifies the artifact. Use `validate` to run a validation replay and promote on success:

```bash
python3 cli.py validate artifacts/fixtures/member_lookup_balance_from_dashboard.json \
  --param member_id=10001 \
  --login artifacts/fixtures/login.json \
  --login-param username=operator \
  --login-param password=demo123
# Lifecycle: DRAFT → VALIDATED
```

Then approve it:

```bash
python3 cli.py approve artifacts/fixtures/member_lookup_balance_from_dashboard.json
# Approved: member.lookup_balance v1.0.0
#   VALIDATED → APPROVED
```

**5. List all artifacts**

```bash
python3 cli.py list
```

---

## Running the Test Suites

### Executor tests (no API key — 24 assertions, ~25s)

Covers happy paths, MEMBER_NOT_FOUND, ACCOUNT_FROZEN, POLICY_DENIED, slow-load, APP_ERROR,
chained login+lookup, and import isolation proof.

```bash
python3 test_executor.py
# All tests passed.
```

### Human handoff test (no API key — 10 assertions, ~15s)

Covers the supervisor-verification interstitial → NEEDS_HUMAN → OperatorChannel →
programmatic signal → replay resumes.

```bash
python3 test_handoff.py
# All tests passed.
```

### Discovery test (requires ANTHROPIC_API_KEY — ~30s, ~$0.05)

End-to-end: LLM discovers the workflow, produces an artifact, artifact replays for two members.

```bash
python3 test_discovery.py
# All tests passed.
```

### Surface and guardrails tests

```bash
python3 test_surface.py
python3 test_guardrails.py
```

---

## CLI Reference

```
cli.py discover --spec SPEC_FILE [options]
  --param key=value        Runtime parameter (repeatable)
  --login ARTIFACT         Login capability to run first
  --login-param key=value  Login parameter (repeatable)
  --policy FILE            Policy JSON (default: config/policy.json)
  --model MODEL_ID         Override LLM model
  --max-steps N            LLM step limit (default: 25)
  --wall-clock SECONDS     Timeout (default: 300)
  --headed                 Show browser window

cli.py replay ARTIFACT [options]
  --param key=value        Runtime parameter (repeatable)
  --mode validation|attended|unattended
  --login ARTIFACT         Login capability to run first
  --login-param key=value  Login parameter (repeatable)
  --operator-channel       Enable human handoff on NEEDS_HUMAN
  --headed                 Show browser window

cli.py validate ARTIFACT [options]   Run validation replay and promote DRAFT → VALIDATED on success
  --param / --login / --login-param  Same as replay
  --headed                           Show browser window

cli.py approve ARTIFACT    Promote VALIDATED → APPROVED
cli.py list [--dir DIR] [--status STATUS]
cli.py schema              Print LLM tool schemas as JSON
```

---

## Fault Injection Reference

The mock server injects specific behaviors by member ID:

| Member ID | Scenario | Classification |
|-----------|----------|----------------|
| `10001`   | Happy path | SUCCESS |
| `10002`   | Second happy path | SUCCESS |
| `99999`   | Member not found | BUSINESS_OUTCOME: MEMBER_NOT_FOUND |
| `99998`   | Supervisor verification interstitial | NEEDS_HUMAN → OperatorChannel |
| `99997`   | Account frozen | BUSINESS_OUTCOME: ACCOUNT_FROZEN |
| `99996`   | Session timeout (fires once) | HARD_FAILURE: SESSION_EXPIRED (re-login recovery not implemented) |
| `99995`   | Known confirm() dialog | Auto-accepted (requires `known_dialog_patterns` in policy) |
| `99994`   | Unknown confirm() dialog | NEEDS_HUMAN (requires `known_dialog_patterns` in policy) |
| `99993`   | HTTP 500 | HARD_FAILURE: APP_ERROR |
| `99991`   | 1.2s server delay | SUCCESS (polling loop) |

---

## Project Layout

```
cli.py                      Entry point for all subcommands
config/
  policy.json               Allowlist policy (domains, routes, risk rules)
  member_lookup_spec.json   Discovery spec (capability goal, param/output schemas)
src/
  models/
    artifact.py             Capability, Step, Locator, Parameter, Output, OutcomeDetector
    result.py               ReplayResult, BusinessOutcome, Failure, InterventionRecord
    policy.py               AllowlistPolicy, ActionPolicy, ExecutionMode
  agent/
    loop.py                 LLM discovery loop (contract-first, vision-augmented)
    llm.py                  Anthropic client, tool schemas, system prompt
  surface/
    base.py                 SurfaceAdapter abstract base
    web.py                  Playwright WebAdapter (hostile tables, iframes, dialogs)
  recorder/
    recorder.py             Step recording, template substitution, anchor-relative locators
  replay/
    executor.py             Replay state machine (zero LLM calls)
    locator.py              Locator resolution (ARIA, label, anchor-relative, frame traversal)
    error.py                Error taxonomy, outcome detector evaluation, checkpoint checking
  escalation/
    operator_channel.py     Human handoff (asyncio race, action capture, ownership lease)
  guardrails/
    policy.py               PolicyGate (pre-action), RouteGuard (network-layer, context.route())
  observability/
    logger.py               Structured JSONL with replace-based sensitive value scrubbing
target_app/
  server.py                 Intentionally hostile bank app + fault injection by member ID
  static/                   login.html, dashboard.html, interstitial.html
artifacts/
  fixtures/                 Hand-written and discovery-produced capability JSON files
evidence/                   Run logs and screenshots from each test run
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required for discovery) | Anthropic API key |
| `DISCOVERY_MODEL` | `claude-sonnet-4-6` | LLM model for discovery agent |
| `PORT` | `8080` | Mock banking app port |
