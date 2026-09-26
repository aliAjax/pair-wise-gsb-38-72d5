"""Pure cue-diff calculation for revision versions.

This module is intentionally free of database and HTTP dependencies so the
matching rules can be unit tested on their own. The persistence layer
(app.py) and the pages (static/) must not reimplement this logic: pages
render the result of ``diff_cues`` verbatim.

Cues are matched by ``cue_index`` (the stable per-version cue number):

- added:     index exists only in the child (revision) version
- removed:   index exists only in the parent version
- modified:  index exists in both but text and/or timing changed
- unchanged: index exists in both with identical text and timing
"""
from __future__ import annotations

from typing import Any

CUE_FIELDS = ("cue_index", "start_ms", "end_ms", "text")


def _key(cue: dict[str, Any]) -> int:
    return int(cue["cue_index"])


def _view(cue: dict[str, Any]) -> dict[str, Any]:
    """Project a cue row down to the fields that make up a sentence."""
    return {field: cue[field] for field in CUE_FIELDS}


def _timing(cue: dict[str, Any]) -> tuple[int, int]:
    return int(cue["start_ms"]), int(cue["end_ms"])


def diff_cues(parent_cues: list[dict[str, Any]],
              child_cues: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two cue lists and return added/removed/modified entries."""
    parent = {_key(cue): cue for cue in parent_cues}
    child = {_key(cue): cue for cue in child_cues}

    added = [_view(child[k]) for k in sorted(child.keys() - parent.keys())]
    removed = [_view(parent[k]) for k in sorted(parent.keys() - child.keys())]

    modified: list[dict[str, Any]] = []
    unchanged: list[int] = []
    for key in sorted(parent.keys() & child.keys()):
        before, after = parent[key], child[key]
        changes: list[str] = []
        if before["text"] != after["text"]:
            changes.append("text")
        if _timing(before) != _timing(after):
            changes.append("timing")
        if changes:
            modified.append({
                "cue_index": key,
                "before": _view(before),
                "after": _view(after),
                "changes": changes,
            })
        else:
            unchanged.append(key)

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_indexes": unchanged,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(unchanged),
        },
    }
