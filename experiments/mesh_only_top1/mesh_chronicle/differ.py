"""Tiny structural diff for response payloads.

Used by `chronicle.replay_diff` to compare a captured response against
a freshly-replayed one. We don't pull in deepdiff — a few dozen lines
of recursive comparison is enough for the demo.
"""
from __future__ import annotations

from typing import Any


def diff(a: Any, b: Any, path: str = "$") -> list[dict]:
    out: list[dict] = []
    if type(a) is not type(b):
        out.append({"path": path, "kind": "type_changed",
                    "old": _typename(a), "new": _typename(b),
                    "old_value": a, "new_value": b})
        return out
    if isinstance(a, dict):
        for k in sorted(set(a.keys()) | set(b.keys())):
            sub = f"{path}.{k}"
            if k not in a:
                out.append({"path": sub, "kind": "added", "new_value": b[k]})
            elif k not in b:
                out.append({"path": sub, "kind": "removed", "old_value": a[k]})
            else:
                out.extend(diff(a[k], b[k], sub))
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            out.append({"path": path, "kind": "list_length",
                        "old_len": len(a), "new_len": len(b)})
        for i in range(min(len(a), len(b))):
            out.extend(diff(a[i], b[i], f"{path}[{i}]"))
        return out
    if a != b:
        out.append({"path": path, "kind": "value_changed",
                    "old_value": a, "new_value": b})
    return out


def _typename(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__
