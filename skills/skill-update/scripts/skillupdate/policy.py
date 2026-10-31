"""Which items an `update` run touches.

This is the one place that encodes the update policy. Everything else just carries it out.
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from .model import KIND_PLUGIN, OUTDATED, UNKNOWN, Item


def matches(item: Item, names: Sequence[str]) -> bool:
    """A name on the command line selects a skill by name, or every skill from one source."""
    return any(n == item.name or n == item.source or item.source.endswith("/" + n) for n in names)


def select(items: Sequence[Item], opts: Any) -> Tuple[List[Item], List[Tuple[Item, str]]]:
    """Split items into (to update, skipped with a reason).

    - Outdated skills are updated. A skill whose files differ from what was installed (edited by
      hand, or drifted) is overwritten after a backup, unless --keep-modified is given and the skill
      was not named explicitly.
    - Plugins whose catalog does not publish a version are handed to Claude Code, which knows
      whether a newer one exists.
    """
    names = list(opts.names)
    chosen: List[Item] = []
    skipped: List[Tuple[Item, str]] = []
    for it in items:
        explicit = bool(names) and matches(it, names)
        if names and not explicit:
            continue
        if it.status == OUTDATED:
            if it.modified and opts.keep_modified and not explicit:
                skipped.append((it, "changed locally; kept because of --keep-modified"))
            else:
                chosen.append(it)
        elif it.kind == KIND_PLUGIN and it.status == UNKNOWN:
            chosen.append(it)
    return chosen, skipped
