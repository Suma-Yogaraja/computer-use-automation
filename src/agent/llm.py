from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import anthropic

# Override with DISCOVERY_MODEL env var (e.g. "claude-haiku-4-5-20251001" for cheaper test runs)
_DEFAULT_MODEL = "claude-sonnet-4-6"

TOOLS: List[Dict] = [
    {
        "name": "navigate",
        "description": (
            "Navigate directly to a URL. "
            "Use ONLY for the initial step (going to the starting page). "
            "For ALL other navigation — including clicking hyperlinks that cause page changes — "
            "use the `click` tool instead. Never construct a URL yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to navigate to"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "fill",
        "description": (
            "Type a value into a text input. "
            "For table-based forms (no visible labels), provide row_context = the adjacent cell text. "
            "ALWAYS use {{param_name}} for parameter values — never the actual value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "Value to type. Use {{param_name}} for parameters (e.g. {{member_id}}).",
                },
                "row_context": {
                    "type": "string",
                    "description": "Text in the adjacent table cell that identifies the row (for table-based forms).",
                },
                "label": {
                    "type": "string",
                    "description": "Label text, if the input has an explicit label (not a table row).",
                },
            },
            "required": ["value"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click any interactive element — buttons AND hyperlinks alike. "
            "When you see a link in the ARIA snapshot (e.g. 'View Account 10001'), "
            "use this tool with role='link', not the navigate tool. "
            "This is the correct tool whenever you need to interact with a visible element."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accessible_name": {
                    "type": "string",
                    "description": (
                        "The button or link text. If the name contains a parameter value "
                        "(e.g. 'View Account 10001'), use the template: 'View Account {{member_id}}'."
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": ["button", "link"],
                    "description": "ARIA role of the element.",
                },
                "row_context": {
                    "type": "string",
                    "description": (
                        "For links inside a table row: text in the adjacent cell that identifies the row. "
                        "If the adjacent cell text is a parameter value, use {{param_name}}."
                    ),
                },
            },
            "required": ["accessible_name", "role"],
        },
    },
    {
        "name": "extract",
        "description": "Read text from a page element and store it as an output value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_key": {
                    "type": "string",
                    "description": "Output key name (must be from the declared output schema).",
                },
                "row_context": {
                    "type": "string",
                    "description": "Text in the adjacent table cell that identifies the row.",
                },
                "column_header": {
                    "type": "string",
                    "description": "Column header text to identify which cell to read.",
                },
                "frame_name": {
                    "type": "string",
                    "description": "Name attribute of the containing iframe, if the element is inside one.",
                },
            },
            "required": ["output_key", "row_context"],
        },
    },
    {
        "name": "goal_complete",
        "description": "Signal that all required outputs have been extracted and the goal is achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief description of what was accomplished."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "goal_failed",
        "description": (
            "Signal that the goal cannot be completed due to a valid business outcome "
            "(member not found, account frozen, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "outcome_code": {
                    "type": "string",
                    "description": "Short code for this outcome (e.g. MEMBER_NOT_FOUND, ACCOUNT_FROZEN).",
                },
                "detail": {
                    "type": "string",
                    "description": "The page text that indicates this outcome.",
                },
            },
            "required": ["outcome_code", "detail"],
        },
    },
]


@dataclass
class ProposedAction:
    action: str
    inputs: Dict[str, Any]
    tool_use_id: str
    # Full content blocks from the assistant response (preserves any text reasoning)
    response_content: List[Any]


class LLMClient:
    def __init__(self, model: Optional[str] = None):
        self._model = model or os.getenv("DISCOVERY_MODEL", _DEFAULT_MODEL)
        self._client = anthropic.Anthropic()

    def build_system_prompt(
        self,
        goal: str,
        params: Dict[str, str],
        outputs: List[Dict[str, str]],
        allowed_domains: List[str],
    ) -> str:
        param_lines = "\n".join(
            f"  - {name}: current value is {value!r}, ALWAYS write as {{{{{name}}}}}"
            for name, value in params.items()
        )
        output_lines = "\n".join(f"  - {o['name']} ({o.get('type', 'string')})" for o in outputs)
        return f"""You are driving a web browser to record a replayable automation workflow.

GOAL: {goal}

PARAMETERS — use template syntax in ALL actions, never the actual value:
{param_lines}

REQUIRED OUTPUTS — you must extract ALL of these before calling goal_complete:
{output_lines}

ALLOWED DOMAINS: {', '.join(allowed_domains)}

RULES:
1. NEVER use an actual parameter value in an action. Always use {{{{param_name}}}}.
   - fill a field → value: "{{{{member_id}}}}" (not "10001")
   - row identifier that is a param value → row_context: "{{{{member_id}}}}" (not "10001")
   - link text that includes a param value → accessible_name: "View Account {{{{member_id}}}}"
2. For table-based forms (cells act as labels), use row_context = adjacent cell text.
3. ALWAYS use `click` for links — never construct a URL and call `navigate` for a link.
   Example: seeing "link 'View Account 10001'" in the ARIA snapshot → call
   click(accessible_name="View Account {{{{member_id}}}}", role="link", row_context="{{{{member_id}}}}")
   NOT navigate(url="http://localhost:8080/account?member_id=10001")
4. When the ARIA snapshot shows a "--- Frame: name ---" section, elements inside it
   require frame_name="name" in extract calls.
5. After extracting ALL required outputs, call goal_complete immediately.
6. If the page shows a business-level outcome (member not found, account frozen, etc.),
   call goal_failed with the appropriate code and the page text that showed it.
"""

    def propose_action(self, messages: List[Dict], system: str) -> ProposedAction:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=messages,
        )

        for block in response.content:
            if block.type == "tool_use":
                # Keep text blocks + the one tool_use being processed.
                # Drop any extra tool_use blocks — if included in the message history
                # without matching tool_results they cause a 400 from the API.
                filtered = [
                    b for b in response.content
                    if getattr(b, "type", None) != "tool_use" or b.id == block.id
                ]
                return ProposedAction(
                    action=block.name,
                    inputs=dict(block.input),
                    tool_use_id=block.id,
                    response_content=filtered,
                )

        raise ValueError(f"LLM returned no tool_use block. Content: {response.content}")
