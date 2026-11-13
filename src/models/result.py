from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    FAILED = "FAILED"
    ESCALATION_TIMEOUT = "ESCALATION_TIMEOUT"


class FailureKind(str, Enum):
    LOCATOR_NOT_FOUND = "LOCATOR_NOT_FOUND"       # expected page, element missing — likely drift
    LOCATOR_AMBIGUOUS = "LOCATOR_AMBIGUOUS"       # locator matched more than one element
    CHECKPOINT_TIMEOUT = "CHECKPOINT_TIMEOUT"     # expected state never appeared within timeout
    POLICY_DENIED = "POLICY_DENIED"               # engine refused action before it reached the app
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"         # artifact or input failed validation
    FRAME_UNREACHABLE = "FRAME_UNREACHABLE"       # iframe could not be found or accessed
    UNEXPECTED_MUTATION = "UNEXPECTED_MUTATION"   # undeclared non-GET at replay — abort immediately
    APP_ERROR = "APP_ERROR"                       # application returned HTTP 5xx or error page
    OPERATOR_ABORT = "OPERATOR_ABORT"             # operator explicitly signaled abort during handoff
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"   # NEEDS_HUMAN raised but no OperatorChannel configured


class Failure(BaseModel):
    kind: FailureKind
    step_id: str
    expected: str
    observed: str
    screenshot_path: Optional[str] = None


class BusinessOutcome(BaseModel):
    """A valid, non-error result that the calling agent must handle programmatically.

    Example: MEMBER_NOT_FOUND is a legitimate answer, not a crash.
    Distinct from POLICY_DENIED (engine refusal) which is a Failure.
    """
    code: str    # e.g. "MEMBER_NOT_FOUND", "ACCOUNT_FROZEN", "PERMISSION_DENIED"
    detail: str


class InterventionRequest(BaseModel):
    """Written to evidence/ when automation cannot safely proceed."""
    run_id: str
    capability_id: str
    step_id: str
    reason: str                  # "NEEDS_HUMAN: unknown page state — supervisor verification interstitial"
    screenshot_path: str
    current_url: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InterventionRecord(BaseModel):
    """Full record of a human intervention — request + what happened + how it resolved."""
    request: InterventionRequest
    signal: str                         # "retry", "done", "abort"
    human_actions: List[Dict[str, Any]] = Field(default_factory=list)  # captured events, values scrubbed
    resumed_at: datetime
    postconditions_verified: bool


class ReplayResult(BaseModel):
    """The structured result returned to the caller after a replay run.

    Caller must handle all four statuses:
      SUCCESS           — outputs are populated
      BUSINESS_OUTCOME  — a declared outcome was reached; check outcome_code
      FAILED            — a hard failure occurred; check failure for debug detail
      ESCALATION_TIMEOUT — human handoff started but lease expired with no signal
    """
    run_id: str
    capability_id: str
    capability_version: str
    status: ReplayStatus
    outputs: Optional[Dict[str, Any]] = None
    business_outcome: Optional[BusinessOutcome] = None
    failure: Optional[Failure] = None
    intervention: Optional[InterventionRecord] = None
    steps_completed: int
    total_steps: int
    started_at: datetime
    completed_at: datetime
    llm_calls: int = 0  # structurally always 0 for replay — provable from log
