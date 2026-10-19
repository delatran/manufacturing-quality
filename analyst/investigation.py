"""Validate owner edits without promoting them to backend-verified findings."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .evidence import EvidenceLedger, canonical, sha256
from .localization import normalize_language, report_number_format
from .localization import response_language as response_language
from .store import SessionStore


def plain_text(value):
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", value.lower().replace("đ", "d"))
        if not unicodedata.combining(char)
    )


def _scope_prose(text, options):
    """Separate authored intent from source blocks and quoted parameter labels."""
    source = str(text)
    lines = []
    fence = None
    blockquote = False
    for line in source.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not line[marker.end() :].strip()
            ):
                fence = None
            lines.append("")
            continue
        if marker:
            fence = marker[1]
            lines.append("")
            continue
        if not line.strip():
            blockquote = False
        if re.match(r"^\s*>", line):
            blockquote = True
        if blockquote or re.match(r"^(?: {4}|\t)", line):
            lines.append("")
        else:
            lines.append(line)
    prose = "\n".join(lines)
    prose = re.sub(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)", " ", prose)
    values = {
        plain_text(value).strip()
        for choices in options.values()
        if isinstance(choices, list)
        for value in choices
        if isinstance(value, str)
    }
    values.update(("nghiem trong", "trung binh", "nhe"))
    for month, name in enumerate(("january", "february", "march", "april", "may", "june"), 1):
        values.update((name, f"thang {month}", f"2024-{month:02d}"))
    prefix = "scopeparametervalue"
    while prefix in plain_text(source):
        prefix += "x"
    literals: dict[str, str] = {}

    def quoted(match):
        if match[0][-1] != {'"': '"', "“": "”", "‘": "’", "'": "'", "`": "`"}[match[0][0]]:
            return " "
        value = plain_text(match[0][1:-1]).strip()
        if value not in values:
            return " "
        token = prefix + str(len(literals)) + "token"
        literals[token] = value
        return token

    # A label may name a filter, but its text cannot supply a verb or prohibition.
    prose = re.sub(
        r'"[^"]*(?:"|$)|“[^”]*(?:”|$)|‘(?:[^’]|’(?=\w))*(?:’|$)'
        r"|(?<!\w)'(?:[^']|'(?=\w))*(?:'(?!\w)|$)|`[^`]*(?:`|$)",
        quoted,
        prose,
    )
    return plain_text(prose), literals


def scope_grants(text, options):
    """Require direct UI intent as well as named scope values before mutation."""
    normalized, literals = _scope_prose(text, options)
    clauses = re.split(r"[.;!?\n]|\b(?:but|nhung)\b", normalized)
    negation = r"\b(?:do not|don['’]?t|dont|never|khong duoc|khong|dung)\b"
    controls = r"\b(?:change|modify|apply|switch|show|display|open|set|select|filter|reset|restore|undo|revert|doi|thay doi|ap dung|loc|mo|hien thi|chon|chuyen|dat|khoi phuc|hoan tac|quay ve|quay lai)\b"
    negative_clauses = [
        clause for clause in clauses if re.search(negation, clause) and re.search(controls, clause)
    ]
    preserve_filters = any(
        re.search(
            r"\b(?:keep|leave|preserve)\b.*\b(?:filters?|scope)\b.*\b(?:unchanged|as is)\b", clause
        )
        or re.search(r"\bgiu nguyen\b.*\b(?:bo loc|pham vi)\b", clause)
        for clause in clauses
    )
    revoke_view = any(
        re.search(r"\b(?:view|panel|dashboard|screen|giao dien|che do xem|man hinh)\b", clause)
        or not re.search(r"\b(?:filters?|scope|selection|bo loc|pham vi)\b", clause)
        for clause in negative_clauses
    )
    revoke_filters = bool(negative_clauses) or preserve_filters
    # Definition requests may use "show" without requesting a product operation.
    # Their mentioned labels cannot grant filters to a separate UI clause either.
    definition = r"\bwhat\b.*\bmeans?\b|\b(?:meaning|definition|nghia|la gi)\b"
    positive = " ".join(
        clause
        for clause in clauses
        if clause not in negative_clauses and not re.search(definition, clause)
    )
    view_requested = bool(
        re.search(controls, positive) or re.match(r"\s*(?:what about|how about|con)\s+", positive)
    )
    normalized = positive
    for token, value in literals.items():
        normalized = normalized.replace(token, value)
    grants = {}
    mappings = {
        "production_line": "production_lines",
        "category": "categories",
        "severity": "severities",
        "defect_type": "defect_types",
        "defect_location": "defect_locations",
        "inspection_method": "inspection_methods",
    }
    for field, option in mappings.items():
        matches = [
            value
            for value in options.get(option, [])
            if re.search(
                r"(?<!\w)" + re.escape(plain_text(value)).replace(r"\-", r"[- ]?") + r"(?!\w)",
                normalized,
            )
        ]
        if len(matches) == 1:
            grants[field] = matches[0]
    for phrase, severity in (
        ("nghiem trong", "Critical"),
        ("trung binh", "Moderate"),
        ("nhe", "Minor"),
    ):
        if phrase in normalized and "severity" not in grants:
            grants["severity"] = severity
    product = re.search(r"(?:product|san pham)\s*(?:id)?\s*#?\s*(\d+)\b", normalized)
    if product and product[1] in options.get("product_ids", []):
        grants["product_id"] = product[1]
    if re.search(r"\b(exclude|remove|without|bo|loai)\b", normalized) and re.search(
        r"\b10\b", normalized
    ):
        grants["scenario"] = "exclude_cost_10"
    if ("ambiguous" in normalized or "mo ho" in normalized) and re.search(
        r"d/?m|day.?month|ngay.?thang", normalized
    ):
        grants["scenario"] = "ambiguous_dm"
    if re.search(r"2026.{0,30}2024|year_2024", normalized):
        grants["scenario"] = "year_2024"
    if re.search(r"\b(reset|restore|ve mac dinh|khoi phuc|quay ve quy tac chuan)\b", normalized):
        grants["scenario"] = ""
    month_values = re.findall(r"2024-(0[1-6])", normalized)
    for month in range(1, 7):
        names = ("january", "february", "march", "april", "may", "june")
        if re.search(r"\b" + names[month - 1] + r"\b|\bthang\s+" + str(month) + r"\b", normalized):
            month_values.append(f"{month:02d}")
    months = sorted(set(month_values))
    if months:
        grants["start_month"], grants["end_month"] = "2024-" + months[0], "2024-" + months[-1]
    return {
        "filters": grants if view_requested and not revoke_filters and not revoke_view else {},
        "undo": bool(re.search(r"\b(undo|revert|hoan tac|quay lai)\b", positive))
        and not revoke_filters
        and not revoke_view,
        "view": view_requested and not revoke_view,
        "revoke_filters": revoke_filters,
        "revoke_view": revoke_view,
    }


def merge_scope_grants(prior, incoming):
    """A later direct prohibition revokes permissions already granted in a run."""
    return {
        "filters": {}
        if incoming.get("revoke_filters") or incoming.get("revoke_view")
        else {**prior.get("filters", {}), **incoming.get("filters", {})},
        "undo": False
        if incoming.get("revoke_filters") or incoming.get("revoke_view")
        else bool(prior.get("undo") or incoming.get("undo")),
        "view": False
        if incoming.get("revoke_view")
        else bool(prior.get("view") or incoming.get("view")),
    }


def validate_scope_change(authority, current_filters, requested_filters):
    changes = {
        key: value
        for key, value in requested_filters.items()
        if value is not None and value != current_filters.get(key)
    }
    if any(authority.get("filters", {}).get(key) != value for key, value in changes.items()):
        raise ValueError("This run did not receive direct user authority for those filter changes")


def export_saved_investigation(
    store: SessionStore, sid: str, version: int, language: str
) -> tuple[Path, dict[str, Any]]:
    """Export the exact saved version, including honest review and locale metadata."""
    language = normalize_language(language)
    current = store.investigation(sid)
    if current["version"] != version:
        raise ValueError("Investigation version changed; save or reload before exporting")
    payload = {
        "investigation": current,
        "export": {
            "schema": "investigation-export-v1",
            "language": language,
            "report_number_format": report_number_format(language),
            "value_locale": "JSON number notation",
            "currency_unit": "local currency",
            "investigation_version": version,
            "state_sha256": sha256(canonical(current)),
            "content_note": "Authored text is preserved exactly as saved. Export does not translate text or promote unverified content.",
        },
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    directory = store.root / "investigation_exports" / sid / f"version-{version}-{language}"
    target = directory / "artifacts" / f"investigation-v{version}-{language}.json"
    if not directory.resolve().is_relative_to(store.root) or target.is_symlink():
        raise ValueError("Investigation export path is outside the local artifact store")
    target.parent.mkdir(parents=True, exist_ok=True)
    ledger = EvidenceLedger(directory)
    if ledger.verify_integrity():
        raise ValueError("Investigation export ledger failed integrity verification")
    if target.exists() and target.read_bytes() != content:
        raise ValueError("Investigation export changed after registration; download was refused")
    if not target.exists():
        target.write_bytes(content)
    if not ledger.records():
        ledger.record(
            "investigation_export",
            "export_saved_investigation\n" + Path(__file__).read_text(encoding="utf-8"),
            {
                "ok": True,
                "session_id": sid,
                "version": version,
                "state_sha256": payload["export"]["state_sha256"],
                "export_sha256": sha256(content),
                "language": language,
                "context_id": current["context_id"],
                "dataset_version": current["dataset_version"],
                "policy_version": current["policy_version"],
            },
        )
    artifact = ledger.register_artifact(target, kind="editable_investigation_export")
    if sha256(target.read_bytes()) != artifact["sha256"]:
        raise ValueError("Investigation export failed final content read-back")
    return target, artifact


def _text(value, field):
    if not isinstance(value, str) or len(value) > 6000:
        raise ValueError(field + " must be text of at most 6000 characters")
    return value


def user_changes(current, body):
    allowed = {"question", "summary", "findings", "hypotheses", "next_steps", "priorities"}
    if (
        not isinstance(body, dict)
        or "expected_version" not in body
        or set(body) - allowed - {"expected_version"}
    ):
        raise ValueError(
            "Investigation edits require expected_version and supported content fields"
        )
    if type(body["expected_version"]) is not int or body["expected_version"] < 0:
        raise ValueError("expected_version must be a nonnegative integer")
    result = {}
    for key in allowed & body.keys():
        value = body[key]
        if key in {"question", "summary"}:
            result[key] = _text(value, key)
            continue
        if not isinstance(value, list) or len(value) > (6 if key == "priorities" else 12):
            raise ValueError(key + " must be a bounded list")
        if key == "next_steps":
            result[key] = [_text(item, key) for item in value]
            continue
        items = []
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(key + " entries must be objects")
            identity = item.get("id") or sha256(canonical(item))[:16]
            if not isinstance(identity, str) or len(identity) > 64:
                raise ValueError("Invalid work-state entry identity")
            if key == "findings":
                if set(item) - {
                    "id",
                    "text",
                    "status",
                    "evidence",
                    "context_id",
                    "numeric_claims_checked",
                }:
                    raise ValueError("Unsupported finding field")
                text = _text(item.get("text"), "finding text")
                prior: dict[str, Any] = next(
                    (entry for entry in current[key] if entry["id"] == identity), {}
                )
                items.append(
                    {
                        "id": identity,
                        "text": text,
                        "status": prior.get("status")
                        if prior.get("text") == text
                        else "user_edited",
                        "evidence": prior.get("evidence", []),
                        "context_id": prior.get("context_id", current.get("context_id")),
                        "numeric_claims_checked": prior.get("numeric_claims_checked", False)
                        if prior.get("text") == text
                        else False,
                    }
                )
            elif key == "hypotheses":
                if set(item) - {"id", "text", "status"} or item.get("status") not in {
                    "open",
                    "rejected",
                    "supported",
                    "unresolved",
                }:
                    raise ValueError("Invalid hypothesis fields or status")
                items.append(
                    {
                        "id": identity,
                        "text": _text(item.get("text"), "hypothesis text"),
                        "status": item["status"],
                    }
                )
            else:
                fields = {"evidence", "action", "owner", "metric"}
                if set(item) - fields - {"id"} or fields - item.keys():
                    raise ValueError("Every priority requires Evidence, Action, Owner and Metric")
                items.append(
                    {"id": identity, **{field: _text(item[field], field) for field in fields}}
                )
        if len({item["id"] for item in items}) != len(items):
            raise ValueError("Work-state entry identities must be unique")
        result[key] = items
    if result:
        result.update(content_status="unverified", semantic_review=None)
    return result
