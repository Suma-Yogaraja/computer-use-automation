#!/usr/bin/env python3
"""
Smoke test for src/surface/web.py and src/replay/locator.py.

Exercises every locator strategy used by the hostile banking app and
confirms observe() returns iframe content via FrameLocator.
"""

import asyncio
import subprocess
import sys
import time

from playwright.async_api import async_playwright

from src.models.artifact import (
    ActionType, AnchorRelativeSpec, Locator, LocatorStrategy, RiskTier,
)
from src.replay.locator import LocatorResolutionError, resolve
from src.surface.web import WebAdapter

SERVER_URL = "http://localhost:8080"


def _pass(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


async def main() -> int:
    proc = subprocess.Popen(
        ["python3", "target_app/server.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    failures = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        adapter = WebAdapter(page, ctx)

        # ------------------------------------------------------------------ login
        await adapter.navigate(f"{SERVER_URL}/login")

        # ARIA_ROLE_NAME: fill username textbox
        username_loc = Locator(
            strategy=LocatorStrategy.ARIA_ROLE_NAME,
            value="",
            role="textbox",
            constraints={"visible": True, "enabled": True, "unique": False},
        )
        try:
            els = await resolve(username_loc, page)
        except Exception as exc:
            _fail("ARIA_ROLE_NAME textbox (no name)", str(exc))
            failures += 1

        # ANCHOR_RELATIVE: fill username by row anchor "Username"
        uname_anchor_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="textbox in Username row",
            role="textbox",
            anchor=AnchorRelativeSpec(
                target_role="textbox",
                row_cell_text="Username",
            ),
        )
        try:
            el = await resolve(uname_anchor_loc, page)
            await el.fill("operator")
            _pass("ANCHOR_RELATIVE: fill username by row cell text")
        except Exception as exc:
            _fail("ANCHOR_RELATIVE: fill username", str(exc))
            failures += 1

        # ANCHOR_RELATIVE: fill password
        pwd_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="textbox in Password row",
            role="textbox",
            anchor=AnchorRelativeSpec(
                target_role="textbox",
                row_cell_text="Password",
            ),
        )
        try:
            el = await resolve(pwd_loc, page)
            await el.fill("demo123")
            _pass("ANCHOR_RELATIVE: fill password by row cell text")
        except Exception as exc:
            _fail("ANCHOR_RELATIVE: fill password", str(exc))
            failures += 1

        # ARIA_ROLE_NAME: click Sign In button
        signin_loc = Locator(
            strategy=LocatorStrategy.ARIA_ROLE_NAME,
            value="Sign In",
            role="button",
        )
        try:
            el = await resolve(signin_loc, page)
            await el.click()
            await page.wait_for_url(f"{SERVER_URL}/dashboard")
            _pass("ARIA_ROLE_NAME: click Sign In button")
        except Exception as exc:
            _fail("ARIA_ROLE_NAME: click Sign In", str(exc))
            failures += 1

        # ------------------------------------------------------------------ dashboard
        # ANCHOR_RELATIVE: fill member ID field
        member_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="textbox in Member ID row",
            role="textbox",
            anchor=AnchorRelativeSpec(
                target_role="textbox",
                row_cell_text="Member ID:",
            ),
        )
        try:
            el = await resolve(member_loc, page)
            await el.fill("10001")
            _pass("ANCHOR_RELATIVE: fill member ID on dashboard")
        except Exception as exc:
            _fail("ANCHOR_RELATIVE: fill member ID", str(exc))
            failures += 1

        # ARIA_ROLE_NAME: click Search button
        search_loc = Locator(
            strategy=LocatorStrategy.ARIA_ROLE_NAME,
            value="Search",
            role="button",
        )
        try:
            el = await resolve(search_loc, page)
            await el.click()
            await page.wait_for_url(f"{SERVER_URL}/member/search", wait_until="networkidle")
            _pass("ARIA_ROLE_NAME: click Search button")
        except Exception as exc:
            _fail("ARIA_ROLE_NAME: click Search", str(exc))
            failures += 1

        # ------------------------------------------------------------------ search results — nested table
        # ANCHOR_RELATIVE with has_not=row: find link in innermost data row (C2)
        view_account_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="link in row with cell 10001",
            role="link",
            anchor=AnchorRelativeSpec(
                target_role="link",
                row_cell_text="10001",
            ),
        )
        try:
            el = await resolve(view_account_loc, page)
            link_text = await el.inner_text()
            assert "View Account 10001" in link_text, f"unexpected: {link_text!r}"
            _pass(f"ANCHOR_RELATIVE: nested table — link text = {link_text!r}")
        except AssertionError as exc:
            _fail("ANCHOR_RELATIVE: nested table link text", str(exc))
            failures += 1
        except Exception as exc:
            _fail("ANCHOR_RELATIVE: nested table (C2)", str(exc))
            failures += 1

        # VISIBLE_TEXT: click the View Account link by its text
        link_loc = Locator(
            strategy=LocatorStrategy.VISIBLE_TEXT,
            value="View Account 10001",
        )
        try:
            el = await resolve(link_loc, page)
            await el.click()
            await page.wait_for_url(f"{SERVER_URL}/account*", wait_until="networkidle")
            await page.wait_for_timeout(500)
            _pass("VISIBLE_TEXT: click View Account link")
        except Exception as exc:
            _fail("VISIBLE_TEXT: click View Account link", str(exc))
            failures += 1

        # ------------------------------------------------------------------ account page — iframe
        # ANCHOR_RELATIVE with column_header: extract Savings balance (C1 + C3)
        savings_balance_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="Savings balance cell",
            role="cell",
            frame_path=["account-frame"],
            anchor=AnchorRelativeSpec(
                target_role="cell",
                row_cell_text="Savings",
                column_header="Balance",
            ),
        )
        try:
            el = await resolve(savings_balance_loc, page)
            balance = await el.inner_text()
            assert balance == "$1,234.56", f"unexpected balance: {balance!r}"
            _pass(f"ANCHOR_RELATIVE in iframe: Savings balance = {balance!r}")
        except AssertionError as exc:
            _fail("ANCHOR_RELATIVE: savings balance value", str(exc))
            failures += 1
        except Exception as exc:
            _fail("ANCHOR_RELATIVE: savings balance (C1+C3 in iframe)", str(exc))
            failures += 1

        # observe() — confirm iframe snapshot included (C4)
        try:
            obs = await adapter.observe()
            assert obs.frame_snapshots, "No frame snapshots returned"
            iframe_snap = obs.frame_snapshots[0]
            assert "Savings" in iframe_snap.yaml, "Savings not in iframe YAML"
            assert "$1,234.56" in iframe_snap.yaml, "Balance not in iframe YAML"
            _pass(f"observe(): iframe snapshot included, frame_name={iframe_snap.frame_name!r}")
        except AssertionError as exc:
            _fail("observe(): iframe snapshot", str(exc))
            failures += 1
        except Exception as exc:
            _fail("observe()", str(exc))
            failures += 1

        # ------------------------------------------------------------------ ambiguity test
        # Confirm LOCATOR_AMBIGUOUS fires when unique=True and multiple match
        multi_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="cell in account-frame",
            role="cell",
            frame_path=["account-frame"],
            anchor=AnchorRelativeSpec(
                target_role="cell",
                row_cell_text="Savings",
                # no column_header → get_by_role("cell") returns multiple cells
            ),
            constraints={"visible": True, "enabled": True, "unique": True},
        )
        try:
            await resolve(multi_loc, page)
            _fail("LOCATOR_AMBIGUOUS expected", "no error raised — multiple cells not caught")
            failures += 1
        except LocatorResolutionError as exc:
            if exc.kind.value == "LOCATOR_AMBIGUOUS":
                _pass(f"LOCATOR_AMBIGUOUS: correctly raised for multi-cell row ({exc.message})")
            else:
                _fail("LOCATOR_AMBIGUOUS expected", f"got {exc.kind.value}: {exc.message}")
                failures += 1
        except Exception as exc:
            _fail("LOCATOR_AMBIGUOUS", str(exc))
            failures += 1

        # ------------------------------------------------------------------ not-found test
        missing_loc = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="textbox in NonExistent row",
            role="textbox",
            frame_path=["account-frame"],
            anchor=AnchorRelativeSpec(
                target_role="textbox",
                row_cell_text="NonExistent",
            ),
        )
        try:
            await resolve(missing_loc, page)
            _fail("LOCATOR_NOT_FOUND expected", "no error raised")
            failures += 1
        except LocatorResolutionError as exc:
            if exc.kind.value == "LOCATOR_NOT_FOUND":
                _pass(f"LOCATOR_NOT_FOUND: correctly raised for missing row")
            else:
                _fail("LOCATOR_NOT_FOUND expected", f"got {exc.kind.value}: {exc.message}")
                failures += 1

        # ------------------------------------------------------------------ dialog test
        await page.goto(f"{SERVER_URL}/dashboard", wait_until="networkidle")
        member_loc2 = Locator(
            strategy=LocatorStrategy.ANCHOR_RELATIVE,
            value="textbox in Member ID row",
            role="textbox",
            anchor=AnchorRelativeSpec(target_role="textbox", row_cell_text="Member ID:"),
        )
        el = await resolve(member_loc2, page)
        await el.fill("99995")
        el2 = await resolve(search_loc, page)
        await el2.click()
        await page.wait_for_load_state("networkidle")

        dialogs = adapter.pop_dialogs()
        if dialogs:
            _pass(f"Dialog captured: type={dialogs[0].dialog_type!r} message={dialogs[0].message!r}")
        else:
            _fail("Dialog capture", "no dialog recorded for member 99995")
            failures += 1

        await browser.close()

    proc.terminate()

    print()
    if failures == 0:
        print("All tests passed.")
    else:
        print(f"{failures} test(s) FAILED.")
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
