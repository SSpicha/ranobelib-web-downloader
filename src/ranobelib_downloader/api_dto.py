"""
DTO/normalization layer for RanobeLIB API responses.

Centralizes type coercion for fields that have drifted between API versions:
- summary: str | dict | list -> str
- name: str | dict | list -> str
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _to_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _to_str(value.get("content") or value.get("text") or "")
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            normalized = _to_str(item)
            if normalized:
                parts.append(normalized)
        return ", ".join(parts)
    return "" if value is None else str(value).strip()


def normalize_novel_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(raw)

    title_raw = out.get("rus_name") or out.get("eng_name") or out.get("name")
    if title_raw is None:
        title_raw = ""
    out["_normalized_title"] = _to_str(title_raw)

    out["summary"] = _to_str(out.get("summary", ""))

    for key in ("rus_name", "eng_name", "name"):
        out[key] = _to_str(out.get(key, ""))

    return out


def normalize_chapter(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(raw)
    out["name"] = _to_str(out.get("name", ""))
    out["number"] = _to_str(out.get("number", "0"))
    out["volume"] = _to_str(out.get("volume", "0"))
    return out
