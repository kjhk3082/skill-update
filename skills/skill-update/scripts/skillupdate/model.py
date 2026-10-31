"""Plain data types passed between the scanner, the update channels and the reporter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Which channel owns an item, i.e. how the skill was installed.
KIND_LOCK = "lock"            # installed by `npx skills` (recorded in the skills lock file)
KIND_GIT = "git"              # a git clone that sits in a skills folder
KIND_PLUGIN = "plugin"        # a Claude Code plugin from a marketplace
KIND_UNMANAGED = "unmanaged"  # no upstream could be determined

# Where an item stands against its upstream.
OUTDATED = "outdated"    # a newer version exists and skill-update can apply it
CURRENT = "current"      # identical to upstream
BLOCKED = "blocked"      # a newer version exists but skill-update will not apply it by itself
UNKNOWN = "unknown"      # upstream could not be compared (moved, no version published, ...)
ERROR = "error"          # the check itself failed (network, auth, ...)
UNMANAGED = "unmanaged"  # nothing to compare against

STATUS_ORDER = [OUTDATED, BLOCKED, ERROR, UNKNOWN, CURRENT, UNMANAGED]


@dataclass
class Changes:
    """What an update would do to the files of one skill."""
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    def total(self) -> int:
        return len(self.added) + len(self.modified) + len(self.removed)

    def summary(self) -> str:
        return "+%d ~%d -%d" % (len(self.added), len(self.modified), len(self.removed))

    def to_dict(self, limit: int = 8) -> Dict[str, Any]:
        return {
            "added": len(self.added), "modified": len(self.modified), "removed": len(self.removed),
            "files": {"added": self.added[:limit], "modified": self.modified[:limit], "removed": self.removed[:limit]},
        }


@dataclass
class Item:
    """One thing that can be updated: a skill, or a plugin that ships skills."""
    name: str
    kind: str
    status: str
    source: str = ""
    agents: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    installed: str = ""
    latest: str = ""
    modified: bool = False
    changes: Optional[Changes] = None
    commits: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    compare_url: str = ""
    data: Dict[str, Any] = field(default_factory=dict, repr=False)  # channel-private; never printed

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name, "kind": self.kind, "status": self.status, "source": self.source,
            "agents": self.agents, "locations": self.locations,
            "installed": self.installed, "latest": self.latest, "modified": self.modified,
            "notes": self.notes,
        }
        if self.changes is not None:
            d["changes"] = self.changes.to_dict()
        if self.commits:
            d["commits"] = self.commits[:10]
        if self.compare_url:
            d["compare_url"] = self.compare_url
        return d


@dataclass
class Result:
    """What happened to one item during `update`."""
    name: str
    kind: str
    outcome: str  # updated | failed | skipped | dry-run
    detail: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "outcome": self.outcome, "detail": self.detail, "notes": self.notes}
