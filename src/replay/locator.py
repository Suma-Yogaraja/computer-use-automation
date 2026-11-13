from __future__ import annotations

from typing import Any, List, Optional

from src.models.artifact import AnchorRelativeSpec, Locator, LocatorStrategy
from src.models.result import FailureKind


class LocatorResolutionError(Exception):
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


async def resolve(loc: Locator, page: Any) -> Any:
    """Resolve a Locator model to a live Playwright locator.

    Raises LocatorResolutionError if the element cannot be found uniquely.
    The page argument is always the top-level Page; frame_path in the locator
    determines whether resolution descends into an iframe.
    """
    root = _get_root(page, loc.frame_path)

    if loc.strategy == LocatorStrategy.ARIA_ROLE_NAME:
        el = _resolve_aria_role_name(loc, root)
    elif loc.strategy == LocatorStrategy.LABEL_ASSOCIATION:
        el = root.get_by_label(loc.value, exact=True)
    elif loc.strategy == LocatorStrategy.ANCHOR_RELATIVE:
        el = await _resolve_anchor_relative(loc, root)
    elif loc.strategy == LocatorStrategy.VISIBLE_TEXT:
        el = root.get_by_text(loc.value, exact=True)
    elif loc.strategy == LocatorStrategy.SEMANTIC_CSS:
        el = root.locator(loc.value)
    elif loc.strategy == LocatorStrategy.RELATIVE_XPATH:
        el = root.locator(f"xpath={loc.value}")
    else:
        raise LocatorResolutionError(
            FailureKind.LOCATOR_NOT_FOUND,
            f"Unsupported locator strategy: {loc.strategy}",
        )

    await _validate_constraints(el, loc)
    return el


def _get_root(page: Any, frame_path: Optional[List[str]]) -> Any:
    """Walk frame_path to get the correct root for locator resolution.

    "main" segments are skipped. Each other segment is treated as an iframe name.
    """
    if not frame_path:
        return page
    root = page
    for segment in frame_path:
        if segment == "main":
            continue
        root = root.frame_locator(f"iframe[name='{segment}']")
    return root


def _resolve_aria_role_name(loc: Locator, root: Any) -> Any:
    if not loc.role:
        raise LocatorResolutionError(
            FailureKind.SCHEMA_VIOLATION,
            "ARIA_ROLE_NAME locator requires the role field",
        )
    kwargs: dict = {}
    if loc.value:
        kwargs["name"] = loc.value
    return root.get_by_role(loc.role, **kwargs)


async def _resolve_anchor_relative(loc: Locator, root: Any) -> Any:
    """Find an element by its position relative to a known cell in a table row.

    Applies has_not=row to reach the innermost data row: nested tables cause
    the outer row to also "contain" the inner cell, so filtering out rows
    that contain descendant rows isolates the leaf row.
    """
    anchor: AnchorRelativeSpec = loc.anchor

    if not anchor.row_cell_text:
        raise LocatorResolutionError(
            FailureKind.SCHEMA_VIOLATION,
            "ANCHOR_RELATIVE locator requires anchor.row_cell_text",
        )

    row = root.get_by_role("row").filter(
        has=root.get_by_role("cell", name=anchor.row_cell_text, exact=True)
    ).filter(has_not=root.get_by_role("row"))

    row_count = await row.count()
    if row_count == 0:
        raise LocatorResolutionError(
            FailureKind.LOCATOR_NOT_FOUND,
            f"No row found containing cell {anchor.row_cell_text!r}",
        )
    if row_count > 1:
        raise LocatorResolutionError(
            FailureKind.LOCATOR_AMBIGUOUS,
            f"{row_count} rows contain cell {anchor.row_cell_text!r} — cannot isolate uniquely",
        )

    if anchor.column_header:
        col_idx = await _resolve_column_index(root, anchor.column_header)
        return row.get_by_role("cell").nth(col_idx)

    return row.get_by_role(anchor.target_role)


async def _resolve_column_index(root: Any, column_header: str) -> int:
    """Find the zero-based index of a column by walking the header row.

    Uses semantic header resolution instead of .nth(N) so column reordering
    doesn't silently break extraction.
    """
    header_row = root.get_by_role("row").filter(
        has=root.get_by_role("cell", name=column_header, exact=True)
    ).filter(has_not=root.get_by_role("row"))

    header_count = await header_row.count()
    if header_count == 0:
        raise LocatorResolutionError(
            FailureKind.LOCATOR_NOT_FOUND,
            f"No header row found containing column {column_header!r}",
        )

    cells = header_row.get_by_role("cell")
    cell_count = await cells.count()
    for i in range(cell_count):
        text = (await cells.nth(i).inner_text()).strip()
        if text == column_header:
            return i

    raise LocatorResolutionError(
        FailureKind.LOCATOR_NOT_FOUND,
        f"Column {column_header!r} not found among {cell_count} header cells",
    )


async def _validate_constraints(el: Any, loc: Locator) -> None:
    constraints = loc.constraints

    if constraints.get("unique", True):
        count = await el.count()
        if count == 0:
            raise LocatorResolutionError(
                FailureKind.LOCATOR_NOT_FOUND,
                f"Locator resolved to 0 elements [{loc.strategy.value} {loc.value!r}]",
            )
        if count > 1:
            raise LocatorResolutionError(
                FailureKind.LOCATOR_AMBIGUOUS,
                f"Locator resolved to {count} elements (expected 1) [{loc.strategy.value} {loc.value!r}]",
            )
