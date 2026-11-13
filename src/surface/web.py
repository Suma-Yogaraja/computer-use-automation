from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from playwright.async_api import BrowserContext, Page

from src.models.artifact import Locator
from src.replay.locator import resolve
from src.surface.base import FrameSnapshot, ObservationResult, SurfaceAdapter


@dataclass
class DialogRecord:
    message: str
    dialog_type: str    # "alert", "confirm", "prompt", "beforeunload"
    accepted: bool = True


class WebAdapter(SurfaceAdapter):
    """Playwright-backed surface adapter for web UIs.

    Handles layout-table hostile apps, named iframes, and dialog interception.
    All locator resolution is delegated to src/replay/locator.py so both the
    discovery agent and the replay executor share the same resolution path.
    """

    def __init__(
        self,
        page: Page,
        context: Optional[BrowserContext] = None,
        known_dialog_patterns: Optional[List[str]] = None,
    ):
        self._page = page
        self._context = context
        self._dialogs: List[DialogRecord] = []
        self._unknown_dialogs: List[DialogRecord] = []
        self._known_dialog_patterns: List[str] = known_dialog_patterns or []
        page.on("dialog", self._handle_dialog)

    async def _handle_dialog(self, dialog) -> None:
        message = dialog.message
        if self._known_dialog_patterns:
            is_known = any(p.lower() in message.lower() for p in self._known_dialog_patterns)
            if not is_known:
                record = DialogRecord(message=message, dialog_type=dialog.type, accepted=False)
                self._dialogs.append(record)
                self._unknown_dialogs.append(record)
                await dialog.dismiss()
                return
        self._dialogs.append(DialogRecord(message=message, dialog_type=dialog.type, accepted=True))
        await dialog.accept()

    def pop_dialogs(self) -> List[DialogRecord]:
        """Return and clear all dialogs recorded since the last call."""
        dialogs = list(self._dialogs)
        self._dialogs.clear()
        return dialogs

    def pop_unknown_dialogs(self) -> List[DialogRecord]:
        """Return and clear dialogs that did not match any known pattern."""
        dialogs = list(self._unknown_dialogs)
        self._unknown_dialogs.clear()
        return dialogs

    # ------------------------------------------------------------------ SurfaceAdapter impl

    async def observe(self) -> ObservationResult:
        """Snapshot the current page state including all named iframes.

        Frame.aria_snapshot() doesn't exist in Playwright 1.60;
        use frame_locator("iframe[name=X]").locator("body").aria_snapshot() instead.
        """
        main_snapshot = await self._page.aria_snapshot()
        screenshot_png = await self._page.screenshot(full_page=True)
        url = self._page.url
        title = await self._page.title()

        frame_snapshots: List[FrameSnapshot] = []
        iframe_elements = await self._page.locator("iframe[name]").all()
        for iframe_el in iframe_elements:
            name = await iframe_el.get_attribute("name")
            if not name:
                continue
            try:
                fl = self._page.frame_locator(f"iframe[name='{name}']")
                yaml = await fl.locator("body").aria_snapshot()
                frame_snapshots.append(FrameSnapshot(
                    frame_name=name,
                    frame_path=[name],
                    yaml=yaml,
                ))
            except Exception:
                pass  # iframe not yet loaded or has no accessible content

        return ObservationResult(
            main_snapshot=main_snapshot,
            frame_snapshots=frame_snapshots,
            screenshot_png=screenshot_png,
            url=url,
            title=title,
        )

    async def navigate(self, url: str) -> None:
        await self._page.goto(url, wait_until="networkidle")

    async def click(self, locator: Locator) -> None:
        el = await resolve(locator, self._page)
        await el.click()

    async def fill(self, locator: Locator, value: str) -> None:
        el = await resolve(locator, self._page)
        await el.fill(value)

    async def extract(self, locator: Locator) -> str:
        el = await resolve(locator, self._page)
        return await el.inner_text()

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(full_page=True)

    async def current_url(self) -> str:
        return self._page.url

    async def wait_for_navigation(self, timeout_ms: int = 15000) -> None:
        await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)

    async def page_contains_text(self, text: str) -> bool:
        return await self._page.get_by_text(text, exact=False).count() > 0
