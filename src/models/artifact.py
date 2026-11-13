from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class LifecycleStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    DEPRECATED = "DEPRECATED"


class Surface(str, Enum):
    WEB = "web"
    DESKTOP = "desktop"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    FINANCIAL = "financial_data"
    PII = "pii"
    SECRET = "secret"


class RiskTier(str, Enum):
    READ = "read"
    MUTATING_EXPECTED = "mutating_expected"
    IRREVERSIBLE = "irreversible"


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    EXTRACT = "extract"
    ASSERT = "assert"


class LocatorStrategy(str, Enum):
    ARIA_ROLE_NAME = "aria_role_name"
    LABEL_ASSOCIATION = "label_association"
    ANCHOR_RELATIVE = "anchor_relative"
    VISIBLE_TEXT = "visible_text"
    SEMANTIC_CSS = "semantic_css"      # web-only
    RELATIVE_XPATH = "relative_xpath"  # web-only
    VISUAL_ANCHOR = "visual_anchor"    # reserved, unbuilt


class CheckpointType(str, Enum):
    PAGE_CONTAINS_TEXT = "page_contains_text"
    ELEMENT_VISIBLE = "element_visible"
    URL_CONTAINS = "url_contains"
    OUTPUT_EXTRACTED = "output_extracted"


class AnchorRelativeSpec(BaseModel):
    """Structured anchor for locating an element relative to a nearby label.

    Used when the surface has no ARIA labels — legacy table layouts where
    the only stable reference is adjacent cell text.
    Example: textbox in the row whose first cell reads 'Member ID:'
    """
    target_role: str                    # "textbox", "button", "link"
    row_cell_text: Optional[str] = None # text in an adjacent table cell
    column_header: Optional[str] = None # text of the column header above
    section_text: Optional[str] = None  # nearby heading or section label


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str                               # accessible name / text / selector / human-readable description
    role: Optional[str] = None               # for ARIA_ROLE_NAME and ANCHOR_RELATIVE
    anchor: Optional[AnchorRelativeSpec] = None  # structured spec for ANCHOR_RELATIVE only
    surface: Optional[Surface] = None        # None = all surfaces; web = web-only
    frame_path: Optional[List[str]] = None   # ["main", "account-frame"] — required for iframe targets
    scope: Optional[str] = None             # accessible name of parent container to disambiguate
    constraints: Dict[str, bool] = Field(
        default_factory=lambda: {"visible": True, "enabled": True, "unique": True}
    )

    @model_validator(mode="after")
    def anchor_relative_requires_anchor(self) -> Locator:
        if self.strategy == LocatorStrategy.ANCHOR_RELATIVE and self.anchor is None:
            raise ValueError("ANCHOR_RELATIVE locator must include an anchor spec")
        return self


class PageIdentity(BaseModel):
    """Identifies which page the executor expects to be on before a step runs.

    Expected page mismatch + locator miss = HARD_FAILURE (drift).
    Unknown page + session alive + URL in allowlist = NEEDS_HUMAN.
    """
    url_pattern: Optional[str] = None    # glob, e.g. "*/dashboard*"
    landmark_text: Optional[str] = None  # text that must be present to confirm identity


class Checkpoint(BaseModel):
    """Asserted after a step executes to confirm the expected state was reached."""
    type: CheckpointType
    value: str


class Parameter(BaseModel):
    name: str
    type: str           # "string", "integer", "decimal"
    required: bool = True
    pattern: Optional[str] = None  # regex for validation at replay time
    sensitivity: Sensitivity = Sensitivity.INTERNAL


class Output(BaseModel):
    name: str
    type: str
    sensitivity: Sensitivity = Sensitivity.FINANCIAL


class OutcomeDetector(BaseModel):
    """Declares a known non-happy-path result generated from a probe run.

    Checked during replay before classifying any failure. Detectors must
    match their probe screen and must NOT match the happy-path screen.
    Priority 1 = checked first (security/permission); higher = checked later.
    """
    code: str                           # e.g. "MEMBER_NOT_FOUND"
    detector_type: CheckpointType
    value: str                          # text / URL pattern to match
    disposition: str = "BUSINESS_OUTCOME"
    priority: int = 10                  # 1 = security violations; 10 = business outcomes


class Step(BaseModel):
    step_id: str
    action: ActionType
    locators: List[Locator] = Field(default_factory=list)
    value: Optional[str] = None           # "{{member_id}}" or static text; None for clicks
    expected_page: Optional[PageIdentity] = None
    preconditions: List[Checkpoint] = Field(default_factory=list)
    postconditions: List[Checkpoint] = Field(default_factory=list)
    checkpoint: Optional[Checkpoint] = None
    timeout_ms: int = 10_000
    retry_safe: bool = True               # False = never repeat even if checkpoint not observed
    risk_tier: RiskTier = RiskTier.READ
    expected_method: Optional[str] = None  # "GET", "POST" — declared at discovery for mutating steps
    expected_route: Optional[str] = None   # "/api/member/search"
    output_key: Optional[str] = None       # for EXTRACT actions — which output this step populates

    @model_validator(mode="after")
    def extract_requires_output_key(self) -> Step:
        if self.action == ActionType.EXTRACT and self.output_key is None:
            raise ValueError("EXTRACT step must declare output_key")
        return self

    @model_validator(mode="after")
    def fill_requires_value(self) -> Step:
        if self.action == ActionType.FILL and self.value is None:
            raise ValueError("FILL step must declare value")
        return self


class CompatibilityInfo(BaseModel):
    vendor: str
    app_version: str
    surface: Surface = Surface.WEB


class Capability(BaseModel):
    """A recorded, replayable automation capability — the core artifact of the system."""
    id: str                   # e.g. "member.lookup_balance"
    version: str = "1.0.0"
    name: str
    description: str
    target: str               # entry point URL
    compatibility: CompatibilityInfo
    parameters: List[Parameter]
    outputs: List[Output]
    steps: List[Step]
    outcome_detectors: List[OutcomeDetector] = Field(default_factory=list)
    lifecycle_status: LifecycleStatus = LifecycleStatus.DRAFT
    human_assisted: bool = False  # if True, cannot promote to VALIDATED — requires re-discovery
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = "agent-discovery"
    base_capability: Optional[str] = None      # for tenant overlays
    tenant_overrides: Optional[Dict[str, Any]] = None

    def can_run_in_validation_mode(self) -> bool:
        return self.lifecycle_status == LifecycleStatus.DRAFT and not self.human_assisted

    def can_run_attended(self) -> bool:
        return self.lifecycle_status in (
            LifecycleStatus.VALIDATED, LifecycleStatus.APPROVED
        )

    def can_run_unattended(self) -> bool:
        return self.lifecycle_status == LifecycleStatus.APPROVED

    def sorted_outcome_detectors(self) -> List[OutcomeDetector]:
        """Security/permission detectors (priority 1) checked before business outcomes."""
        return sorted(self.outcome_detectors, key=lambda d: d.priority)
