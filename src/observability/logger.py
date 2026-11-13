from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict


class EventType(str, Enum):
    RUN_START = "run_start"
    RUN_COMPLETE = "run_complete"
    STEP_START = "step_start"
    STEP_COMPLETE = "step_complete"
    STEP_FAILED = "step_failed"
    ACTION_NAVIGATE = "action_navigate"
    ACTION_CLICK = "action_click"
    ACTION_FILL = "action_fill"      # value is NEVER logged — only field ref
    ACTION_EXTRACT = "action_extract"
    ACTION_ASSERT = "action_assert"
    CHECKPOINT_PASS = "checkpoint_pass"
    CHECKPOINT_FAIL = "checkpoint_fail"
    DIALOG_CAPTURED = "dialog_captured"
    OUTCOME_DETECTED = "outcome_detected"
    ESCALATION_START = "escalation_start"
    ESCALATION_END = "escalation_end"
    POLICY_DENIED = "policy_denied"
    LLM_CALL = "llm_call"            # discovery loop only — proves 0 LLM calls in replay


class RunLogger:
    """Append-only structured JSONL logger with replace-based sensitive value scrubbing.

    Design: scrubbing happens at write time, not at log-call time.
    Call register_sensitive(name, value) for each input/output value that
    should be hidden, then log freely — occurrences are replaced with <name>
    before the line hits disk.

    FILL action values are never passed to log() at all — callers log
    ACTION_FILL with field_ref only, never the value itself.
    """

    def __init__(
        self,
        run_id: str,
        capability_id: str,
        log_path: Path,
        min_scrub_length: int = 4,
    ):
        self._run_id = run_id
        self._capability_id = capability_id
        self._min_scrub_length = min_scrub_length
        self._sensitive: Dict[str, str] = {}  # placeholder_name → raw value

        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = log_path.open("w", encoding="utf-8")

    def register_sensitive(self, name: str, value: str) -> None:
        """Register value to be replaced with <name> in all subsequent writes.

        Values shorter than min_scrub_length are skipped — avoids replacing
        short tokens like "0" or "$" that appear everywhere on the page.
        """
        if value and len(value) >= self._min_scrub_length:
            self._sensitive[name] = value

    def log(self, event_type: EventType, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "capability_id": self._capability_id,
            "event": event_type.value,
            **fields,
        }
        line = self._scrub(json.dumps(record, default=str))
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def _scrub(self, text: str) -> str:
        for name, value in self._sensitive.items():
            text = text.replace(value, f"<{name}>")
        return text

    def __enter__(self) -> RunLogger:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
