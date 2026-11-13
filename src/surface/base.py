from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FrameSnapshot:
    frame_name: str
    frame_path: List[str]   # e.g. ["account-frame"]
    yaml: str


@dataclass
class ObservationResult:
    main_snapshot: str
    frame_snapshots: List[FrameSnapshot] = field(default_factory=list)
    screenshot_png: bytes = b""
    url: str = ""
    title: str = ""


class SurfaceAdapter(ABC):

    @abstractmethod
    async def observe(self) -> ObservationResult: ...

    @abstractmethod
    async def navigate(self, url: str) -> None: ...

    @abstractmethod
    async def click(self, locator) -> None: ...

    @abstractmethod
    async def fill(self, locator, value: str) -> None: ...

    @abstractmethod
    async def extract(self, locator) -> str: ...

    @abstractmethod
    async def screenshot(self) -> bytes: ...

    @abstractmethod
    async def current_url(self) -> str: ...

    @abstractmethod
    async def page_contains_text(self, text: str) -> bool: ...
