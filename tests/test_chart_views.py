"""Source-cell chart binding, failure handling, and actual registered PNG output."""

import asyncio
import copy
import io
import json
import struct
from types import SimpleNamespace

import pytest
from analyst.chart_views import create_chart_view, validate_chart_view
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, sha256
from analyst.runtime import AnalystRuntime, table_selection
from PIL import Image
from test_dashboard import service
from test_runtime import FrozenAnalysisService, canonical_context


def context(identity="scope-a"):
    return {
        "context_id": identity,
        "snapshot_id": "snapshot-a",
        "dataset_version": "dataset-a",
        "policy_version": "policy-a",
        "scenario": "",
        "filters": {"scenario": ""},
    }


def source(ledger, rows=None, *, ok=True, result_context=None, entry_context=None):
    scope = result_context or context()
    return ledger.record(
        "analysis_query",
        "# frozen fixture calculation",
        {
            "ok": ok,
            "analysis": {
                "rows": rows
                if rows is not None
                else [
                    {"category": "Category 1", "event_count": 0},
                    {"category": "Category 2", "event_count": -2.5},
                    {"category": "Category 1", "event_count": None},
                    {"category": "Other", "event_count": 8},
                ]
            },
            "context": scope,
        },
        analysis_context=entry_context or scope,
        execution_kind="bounded_analysis",
    )


def arguments(entry, **changes):
    return {
        "receipt_id": entry["execution_id"],
        "path": "$.analysis.rows",
        "x": "category",
        "y": "event_count",
        "kind": "bar",
        "offset": 0,
        "limit": 100,
        **changes,
    }


def create(ledger, entry, *, language="en", **changes):
    return create_chart_view(
        ledger, arguments(entry, **changes), language=language, select_rows=table_selection
    )


@pytest.mark.parametrize("kind", ["bar", "line"])
def test_actual_png_and_registry_preserve_zero_negative_nulls_and_repeated_labels(tmp_path, kind):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger)
    result = create(ledger, entry, kind=kind)
    chart, artifact = result["chart"], result["artifact"]
    assert [point["value"] for point in chart["points"]] == [0, -2.5, None, 8]
    assert [point["label"] for point in chart["points"]] == [
        "Category 1",
        "Category 2",
        "Category 1",
        "Other",
    ]
    assert [point["value_path"] for point in chart["points"]] == [
        f"$.analysis.rows[{index}].event_count" for index in range(4)
    ]
    assert chart["null_policy"] == "gap"
    assert chart["analysis_contexts"] == [context()]
    assert artifact["context_id"] == context()["context_id"]
    assert artifact["export_locale"] == "en"
    assert chart["source"] == {
        key: entry[key] for key in ("execution_id", "entry_sha256", "result_sha256", "code_sha256")
    }
    assert len(chart["renderer_sha256"]) == 64
    path = ledger.resolve_artifact(artifact["path"])
    content = path.read_bytes()
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", content[16:24])
    assert width >= 1000 and height >= 400
    with Image.open(io.BytesIO(content)) as image:
        assert json.loads(image.info["Description"]) == chart
        image.load()
    assert artifact["sha256"] == sha256(content)
    assert ledger.artifacts() == [artifact]
    assert ledger.verify_integrity() == []
    validate_chart_view(ledger, chart, select_rows=table_selection)
    assert create(ledger, entry, kind=kind) == result


def test_page_is_exact_and_unknown_axis_keys_are_not_invented(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = source(
        ledger,
        [{"custom_label": "C", "custom_measure": 3}, {"custom_label": "A", "custom_measure": 1}],
    )
    result = create(ledger, entry, x="custom_label", y="custom_measure", offset=1, limit=1)
    chart = result["chart"]
    assert chart["points"] == [
        {
            "label_path": "$.analysis.rows[1].custom_label",
            "label": "A",
            "value_path": "$.analysis.rows[1].custom_measure",
            "value": 1,
        }
    ]
    assert chart["axis_labels"] == {"x": "custom_label", "y": "custom_measure"}
    assert chart["rows_total"] == 2 and chart["rows_returned"] == 1


def test_vietnamese_and_existing_glossary_are_bound_without_rewriting_source_labels(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger, [{"category": "Category 1", "known_repair_cost": 12.5}])
    result = create(ledger, entry, y="known_repair_cost", language="vi")
    assert result["chart"]["axis_labels"] == {
        "x": "Nhóm sản phẩm",
        "y": "chi phí sửa chữa hợp lệ đã ghi nhận",
    }
    assert result["chart"]["points"][0]["label"] == "Category 1"
    assert result["artifact"]["export_locale"] == "vi"
    assert result["chart"]["language"] == "vi"
    with pytest.raises(ValueError, match="language"):
        create(ledger, entry, y="known_repair_cost", language="fr")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"category": "A", "event_count": None}],
        [{"category": "A", "event_count": "12"}],
        [{"category": "A", "event_count": True}],
        [{"category": None, "event_count": 1}],
        [{"category": " ", "event_count": 1}],
        [{"category": "A", "different": 1}],
    ],
)
def test_unusable_cells_fail_without_creating_artifacts(tmp_path, rows):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger, rows)
    with pytest.raises(ValueError):
        create(ledger, entry)
    assert not ledger.artifacts() and not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "pie"},
        {"y": ["event_count"]},
        {"x": "category;__import__('os')"},
        {"path": "$.analysis.rows[*]"},
        {"path": "../../secret"},
        {"offset": -1},
        {"limit": 0},
        {"limit": 101},
        {"offset": 100},
        {"x": "event_count"},
        {"data": [{"category": "invented", "event_count": 1}]},
        {"output_path": "../../secret.png"},
        {"title": "Invented claim"},
        {"code": "print('no')"},
    ],
)
def test_only_exact_source_selection_is_accepted(tmp_path, changes):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger)
    with pytest.raises((ValueError, TypeError)):
        create(ledger, entry, **changes)
    assert not ledger.artifacts()


def test_unknown_other_run_and_failed_receipts_are_rejected(tmp_path):
    ledger = EvidenceLedger(tmp_path / "current")
    entry = source(ledger, ok=False)
    with pytest.raises(ValueError, match="successful"):
        create(ledger, entry)
    other = source(EvidenceLedger(tmp_path / "other"))
    for identity in ("unknown", other["execution_id"]):
        with pytest.raises(ValueError, match="this run"):
            create(ledger, entry, receipt_id=identity)


def test_tampered_ledger_context_and_chart_binding_are_rejected(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    source(ledger, result_context=context("a"), entry_context=context("b"))
    entry = ledger.records()[0]
    with pytest.raises(ValueError, match="context"):
        create(ledger, entry)
    entry = source(ledger)
    result = create(ledger, entry)
    for field in ("label", "value", "value_path"):
        altered = copy.deepcopy(result["chart"])
        altered["points"][0][field] = "forged"
        with pytest.raises(ValueError, match="differs"):
            validate_chart_view(ledger, altered, select_rows=table_selection)
    records = ledger.records()
    records[0]["result"]["analysis"]["rows"][0]["event_count"] = 999
    ledger.path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        create(ledger, entry)


def test_measurement_role_and_context_are_required_for_scoped_sources(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger, [{"category": "A", "product_id": 12}])
    with pytest.raises(ValueError, match="identifiers"):
        create(ledger, entry, y="product_id")
    invalid = context()
    invalid["scenario"] = "mismatched"
    entry = source(ledger, result_context=invalid)
    with pytest.raises(ValueError, match="frozen source context"):
        create(ledger, entry)


def test_artifact_tampering_is_not_silently_overwritten(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = source(ledger)
    result = create(ledger, entry)
    path = ledger.resolve_artifact(result["artifact"]["path"])
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        ledger.resolve_artifact(result["artifact"]["path"])
    with pytest.raises(ValueError, match="refusing to overwrite"):
        create(ledger, entry)
    assert path.read_bytes() == b"tampered"


def test_nonfinite_receipts_cannot_be_sealed_or_plotted(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    for value in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            source(ledger, [{"category": "A", "event_count": value}])
    assert not ledger.artifacts()


def test_source_text_stays_literal_in_plot_and_embedded_binding(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    label = r"$\not_a_math_command$ <script>not code</script>"
    entry = source(ledger, [{"category": label, "event_count": 1}])
    result = create(ledger, entry)
    assert result["chart"]["points"][0]["label"] == label
    with Image.open(ledger.resolve_artifact(result["artifact"]["path"])) as image:
        assert json.loads(image.info["Description"])["points"][0]["label"] == label
        image.load()


def test_comparison_side_keeps_its_own_frozen_context(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    baseline, comparison = context("baseline"), context("comparison")
    entry = ledger.record(
        "analysis_compare",
        "# frozen comparison fixture",
        {
            "ok": True,
            "baseline": {
                "context": baseline,
                "analysis": {"rows": [{"month": "First", "event_count": 1}]},
            },
            "comparison": {
                "context": comparison,
                "analysis": {"rows": [{"month": "Second", "event_count": 2}]},
            },
        },
        analysis_context=context("run"),
        execution_kind="bounded_analysis",
    )
    result = create(ledger, entry, path="$.comparison.analysis.rows", x="month")
    assert result["chart"]["analysis_contexts"] == [comparison]
    assert result["artifact"]["context_id"] == "comparison"
    altered = copy.deepcopy(result["chart"])
    altered["analysis_contexts"] = [baseline]
    with pytest.raises(ValueError, match="differs"):
        validate_chart_view(ledger, altered, select_rows=table_selection)


def test_ambiguous_flattened_numeric_paths_cannot_be_chart_evidence(tmp_path):
    ledger = EvidenceLedger(tmp_path)
    entry = ledger.record(
        "analysis_query",
        "# ambiguous original source fixture",
        {
            "ok": True,
            "analysis": {"rows": [{"category": "A", "event_count": 1}]},
            "analysis.rows[0].event_count": 999,
            "context": context(),
        },
        analysis_context=context(),
        execution_kind="bounded_analysis",
    )
    with pytest.raises(ValueError, match="unique addressable"):
        create(ledger, entry)


def test_artifacts_directory_cannot_redirect_chart_writes_outside_run(tmp_path):
    ledger = EvidenceLedger(tmp_path / "run")
    entry = source(ledger)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (ledger.run_dir / "artifacts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("The current host does not permit directory symbolic links")
    with pytest.raises(ValueError, match="inside the current run"):
        create(ledger, entry)
    assert list(outside.iterdir()) == []


def test_real_imported_canonical_context_can_render_monthly_without_ok_flag(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        ledger = EvidenceLedger(tmp_path / "current-run")

        async def emit(*_):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock"),
            SimpleNamespace(ledger=ledger, run_dir=ledger.run_dir, input_paths=[], schemas=[]),
            emit,
            analysis_service=FrozenAnalysisService(payload),
            analysis_context=payload,
        )
        bound = await runtime.read_dashboard_context()
        entry = runtime.import_dashboard_receipt(bound, "dashboard_context")
        assert "ok" not in entry["result"]
        assert entry["execution_kind"] == "canonical_receipt_import"
        result = create(ledger, entry, path="$.monthly", x="month", kind="line")
        assert [point["value"] for point in result["chart"]["points"]] == [4, 8]
        assert [point["label"] for point in result["chart"]["points"]] == ["2024-01", "2024-02"]
        assert result["chart"]["analysis_contexts"] == [payload["context"]]
        assert result["artifact"]["context_id"] == payload["context"]["context_id"]
        ledger.resolve_artifact(result["artifact"]["path"])

    asyncio.run(scenario())


@pytest.mark.parametrize("language,label", [("en", "Product category"), ("vi", "Nhóm sản phẩm")])
def test_canonical_category_summary_uses_product_category_not_defect_type(
    tmp_path, language, label
):
    async def scenario():
        analysis_service = service(tmp_path)
        payload = await analysis_service.get_context()
        categories = payload["analysis"]["category_summary"]
        assert {row["category"] for row in categories} >= {"Category 1", "Category 2"}
        assert "Functional" in payload["options"]["defect_types"]
        ledger = EvidenceLedger(tmp_path / "category-run")

        async def emit(*_):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock"),
            SimpleNamespace(ledger=ledger, run_dir=ledger.run_dir, input_paths=[], schemas=[]),
            emit,
            analysis_service=analysis_service,
            analysis_context=payload,
        )
        entry = runtime.import_dashboard_receipt(payload, "dashboard_context")
        result = create(ledger, entry, path="$.category_summary", x="category", language=language)
        chart = result["chart"]
        assert chart["axis_labels"]["x"] == label
        assert [point["label"] for point in chart["points"]] == [
            row["category"] for row in categories
        ]
        assert [point["value"] for point in chart["points"]] == [
            row["event_count"] for row in categories
        ]
        assert "Functional" not in [point["label"] for point in chart["points"]]
        with Image.open(ledger.resolve_artifact(result["artifact"]["path"])) as image:
            assert json.loads(image.info["Description"])["axis_labels"]["x"] == label
            image.load()

    asyncio.run(scenario())
