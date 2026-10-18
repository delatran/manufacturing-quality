"""Deterministic PNG views over original cells in successful sealed receipts."""

from __future__ import annotations

import io
import json
import math
import re
import textwrap
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .claim_feedback import numeric_role, source_contexts
from .evidence import EvidenceLedger, canonical, sha256
from .localization import display_number, normalize_language
from .report_output import unit_note

RENDERER = "source_rows_png_v1"
RowSelector = Callable[[dict, dict], tuple[list, list]]
_KEY = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_FIELDS = {
    "event_count": {"en": "Logged defect events", "vi": "Sự kiện lỗi đã ghi nhận"},
    "month": {"en": "Month", "vi": "Tháng"},
    "production_line": {"en": "Production line", "vi": "Chuyền sản xuất"},
    "category": {"en": "Product category", "vi": "Nhóm sản phẩm"},
}


def _field_label(key: str, language: str, terms: dict) -> str:
    # Unknown analytical names remain exact source keys rather than guessed prose.
    return _FIELDS.get(key, terms.get(key, {})).get(language, key)


def _chart_binding(
    ledger: EvidenceLedger, arguments: dict, language: str, select_rows: RowSelector
) -> dict[str, Any]:
    normalize_language(language)
    if not isinstance(arguments, dict) or set(arguments) != {
        "receipt_id",
        "path",
        "x",
        "y",
        "kind",
        "offset",
        "limit",
    }:
        raise ValueError("A chart requires only a receipt, original path, x/y keys, kind and page")
    if arguments["kind"] not in {"bar", "line"}:
        raise ValueError("Chart kind must be bar or line")
    x, y = arguments["x"], arguments["y"]
    if any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in (x, y)) or x == y:
        raise ValueError("Chart axes must be distinct exact source column keys")
    records = ledger.records()
    if errors := ledger.verify_integrity(records):
        raise ValueError("Chart source ledger integrity failed: " + "; ".join(errors))
    source = next(
        (entry for entry in records if entry["execution_id"] == arguments["receipt_id"]), None
    )
    if source is None:
        raise ValueError("The chart source receipt is not available in this run")
    if not isinstance(source.get("result"), dict) or source["result"].get("ok") is False:
        raise ValueError("Charts require a successful sealed computation receipt")
    result_context = source["result"].get("context")
    entry_context = source.get("analysis_context")
    if result_context is not None and entry_context is not None and result_context != entry_context:
        raise ValueError("Chart source context differs from its sealed execution context")
    selection = {key: arguments[key] for key in ("path", "offset", "limit")}
    rows, selected = select_rows(source, {**selection, "columns": [x, y]})
    if not selected:
        raise ValueError("A chart requires at least one selected source row")
    contexts = source_contexts(source, arguments["path"])
    if source.get("execution_kind") in {"bounded_analysis", "canonical_receipt_import"}:
        if not contexts:
            raise ValueError("A scoped chart requires a valid frozen source context")
    points = []
    claims = []
    numeric_paths = Counter(item["path"] for item in source["numeric_values"])
    for path, row in selected:
        label, value = row[x], row[y]
        if label is None or isinstance(label, str) and not label.strip():
            raise ValueError("Every chart row requires an available source label")
        if type(value) not in {int, float, type(None)}:
            raise ValueError("Chart values must be numeric source cells or null")
        value_path = path + "." + y
        if value is not None:
            if not math.isfinite(value):
                raise ValueError("Chart values must be finite")
            if numeric_paths[value_path] != 1:
                raise ValueError("Chart values require unique addressable numeric source paths")
            if numeric_role(source, value_path) != "measurement":
                raise ValueError("Chart values must be measurements, not identifiers or parameters")
            claims.append(
                {"execution_id": source["execution_id"], "path": value_path, "value": value}
            )
        points.append(
            {"label_path": path + "." + x, "label": label, "value_path": value_path, "value": value}
        )
    if not claims:
        raise ValueError("A chart requires at least one available numeric source value")
    glossary = Path(__file__).resolve().parents[1] / "knowledge" / "glossary.json"
    glossary_bytes = glossary.read_bytes() if glossary.is_file() else b""
    terms = json.loads(glossary_bytes)["terms"] if glossary_bytes else {}
    binding = {
        "renderer": RENDERER,
        "renderer_sha256": sha256(Path(__file__).read_bytes()),
        "glossary_sha256": sha256(glossary_bytes),
        "run_id": ledger.run_dir.name,
        "language": language,
        "kind": arguments["kind"],
        "source": {
            key: source[key]
            for key in ("execution_id", "entry_sha256", "result_sha256", "code_sha256")
        },
        "selection": {**selection, "x": x, "y": y},
        "points": points,
        "points_sha256": sha256(canonical(points)),
        "rows_total": len(rows),
        "rows_returned": len(points),
        "analysis_contexts": contexts,
        "axis_labels": {
            "x": _field_label(x, language, terms),
            "y": _field_label(y, language, terms),
        },
        "unit_note": unit_note(claims, ledger, language),
        "null_policy": "gap",
    }
    return {"id": sha256(canonical(binding))[:24], **binding}


def validate_chart_view(ledger: EvidenceLedger, chart: dict, *, select_rows: RowSelector) -> None:
    """Rebuild the source binding before accepting its measurement/label mapping."""
    try:
        arguments = {
            "receipt_id": chart["source"]["execution_id"],
            "kind": chart["kind"],
            **chart["selection"],
        }
        expected = _chart_binding(ledger, arguments, chart["language"], select_rows)
    except (KeyError, TypeError) as error:
        raise ValueError("Invalid source-chart binding") from error
    if canonical(chart) != canonical(expected):
        raise ValueError("Source-chart binding differs from its successful sealed source")


def _png(chart: dict) -> bytes:
    # The object-oriented Agg interface avoids pyplot's shared figure state.
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.text import Text
    from matplotlib.ticker import FuncFormatter

    language = chart["language"]
    points = chart["points"]
    labels = [str(point["label"]) for point in points]
    values = [math.nan if point["value"] is None else point["value"] for point in points]
    positions = list(range(len(points)))
    height = max(3.8, min(16.0, len(points) * 0.34)) if chart["kind"] == "bar" else 4.8
    figure = Figure(figsize=(9.5, height), dpi=120, facecolor="#faf8f3")
    axes = figure.subplots()
    axes.set_facecolor("#faf8f3")
    if chart["kind"] == "bar":
        axes.barh(positions, values, color="#225926", height=0.65)
        axes.set_yticks(positions, labels=labels)
        axes.invert_yaxis()
        axes.axvline(0, color="#6e786c", linewidth=0.7)
        axes.set_xlabel(chart["axis_labels"]["y"])
        axes.set_ylabel(chart["axis_labels"]["x"])
        axes.xaxis.set_major_formatter(
            FuncFormatter(lambda value, _: display_number(format(value, ".8g"), language))
        )
        for position, point in enumerate(points):
            value = point["value"]
            text = (
                display_number(str(value), language)
                if value is not None
                else "Không có dữ liệu"
                if language == "vi"
                else "Unavailable"
            )
            axes.annotate(
                text,
                (0 if value is None else value, position),
                xytext=(-5 if value is not None and value < 0 else 5, 0),
                textcoords="offset points",
                va="center",
                ha="right" if value is not None and value < 0 else "left",
                fontsize=8,
            )
        axes.margins(x=0.18)
    else:
        axes.plot(positions, values, marker="o", color="#225926", linewidth=1.8)
        axes.set_xticks(positions, labels=labels, rotation=40, ha="right")
        axes.set_xlabel(chart["axis_labels"]["x"])
        axes.set_ylabel(chart["axis_labels"]["y"])
        axes.yaxis.set_major_formatter(
            FuncFormatter(lambda value, _: display_number(format(value, ".8g"), language))
        )
        axes.margins(y=0.15)
    axes.tick_params(labelsize=8)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    axes.set_title(
        chart["axis_labels"]["y"]
        + (" theo " if language == "vi" else " by ")
        + chart["axis_labels"]["x"],
        loc="left",
        fontsize=12,
        fontweight="bold",
        pad=12,
    )
    missing = (
        "Giá trị thiếu để trống, không nội suy. Thứ tự giữ nguyên theo nguồn."
        if language == "vi"
        else "Missing values remain gaps. No interpolation. Original source order."
    )
    notes = [missing]
    if chart["rows_returned"] != chart["rows_total"]:
        notes.append(
            "Chỉ hiển thị trang dữ liệu đã chọn."
            if language == "vi"
            else "Only the selected source page is shown."
        )
    if chart["unit_note"]:
        notes.append(chart["unit_note"].replace("**", ""))
    figure.text(0.02, 0.015, textwrap.fill(" ".join(notes), width=132), fontsize=8, va="bottom")
    for text in figure.findobj(Text):
        text.set_parse_math(False)
        text.set_fontfamily("DejaVu Sans")
    figure.tight_layout(rect=(0.02, 0.13, 0.98, 0.98))
    output = io.BytesIO()
    FigureCanvasAgg(figure).print_png(output, metadata={"Description": canonical(chart)})
    return output.getvalue()


def create_chart_view(
    ledger: EvidenceLedger, arguments: dict, *, language: str, select_rows: RowSelector
) -> dict[str, Any]:
    """Select original source cells, render them without aggregation, and register PNG bytes."""
    chart = _chart_binding(ledger, arguments, language, select_rows)
    content = _png(chart)
    validate_chart_view(ledger, chart, select_rows=select_rows)
    artifact_root = ledger.run_dir / "artifacts"
    if artifact_root.is_symlink() or not artifact_root.resolve().is_relative_to(ledger.run_dir):
        raise ValueError("Chart artifacts must stay inside the current run")
    artifact_root.mkdir(exist_ok=True)
    target = artifact_root / ("source-chart-" + chart["id"] + ".png")
    if target.is_symlink():
        raise ValueError("A chart artifact cannot be a symbolic link")
    try:
        with target.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if target.read_bytes() != content:
            raise ValueError(
                "Existing chart bytes differ; refusing to overwrite evidence"
            ) from None
    contexts = chart["analysis_contexts"]
    artifact = ledger.register_artifact(
        target,
        kind="source_chart",
        context=contexts[0] if len(contexts) == 1 else None,
        locale=language,
    )
    return {"ok": True, "artifact": artifact, "chart": chart}
