"""Fetch the required HTTP source in memory with bounded retries and validation.

Raw defect JSON is never cached or read from disk. A failed fetch/schema check
raises SourceError so the service can retain its previous complete snapshot.
"""

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import REQUIRED_EVENT_FIELDS, SOURCE_URL

MAX_SOURCE_BYTES = 10 * 1024 * 1024


class SourceError(RuntimeError):
    """Source transport, content or schema could not be trusted."""


def fetch_events(
    url: str = SOURCE_URL,
    *,
    transport: httpx.BaseTransport | None = None,
    attempts: int = 3,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return validated JSON records and byte-level provenance, without files."""
    if not 1 <= attempts <= 3:
        raise ValueError("attempts must be between 1 and 3")
    timeout = httpx.Timeout(15.0, connect=5.0, pool=5.0)
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for attempt in range(attempts):
            try:
                with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                    response.raise_for_status()
                    parts: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > MAX_SOURCE_BYTES:
                            raise SourceError("HTTP payload exceeds the 10 MiB source limit")
                        parts.append(chunk)
                    payload = b"".join(parts)
                    etag = response.headers.get("etag")
                    content_type = response.headers.get("content-type", "")
                try:
                    records = json.loads(payload)
                except (ValueError, UnicodeError) as exc:
                    raise SourceError("HTTP source is not valid JSON") from exc
                if not isinstance(records, list) or not records:
                    raise SourceError("HTTP source must be a nonempty JSON array")
                for index, row in enumerate(records, start=1):
                    if not isinstance(row, dict):
                        raise SourceError(f"Source row {index} is not a JSON object")
                    missing = sorted(set(REQUIRED_EVENT_FIELDS) - set(row))
                    if missing:
                        raise SourceError(
                            f"Source row {index} is missing fields: {', '.join(missing)}"
                        )

                def value_type(value: Any) -> str:
                    if value is None:
                        return "null"
                    if isinstance(value, bool):
                        return "boolean"
                    if isinstance(value, (int, float)):
                        return "number"
                    if isinstance(value, str):
                        return "string"
                    return "object" if isinstance(value, dict) else "array"

                schema = {
                    field: sorted({value_type(row.get(field)) for row in records})
                    for field in sorted({key for row in records for key in row})
                }
                return records, {
                    "source_url": url,
                    "source_sha256": hashlib.sha256(payload).hexdigest(),
                    "source_records_sha256": hashlib.sha256(
                        json.dumps(
                            records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode("utf-8")
                    ).hexdigest(),
                    "source_bytes": len(payload),
                    "source_etag": etag,
                    "source_content_type": content_type,
                    "source_schema": {
                        "container": "array",
                        "row_count": len(records),
                        "required_fields": list(REQUIRED_EVENT_FIELDS),
                        "observed_field_types": schema,
                    },
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "http_status": response.status_code,
                    "http_attempts": attempt + 1,
                }
            except httpx.HTTPStatusError as exc:
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
                if not retryable or attempt == attempts - 1:
                    raise SourceError(
                        f"HTTP source returned status {exc.response.status_code}"
                    ) from exc
            except httpx.RequestError as exc:
                if attempt == attempts - 1:
                    raise SourceError(f"HTTP source request failed: {type(exc).__name__}") from exc
            if attempt < attempts - 1:
                time.sleep(0.25 * (2**attempt))
    raise SourceError("HTTP source could not be fetched")
