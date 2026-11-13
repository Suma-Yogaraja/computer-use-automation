from __future__ import annotations

import fnmatch
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import BrowserContext

from src.models.artifact import RiskTier, Step
from src.models.policy import AllowlistPolicy
from src.models.result import FailureKind


class PolicyViolation(Exception):
    """Raised when an action is refused before it reaches the application."""
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class EscalationRequired(Exception):
    """Raised when an IRREVERSIBLE step must pause for human approval.

    This is not a failure — it enters the OperatorChannel handoff flow.
    Distinct from PolicyViolation: escalation is a workflow transition,
    policy denial is a hard stop.
    """
    def __init__(self, step_id: str, reason: str):
        super().__init__(reason)
        self.step_id = step_id
        self.reason = reason


class PolicyGate:
    """Pre-action checks run before Playwright executes anything.

    Three-tier classification:
      READ              → always allowed (no network side effects)
      MUTATING_EXPECTED → allowed if the step declared expected_method + expected_route
      IRREVERSIBLE      → raises EscalationRequired, never auto-executed
    """

    def __init__(self, policy: AllowlistPolicy):
        self._policy = policy

    def check_navigate(self, url: str) -> None:
        if not _is_allowed_url(url, self._policy):
            raise PolicyViolation(
                FailureKind.POLICY_DENIED,
                f"Navigate target outside allowlist: {url}",
            )

    def check_step(self, step: Step, accessible_name: str = "") -> None:
        """Check risk tier and accessible name before the action runs.

        Raises EscalationRequired for IRREVERSIBLE tiers or matching name patterns.
        Never raises for READ or MUTATING_EXPECTED tiers — the RouteGuard handles
        those at the network layer.
        """
        if step.risk_tier == RiskTier.IRREVERSIBLE:
            raise EscalationRequired(
                step.step_id,
                f"Step {step.step_id!r} has IRREVERSIBLE risk tier — requires human approval",
            )

        if accessible_name:
            name_lower = accessible_name.lower()
            for pattern in self._policy.action.irreversible_name_patterns:
                if pattern.lower() in name_lower:
                    raise EscalationRequired(
                        step.step_id,
                        f"Accessible name {accessible_name!r} matches irreversible pattern {pattern!r}",
                    )


class RouteGuard:
    """Network-layer policy enforcement installed via context.route().

    Covers all requests including those from iframes, redirects, and human
    actions during operator handoff. Two enforcement modes:

    - Automation mode (default): non-GET requests are only allowed when
      expect_mutation() was called with a matching method + route pattern.
      Any other non-GET is aborted and recorded as an unexpected mutation.
    - Human mode: mutations are allowed freely (operator is in control),
      but the domain/route allowlist still applies.
    """

    def __init__(self, policy: AllowlistPolicy):
        self._policy = policy
        self._expected_method: Optional[str] = None
        self._expected_route: Optional[str] = None
        self._allowlist_violations: List[dict] = []
        self._unexpected_mutations: List[dict] = []
        self._human_mode: bool = False

    def expect_mutation(self, method: str, route: str) -> None:
        """Register the one mutation expected for the upcoming step.

        Call before the action that triggers the network request.
        Clear with clear_expected() after the step completes.
        """
        self._expected_method = method.upper()
        self._expected_route = route

    def clear_expected(self) -> None:
        self._expected_method = None
        self._expected_route = None

    def set_human_mode(self, enabled: bool) -> None:
        """Enable/disable human mode. In human mode, only the allowlist is enforced."""
        self._human_mode = enabled

    async def install(self, context: BrowserContext) -> None:
        await context.route("**/*", self._handler)

    async def _handler(self, route) -> None:
        req = route.request
        url = req.url
        method = req.method.upper()

        if urlparse(url).scheme in ("data", "blob", "about"):
            await route.continue_()
            return

        if not _is_allowed_url(url, self._policy):
            self._allowlist_violations.append({"url": url, "method": method})
            await route.abort()
            return

        if method != "GET":
            if self._human_mode:
                await route.continue_()
                return

            path = urlparse(url).path
            is_expected = (
                self._expected_method == method
                and self._expected_route is not None
                and fnmatch.fnmatch(path, self._expected_route)
            )
            if not is_expected:
                self._unexpected_mutations.append({"url": url, "method": method})
                await route.abort()
                return

        await route.continue_()

    def take_allowlist_violations(self) -> List[dict]:
        """Return and clear domain/route allowlist violations since last call."""
        v = list(self._allowlist_violations)
        self._allowlist_violations.clear()
        return v

    def take_unexpected_mutations(self) -> List[dict]:
        """Return and clear unexpected non-GET requests that were aborted."""
        m = list(self._unexpected_mutations)
        self._unexpected_mutations.clear()
        return m


def _is_allowed_url(url: str, policy: AllowlistPolicy) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme in ("data", "blob", "about"):
        return True

    hostname = parsed.hostname or ""
    port = parsed.port
    host_with_port = f"{hostname}:{port}" if port else hostname

    domain_ok = any(
        fnmatch.fnmatch(host_with_port, pat) or fnmatch.fnmatch(hostname, pat)
        for pat in policy.allowed_domains
    )
    if not domain_ok:
        return False

    path = parsed.path or "/"

    if any(fnmatch.fnmatch(path, pat) for pat in policy.blocked_routes):
        return False

    if policy.allowed_routes:
        if not any(fnmatch.fnmatch(path, pat) for pat in policy.allowed_routes):
            return False

    return True
