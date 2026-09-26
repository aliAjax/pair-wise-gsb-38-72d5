"""字幕版本之间的句子级差异计算。

该模块不依赖数据库和 HTTP 层：输入基线字幕与当前字幕（均为 dict 列表，
字段为 cue_index/start_ms/end_ms/text），按字幕序号对齐后输出新增、改写、
移除和未变化的句子清单，可单独维护和测试。
"""
from __future__ import annotations

from typing import Any

# 参与“改写”判定的字段；序号本身用于对齐，不算改写。
_FIELDS = ("start_ms", "end_ms", "text")


def _public(cue: dict[str, Any]) -> dict[str, Any]:
    return {
        "cue_index": int(cue["cue_index"]),
        "start_ms": int(cue["start_ms"]),
        "end_ms": int(cue["end_ms"]),
        "text": str(cue["text"]),
    }


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [field for field in _FIELDS if before[field] != after[field]]


def compute_diff(baseline: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    """计算当前字幕相对基线字幕的差异。

    对齐键是 cue_index：基线有而当前没有视为移除，当前有而基线没有视为新增，
    两边都有但时间或文本不同视为改写。返回结构稳定，方便页面直接渲染。
    """
    base_by_index = {int(c["cue_index"]): c for c in baseline}
    curr_by_index = {int(c["cue_index"]): c for c in current}
    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    unchanged = 0
    for index in sorted(curr_by_index):
        cur = curr_by_index[index]
        base = base_by_index.get(index)
        if base is None:
            added.append(_public(cur))
            continue
        fields = _changed_fields(base, cur)
        if fields:
            modified.append({
                "cue_index": index,
                "changes": fields,
                "before": _public(base),
                "after": _public(cur),
            })
        else:
            unchanged += 1
    for index in sorted(base_by_index):
        if index not in curr_by_index:
            removed.append(_public(base_by_index[index]))
    return {"added": added, "modified": modified, "removed": removed, "unchanged": unchanged}
