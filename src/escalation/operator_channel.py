from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, Page

from src.models.result import InterventionRecord, InterventionRequest
from src.observability.logger import EventType, RunLogger


class OperatorChannel:
    """Hands browser control to a human when NEEDS_HUMAN is raised during replay.

    Flow:
      1. Screenshot saved to evidence/<evidence_dir>/screenshots/
      2. Intervention details printed to stdout
      3. Action capture JS injected into current page and future pages
      4. CLI prompt raced against ownership lease timer
      5. Caller handles the returned signal:
           retry   → re-execute the same step (page must be ready)
           done    → verify postconditions, advance to next step
           abort   → return FAILED immediately
           timeout → return ESCALATION_TIMEOUT

    Inject signal_provider for testing to avoid blocking on stdin.
    signal_provider is an async callable with no arguments returning a raw string
    ("R", "D", "A", or any string — unknown input defaults to "abort").
    """

    def __init__(
        self,
        evidence_dir: Path,
        logger: RunLogger,
        lease_seconds: int = 300,
        signal_provider: Optional[Callable[[], Coroutine[Any, Any, str]]] = None,
    ):
        self._evidence_dir = evidence_dir
        self._logger = logger
        self._lease_seconds = lease_seconds
        self._signal_provider = signal_provider or OperatorChannel._stdin_provider

    @staticmethod
    async def _stdin_provider() -> str:
        """Read one line from stdin without blocking the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input)

    async def request_handoff(
        self,
        request: InterventionRequest,
        context: BrowserContext,
        page: Page,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """Present the intervention to the operator and wait for their signal.

        Returns (signal, human_actions) where:
          signal        — "retry" | "done" | "abort" | "timeout"
          human_actions — scrubbed list of click/submit events observed while human was in control
        """
        # Save screenshot
        shots_dir = self._evidence_dir / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        shot_path = shots_dir / f"{request.step_id}_escalation.png"
        try:
            png = await page.screenshot(full_page=True)
            shot_path.write_bytes(png)
        except Exception:
            pass

        # Track non-GET requests at the context level — survives page navigations
        context_requests: List[Dict[str, Any]] = []

        def _on_context_request(req) -> None:
            if req.method.upper() != "GET":
                context_requests.append({
                    "type": "request",
                    "method": req.method,
                    "url": req.url,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })

        context.on("request", _on_context_request)

        # Print the intervention prompt
        sep = "=" * 62
        print(f"\n{sep}")
        print("  ESCALATION — HUMAN ACTION REQUIRED")
        print(f"  Run:    {request.run_id}")
        print(f"  Step:   {request.step_id}")
        print(f"  Reason: {request.reason}")
        print(f"  URL:    {request.current_url}")
        print(f"  Photo:  {shot_path}")
        print(sep)
        print(f"  Browser is live.  R=retry  D=done  A=abort  then Enter")
        print(f"  Lease expires in {self._lease_seconds}s")
        print("> ", end="", flush=True)

        self._logger.log(
            EventType.ESCALATION_START,
            step_id=request.step_id,
            reason=request.reason,
            url=request.current_url,
        )

        signal = await self._race_signal()

        context.remove_listener("request", _on_context_request)
        human_actions: List[Dict[str, Any]] = list(context_requests)

        self._logger.log(
            EventType.ESCALATION_END,
            step_id=request.step_id,
            signal=signal,
            human_actions_count=len(human_actions),
        )

        return signal, human_actions

    async def _race_signal(self) -> str:
        """Race the signal provider against the lease timer."""
        async def read() -> str:
            raw = (await self._signal_provider()).strip().upper()
            if raw in ("R", "RETRY"):
                return "retry"
            if raw in ("D", "DONE"):
                return "done"
            if raw in ("A", "ABORT"):
                return "abort"
            return "abort"

        try:
            return await asyncio.wait_for(read(), timeout=self._lease_seconds)
        except asyncio.TimeoutError:
            return "timeout"

    @staticmethod
    def build_intervention_record(
        request: InterventionRequest,
        signal: str,
        human_actions: List[Dict[str, Any]],
        postconditions_verified: bool,
    ) -> InterventionRecord:
        return InterventionRecord(
            request=request,
            signal=signal,
            human_actions=human_actions,
            resumed_at=datetime.now(timezone.utc),
            postconditions_verified=postconditions_verified,
        )
