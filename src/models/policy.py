from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class ExecutionMode(str, Enum):
    VALIDATION = "validation"   # DRAFT artifacts only — used to promote to VALIDATED
    ATTENDED = "attended"       # VALIDATED artifacts — human is present and watching
    UNATTENDED = "unattended"   # APPROVED artifacts only — full automation


class ActionPolicy(BaseModel):
    """Deterministic rules for action risk classification.

    Irreversible patterns match against the control's accessible name or the
    request route — not LLM judgment. Add patterns here as new capabilities
    are recorded for risky operations.
    """
    irreversible_name_patterns: List[str] = Field(
        default_factory=lambda: [
            "submit transfer",
            "confirm transfer",
            "approve transfer",
            "close account",
            "delete account",
            "approve transaction",
            "confirm payment",
        ],
        description="Accessible name substrings (case-insensitive) that classify a click as irreversible",
    )
    irreversible_route_patterns: List[str] = Field(
        default_factory=lambda: [
            "/transfer/submit",
            "/transfer/confirm",
            "/account/close",
            "/account/delete",
            "/payment/confirm",
        ],
        description="Request route substrings that classify an action as irreversible",
    )
    sensitive_value_min_length: int = Field(
        default=4,
        description="Values shorter than this are not scrubbed — avoids replacing '0' or '$' everywhere",
    )
    known_dialog_patterns: List[str] = Field(
        default_factory=list,
        description="Message substrings (case-insensitive) that identify safe auto-dismissible dialogs. "
                    "Dialogs NOT matching any pattern are flagged as unknown and escalate to NEEDS_HUMAN.",
    )
    max_recovery_attempts: int = 2
    recovery_timeout_ms: int = 10_000


class AllowlistPolicy(BaseModel):
    """Runtime policy loaded from config — always evaluated against current values, never from artifact."""
    allowed_domains: List[str]
    allowed_routes: List[str]            # glob patterns, e.g. ["/member/*", "/account/*"]
    blocked_routes: List[str] = Field(default_factory=list)
    action: ActionPolicy = Field(default_factory=ActionPolicy)
    operator_lease_seconds: int = 300
    session_restart_max_attempts: int = 1  # session timeout recovery capped at one restart
