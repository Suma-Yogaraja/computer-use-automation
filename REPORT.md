# Design Report — Computer-Use Automation System

**Assignment:** interface.ai — Software Engineer II take-home  
**Date:** 2026-11-12

---

## 1. Architecture

The system is built in three layers with strict authority boundaries.

```
Layer 1 — INTELLIGENCE
  LLM (Claude Sonnet 4.6)
  Observes UI via ARIA snapshot + screenshot.
  Proposes actions using template syntax — never concrete parameter values.
  Has zero execution authority.

Layer 2 — CONTROL
  Orchestration + Policy Engine + OperatorChannel
  Validates every proposed action before it reaches the browser.
  Policy loaded at runtime from config/policy.json — never embedded in an artifact.
  Enforced at two independent points (see Safety).

Layer 3 — EXECUTION
  SurfaceAdapter (WebAdapter) + ReplayExecutor
  Resolves locators, executes browser actions, verifies checkpoints.
  Has no policy knowledge — only does what Layer 2 authorizes.
```

The separation has two practical consequences. First, the replay engine can be tested independently of the LLM — zero LLM calls at replay time is structurally guaranteed, not just a convention. Second, the policy can be updated without touching any capability artifact; every execution reads the policy fresh from disk.

The system prompt includes the concrete parameter value alongside the template form as a recognition hint: `"member_id: current value is '10001', ALWAYS write as {{member_id}}"`. The LLM uses this to identify the value when it appears in ARIA snapshot text (e.g., "link 'View Account 10001'") and produce the correct template form ("View Account {{member_id}}") in the recorded step. The concrete value never appears as an action parameter in the produced artifact.

---

## 2. Artifact schema

A `Capability` is a typed, versioned, self-describing callable. It records not just what actions to take but enough context to reason about failures.

**Locator priority** — most to least robust:
1. `aria_role_name` — role + accessible name. Works on any ARIA-compliant surface.
2. `label_association` — `<label for>` or aria-labelledby.
3. `anchor_relative` — "textbox in the row whose adjacent cell reads `Member ID:`." Primary strategy for the hostile demo app: no ids, no data-testid, table-based layout where only adjacent text distinguishes inputs.
4. `visible_text`, `semantic_css`, `relative_xpath` — progressively more fragile.

The artifact stores locators in this priority order. The current replay implementation resolves `locators[0]` only; the fallback chain is designed but not implemented in this submission.

**Template substitution** — the artifact stores `{{member_id}}`, never `10001`. The recorder replaces concrete values at record time (`_template()`). The executor renders them back at replay time (`_render()`). One artifact serves any member.

**Step metadata** — each step stores:
- `expected_page` (URL pattern + landmark text): what page should the browser be on before this step? A mismatch distinguishes unknown page state (NEEDS_HUMAN) from missing locator (drift → HARD_FAILURE: LOCATOR_NOT_FOUND).
- `postconditions`: checkpoints polled after the action to confirm the expected state was reached before proceeding.
- `risk_tier`: READ / MUTATING_EXPECTED / IRREVERSIBLE — drives policy checks and RouteGuard configuration per step.
- `retry_safe`: false for form submits (POST side effects are not idempotent).

**Lifecycle** — DRAFT → VALIDATED → APPROVED → DEPRECATED. Execution mode is checked before the first step: VALIDATION mode accepts DRAFT artifacts; ATTENDED requires VALIDATED/APPROVED; UNATTENDED requires only APPROVED.

---

## 3. Determinism & error handling

**Zero LLM calls** — the replay executor is structurally incapable of calling the LLM. The import isolation test confirms this via subprocess: `anthropic` and `src.agent` do not appear in `sys.modules` after importing `src.replay`. The structured log proves it at runtime: no `llm_call` events appear in replay logs.

**Polling loop** — `_poll_until_settled()` races detectors against postconditions in a 300ms loop up to `step.timeout_ms`. This handles slow-loading pages without hardcoded sleeps. A final detector check at the deadline catches pages that loaded an error state after networkidle but before the last poll.

**Detector-first ordering** — outcome detectors are checked before postconditions, before page-identity disambiguation. When a locator fails, the first question is "did the page change to a known error state?" not "is this drift?". Without this ordering, the MEMBER_NOT_FOUND page would become NEEDS_HUMAN instead of BUSINESS_OUTCOME: MEMBER_NOT_FOUND, because the results-table locator finds nothing and the page-identity check sees an unexpected URL.

**Error taxonomy:**

| Kind | Meaning |
|------|---------|
| `LOCATOR_NOT_FOUND` | Expected page, element missing — likely UI drift |
| `LOCATOR_AMBIGUOUS` | Locator matched more than one element |
| `CHECKPOINT_TIMEOUT` | Expected state never arrived within timeout |
| `POLICY_DENIED` | Engine refused action before it reached the app |
| `SCHEMA_VIOLATION` | Artifact or input failed validation |
| `FRAME_UNREACHABLE` | Named iframe not found or inaccessible |
| `UNEXPECTED_MUTATION` | Undeclared non-GET at replay — abort immediately |
| `APP_ERROR` | Application returned HTTP 5xx or error page text |
| `OPERATOR_ABORT` | Human operator explicitly signaled abort |
| `ESCALATION_REQUIRED` | NEEDS_HUMAN raised but no OperatorChannel configured |

`UNEXPECTED_MUTATION` is never escalated to a human. An undeclared mutation is a system integrity issue — escalating it would let a bad artifact execute unknown side effects on approval from an operator who may not understand what the mutation did.

**Failure screenshots** — a screenshot is captured at the moment of failure and stored in `evidence/failures/`. The path is attached to the `Failure` record so reviewers can inspect the browser state at failure time.

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction** — `SurfaceAdapter` is an abstract base class defining `navigate`, `click`, `fill`, `extract`, `screenshot`, `observe`, and `page_contains_text`. The recorder, replay engine, and discovery agent all talk to this interface. A desktop implementation would swap Playwright for PyAutoGUI or pywinauto with zero changes in those layers.

**Hostile table layout** — the demo app uses `<td>` as labels, table-based layout, and no ids or data-testid attributes. The `anchor_relative` locator type describes elements the way a human would: "the textbox in the row whose adjacent cell reads 'Member ID:'". This is robust across any UI that shares the same spatial relationships, regardless of DOM structure.

**Multi-tenant artifact reuse** — the `Capability` model carries `base_capability` and `tenant_overrides` fields. A tenant overlay specifies alternative locator values for elements that differ between tenants (different label text, different entry point URL). The resolver merges base + overlay at replay time. Tenants sharing the same workflow structure share one base artifact; only the diff is stored per tenant. Designed; not built for this submission.

**Drift detection per tenant** — a locator failure means different things in a multi-tenant deployment. If the base locator fails but the tenant overlay provides an alternative locator that succeeds, it is a known tenant difference, not drift. Only if all locators for a step (base + overlay) fail does the engine classify it as drift and emit HARD_FAILURE: LOCATOR_NOT_FOUND. This distinguishes "tenant A uses 'Client Number' where tenant B uses 'Member ID'" from "the UI changed and needs re-discovery." Requires the fallback locator chain (see §2).

**Dialog classification** — `WebAdapter` accepts an optional `known_dialog_patterns` list. Dialogs whose message matches a registered pattern are auto-accepted. Dialogs that do not match are dismissed and flagged, causing the executor to escalate to NEEDS_HUMAN. This is the mechanism that separates 99995 (known maintenance dialog — RECOVERABLE) from 99994 (unknown regulatory hold — NEEDS_HUMAN).

---

## 5. Escalation & handoff

**Three triggers for NEEDS_HUMAN:**
1. Page identity mismatch before a step: the browser is on an unexpected in-policy URL. Example: member 99998 triggers a supervisor-verification interstitial instead of the search-results page.
2. Checkpoint timeout with an in-policy URL: the step's postcondition never resolved, but the current URL is within the allowlist.
3. Unknown dialog: a confirm/prompt dialog that does not match any registered known pattern.

**Handoff flow:**
```
NEEDS_HUMAN raised
  │
  ▼  OperatorChannel.request_handoff()
  │  - Screenshot saved to evidence/screenshots/
  │  - Stdout prompt printed
  │  - Context-level request listener installed (survives navigations)
  │  - RouteGuard set to human_mode (mutations allowed, allowlist still enforced)
  │  - asyncio.wait_for(signal_provider(), timeout=lease_seconds)
  │
  ▼  Signal received (R=retry / D=done / A=abort / lease timeout)
  │  RouteGuard human_mode cleared
  │  Context request listener removed
  │
  ├── done    → verify step postconditions → continue if pass, FAILED if not
  ├── retry   → verify expected_page → re-execute step if pass, FAILED if not
  ├── abort   → FAILED immediately (OPERATOR_ABORT)
  └── timeout → ESCALATION_TIMEOUT
```

Human actions are captured via a context-level `context.on("request", ...)` listener rather than page-level JavaScript. This survives page navigations during handoff and records the full HTTP activity the operator generated.

The lease timer is raced against the signal using `asyncio.wait_for` with `loop.run_in_executor(None, input)` — blocking stdin read in a thread, non-blocking asyncio.

---

## 6. Safety

**Two enforcement points:**

1. **PolicyGate** (pre-action, Python): checks before any browser action runs. Navigate targets checked against domain/route allowlist. IRREVERSIBLE steps raise `EscalationRequired` before the click executes. This check is fast and cheap.

2. **RouteGuard** (network layer, `context.route()`): installs a Playwright route handler on the BrowserContext. Any non-GET request not pre-registered via `expect_mutation()` is aborted and recorded as `UNEXPECTED_MUTATION`. This covers requests generated by iframes, JavaScript redirects, and human actions during handoff (in human_mode, only the allowlist is enforced — mutations are allowed freely so the operator can operate the browser).

**Three-tier risk classification:**

| Tier | At record time | At replay time |
|------|---------------|----------------|
| READ | No declaration needed | Always allowed |
| MUTATING_EXPECTED | Route inference captures POST method + route | RouteGuard allows one declared mutation |
| IRREVERSIBLE | LLM-proposed steps matching patterns | PolicyGate raises EscalationRequired |

The name and route patterns in `config/policy.json` are defined but not enforced in this submission. Nothing in the discovery loop or recorder checks them, so `risk_tier=IRREVERSIBLE` is never set by the agent. The only active irreversible check is against a step's already-recorded `risk_tier` field: if a step was manually recorded with `risk_tier=IRREVERSIBLE`, PolicyGate raises EscalationRequired before it executes. Wiring the patterns into the recording path is the next step.

**Replace-based scrubbing** — the logger scrubs sensitive values at write time, not at log-call time. Callers register `(name, value)` pairs; subsequent writes replace occurrences with `<name>`. Values shorter than 4 characters are skipped to avoid collateral replacement. The test confirms this: member IDs, credentials, and balance values do not appear in any replay log line.

---

## 7. Cuts

**LegacyWebAdapter** — not built. The hostile demo app uses `<td>` as labels with table-based layout and no semantic elements. The existing `WebAdapter` handles this via `anchor_relative` locators. A separate "legacy" adapter would be the same code with a different name.

**DesktopAdapter** — interface designed (`SurfaceAdapter` abstract base), implementation not built. The contract is clear: swap Playwright for a desktop automation library; recorder, replay, and agent require no changes.

**Session timeout recovery** — member 99996 triggers a session timeout. The `SESSION_EXPIRED` detector fires as `HARD_FAILURE`. The design calls for re-invoking the login capability and restarting — capped at one attempt and only when all executed steps have `retry_safe=True`. The state machine fields are in place; the re-login loop is not implemented.

**Full approval workflow automation** — DRAFT → VALIDATED requires a successful validation-mode replay. The `cli.py replay` command promotes the artifact to VALIDATED on success. The probe automation (run each outcome detector against a known triggering member and confirm it fires) is not automated for this submission.

**Production OperatorChannel broker** — the demo reads signals from stdin. In production, a message broker would relay the browser control token and the signal, enforce the ownership lease on the server side, and prevent two operators from simultaneously claiming a session.

**Human-assisted discovery** — if a human intervenes during discovery, their actions are not captured in the artifact. The design marks such artifacts `human_assisted: true` and blocks promotion to VALIDATED without a clean re-discovery. Flag designed; not wired.

**Multi-tenant overlays** — base + tenant_overrides fields exist in the model; the merge logic at replay time is not implemented.
