"""Project public provider summaries without changing their source language."""

from __future__ import annotations

import re
from typing import Any

from .localization import text_language

_NUMERIC_TOKEN = re.compile(r"(?<!\w)[+-]?\d(?:[\d.,]*\d)?(?:[eE][+-]?\d+)?%?")
_PROTECTED_TOKEN = re.compile(
    r"```[\s\S]*?```|`[^`\n]+`|\[claim:(?:[^\[\]\n]|\[\d+\])+\]|https?://[^\s<>]+"
    r"|(?<!\w)(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*(?:\[\d+\])*"
    r"|(?<!\w)[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"
    r"|(?<!\w)(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
)
_VIETNAMESE_DIACRITIC = re.compile(
    "[ăâđêôơưĂÂĐÊÔƠƯàáạảãằắặẳẵầấậẩẫèéẹẻẽềếệểễìíịỉĩòóọỏõồốộổỗờớợởỡùúụủũừứựửữỳýỵỷỹ]"
)


def summary_language(text: str) -> str | None:
    """Describe short public prose without changing question-language decisions."""
    detected = text_language(text)
    if detected is not None:
        return detected
    prose = _NUMERIC_TOKEN.sub(" ", _PROTECTED_TOKEN.sub(" ", text))
    words = re.findall(r"[^\W\d_]+", prose, re.UNICODE)
    if words and _VIETNAMESE_DIACRITIC.search(" ".join(words)):
        return "vi"
    return None


class PublicSummaryDisplay:
    """Accumulate source snapshots and retain native replay identity mappings."""

    def __init__(self) -> None:
        self.parts: dict[tuple, str] = {}
        self.aliases: dict[tuple, str] = {}
        self.summary_indices: dict[tuple, tuple[int, ...]] = {}

    def key(self, data: dict[str, Any]) -> tuple:
        session = data.get("provider_session_id") or data.get("session_id")
        turn = data.get("provider_turn_id") or data.get("turn_id") or data.get("response_id")
        identity = data.get("item_id")
        index = data.get("summary_index", 0)
        if identity is not None:
            alias_key = session, turn, identity
            indices = self.summary_indices.get(alias_key)
            if indices is None:
                identity = self.aliases.get(alias_key, identity)
            elif type(index) is int and 0 <= index < len(indices):
                identity = self.aliases[alias_key]
                index = indices[index]
            item = ("item", identity)
        else:
            item = ("output", data.get("output_index"))
        return session, turn, item, index

    def alias(self, data: dict[str, Any]) -> None:
        if data.get("reconciled") is not True or not data.get("canonical_item_id"):
            return
        indices = data.get("canonical_summary_indices")
        if indices is not None and (
            not isinstance(indices, list)
            or not indices
            or any(type(index) is not int or index < 0 for index in indices)
            or len(set(indices)) != len(indices)
        ):
            return
        session, turn, _item, _index = self.key(data)
        source_item = data.get("item_id")
        prior_parts = [
            (key, text)
            for key, text in self.parts.items()
            if key[:2] == (session, turn) and key[2] == ("item", source_item)
        ]
        alias_key = session, turn, source_item
        self.aliases[alias_key] = data["canonical_item_id"]
        if indices is not None:
            self.summary_indices[alias_key] = tuple(indices)
        # A replay can finish under a transient ID before the durable identity
        # arrives. Keep that source snapshot available to later native deltas.
        for key, text in prior_parts:
            canonical_key = self.key({**data, "summary_index": key[3]})
            self.parts.setdefault(canonical_key, text)

    def project(self, data: dict[str, Any]) -> dict[str, Any]:
        key = self.key(data)
        identified = any(value is not None for value in (key[0], key[1], key[2][1]))
        previous = self.parts.get(key, "") if identified else ""
        if isinstance(data.get("delta"), str):
            text = previous + data["delta"]
        elif isinstance(data.get("text"), str) and data["text"]:
            text = data["text"]
        else:
            text = previous
        if identified:
            self.parts[key] = text
        return {
            **data,
            "display_text": text,
            "display_mode": "snapshot",
            "display_localized": False,
            "display_source": "provider_summary",
            "display_language": summary_language(text),
        }
