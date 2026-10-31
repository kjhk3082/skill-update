"""State shared by one invocation: options, what is already claimed by a channel, and notes for the reader."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, TypeVar

from .util import clean

T = TypeVar("T")
R = TypeVar("R")


class Ctx:
    def __init__(self, opts: Any) -> None:
        self.opts = opts
        self.claimed: Set[str] = set()              # real paths already handled by a channel
        self.unmanaged_notes: Dict[str, List[str]] = {}
        self.notes: List[str] = []                  # printed once at the end of a report
        self.lock_path: Optional[Path] = None
        self.lock_writable = False
        self.workers = 8

    def note(self, message: str) -> None:
        message = clean(message)
        if message not in self.notes:
            self.notes.append(message)

    def parallel(self, fn: Callable[[T], R], jobs: Sequence[T]) -> List[R]:
        """Run `fn` over `jobs` on a few threads, keeping the order of `jobs`."""
        if len(jobs) <= 1:
            return [fn(j) for j in jobs]
        with ThreadPoolExecutor(max_workers=min(self.workers, len(jobs))) as pool:
            return list(pool.map(fn, jobs))
