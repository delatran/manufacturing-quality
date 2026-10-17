"""Curated analyst knowledge; never an archive of source rows."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

CATEGORIES = {"schema", "data_quality", "kpi", "report_preference", "methodology"}


def embedding(text: str, dimensions: int = 256) -> list[float]:
    """Offline hashed word + character features, not an OpenAI embedding."""
    terms = re.findall(r"\w+", text.lower())
    features = terms + [word[i : i + 3] for word in terms for i in range(max(0, len(word) - 2))]
    vector = [0.0] * dimensions
    for feature in features:
        digest = hashlib.sha256(feature.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % dimensions] += 1 if digest[4] & 1 else -1
    norm = math.sqrt(sum(x * x for x in vector)) or 1
    return [x / norm for x in vector]


class AnalystMemory:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / "memory.sqlite3"
        self.markdown_path = self.directory / "ANALYST_MEMORY.md"
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS knowledge (key TEXT PRIMARY KEY, category TEXT NOT NULL, content TEXT NOT NULL, embedding TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def store(self, key: str, content: str, category: str, *, update: bool = False) -> dict:
        if category not in CATEGORIES:
            raise ValueError(f"Memory category must be one of {sorted(CATEGORIES)}")
        if not re.fullmatch(r"[\w.-]{1,100}", key):
            raise ValueError(
                "Memory key must contain 1-100 letters, numbers, dots, underscores or hyphens"
            )
        if not content.strip() or len(content) > 2000:
            raise ValueError(
                "Memory content must be a concise knowledge note, at most 2000 characters"
            )
        if re.search(r"(?m)^\s*[{\[]|(?:[^\n]*[,\t][^\n]*\n){2}|\|[^\n]*\|\s*\n", content):
            raise ValueError(
                "Raw rows and structured record dumps cannot be stored in long-term memory"
            )
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute("SELECT key FROM knowledge WHERE key=?", (key,)).fetchone()
            if existing and not update:
                raise ValueError("Memory key already exists; use memory_update")
            if update and not existing:
                raise ValueError("Memory key does not exist; use memory_store")
            conn.execute(
                "INSERT OR REPLACE INTO knowledge VALUES (?,?,?,?,?)",
                (key, category, content.strip(), json.dumps(embedding(content)), now),
            )
        self._render()
        return {
            "key": key,
            "category": category,
            "content": content.strip(),
            "updated_at": now,
            "embedding_method": "offline_hashed_word_character_features",
        }

    def recall(self, query: str, limit: int = 8) -> dict:
        vector = embedding(query)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key,category,content,embedding,updated_at FROM knowledge"
            ).fetchall()
        scored = [
            {
                "key": key,
                "category": category,
                "content": content,
                "updated_at": updated,
                "score": sum(a * b for a, b in zip(vector, json.loads(encoded), strict=True)),
            }
            for key, category, content, encoded, updated in rows
        ]
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {
            "embedding_method": "offline_hashed_word_character_features",
            "items": scored[: max(1, min(limit, 20))],
        }

    def _render(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key,category,content FROM knowledge ORDER BY category,key"
            ).fetchall()
        text = "# Analyst memory\n\nCurated knowledge only. Retrieval uses offline hashed word and character features.\n"
        for key, category, content in rows:
            text += f"\n## {key} ({category})\n\n{content}\n"
        self.markdown_path.write_text(text, encoding="utf-8")
