"""Default presentation hides only whole, exactly retrievable source collections."""

import asyncio
import json
from copy import deepcopy

import pytest
from analyst.evidence import canonical, sha256
from analyst.runtime import AnalystReport, ReportDraft, result_at_path
from test_report_tables import runtime_at


def receipt(runtime, value):
    return runtime.toolkit.ledger.record(
        "local_python_repl", "# synthetic structured result", value
    )


def test_hidden_collections_roundtrip_by_original_paths_and_keep_ids(tmp_path):
    runtime = runtime_at(tmp_path)
    original = {
        "ok": True,
        "overview": {"event_count": 999, "missing": None},
        "rows": [
            {"label": "same" * 50, "value": index, "nested": [True, None]} for index in range(122)
        ],
        "child": {"series": list(range(104))},
        "empty": [],
        "labels": ["A", "B"],
    }
    entry = receipt(runtime, original)
    ledger_before = runtime.toolkit.ledger.path.read_bytes()
    view = runtime.model_receipt(entry)
    assert view["result"] == {
        "ok": True,
        "overview": original["overview"],
        "child": {},
        "empty": [],
        "labels": ["A", "B"],
    }
    assert {item["path"] for item in view["omitted_collections"]} == {"$.rows", "$.child.series"}
    assert [item["path"] for item in view["numeric_evidence"]] == ["$.overview.event_count"]
    assert view["numeric_evidence"][0]["value"] == 999
    restored = deepcopy(view["result"])
    issued = {}
    for navigation in view["omitted_collections"]:
        path = navigation["path"]
        source_values = result_at_path(entry["result"], path)
        assert navigation["item_count"] == len(source_values)
        for hint in (navigation["first_item"], navigation["next_item"]):
            base, selected = asyncio.run(
                runtime.evidence_detail({"receipt_id": entry["execution_id"], **hint})
            )
            assert selected == [(hint["path"], result_at_path(entry["result"], hint["path"]))]
            assert (
                runtime.model_receipt(base, selected)["result"]["selected"][0]["path"]
                == hint["path"]
            )
        for index, source_value in enumerate(source_values):
            selected_path = f"{path}[{index}]"
            base, selected = asyncio.run(
                runtime.evidence_detail(
                    {
                        "receipt_id": entry["execution_id"],
                        "path": selected_path,
                        "offset": 0,
                        "limit": 100,
                    }
                )
            )
            assert selected == [(selected_path, source_value)]
            selected_view = runtime.model_receipt(base, selected)
            assert "omitted_collections" not in selected_view
            for number in selected_view["numeric_evidence"]:
                assert number["id"] == issued.setdefault(number["path"], number["id"])
        parent_path, _, key = path.rpartition(".")
        result_at_path(restored, parent_path)[key] = source_values
    assert restored == original
    assert runtime.model_receipt(entry)["numeric_evidence"] == view["numeric_evidence"]
    assert set(issued.values()).issubset(runtime.available_claims())
    assert runtime.toolkit.ledger.path.read_bytes() == ledger_before
    assert runtime.toolkit.ledger.records()[0]["result"] == original


@pytest.mark.parametrize("key", ["daily summary", "key.with.dot", "Số liệu", "[records]"])
def test_unaddressable_subtrees_remain_exactly_visible(tmp_path, key):
    runtime = runtime_at(tmp_path)
    subtree = {"rows": [{"value": index, "label": "original" * 40} for index in range(120)]}
    entry = receipt(runtime, {key: subtree, "safe": [{"value": 7}]})
    view = runtime.model_receipt(entry)
    assert view["result"][key] == subtree
    assert [item["path"] for item in view["omitted_collections"]] == ["$.safe"]


@pytest.mark.parametrize("item", [{"text": "x" * 24001}, {"nested": list(range(101))}])
def test_a_collection_with_an_unretrievable_record_is_never_hidden(tmp_path, item):
    runtime = runtime_at(tmp_path)
    result = {"rows": [{"label": "small"}, item], "unpaged_note": "x" * 24001}
    view = runtime.model_receipt(receipt(runtime, result))
    assert view["result"] == result
    assert "omitted_collections" not in view


def test_nested_records_use_indexed_detail_without_faking_a_page_guarantee(tmp_path):
    runtime = runtime_at(tmp_path)
    result = {
        "rows": [{"nested": list(range(60))}, {"nested": list(range(60))}],
        "unpaged_note": "x" * 24001,
    }
    entry = receipt(runtime, result)
    navigation = runtime.model_receipt(entry)["omitted_collections"][0]
    with pytest.raises(ValueError, match="unpaged"):
        asyncio.run(
            runtime.evidence_detail(
                {"receipt_id": entry["execution_id"], "path": "$.rows", "offset": 0, "limit": 1}
            )
        )
    for hint in (navigation["first_item"], navigation["next_item"]):
        _, selected = asyncio.run(
            runtime.evidence_detail({"receipt_id": entry["execution_id"], **hint})
        )
        assert selected[0][1] == result_at_path(result, hint["path"])


def test_nested_list_near_character_bound_remains_visible_if_actual_indexed_page_is_too_large(
    tmp_path,
):
    runtime = runtime_at(tmp_path)
    nested = ["x" * 1180] * 20
    result = {"rows": [nested] + ["tail"] * 100}
    entry = receipt(runtime, result)
    assert len(canonical(result)) > 24000
    assert len(canonical([("$.rows[0]", nested)])) <= 24000
    actual_selection = [(f"$.rows[0][{index}]", value) for index, value in enumerate(nested)]
    assert len(canonical(actual_selection)) > 24000
    with pytest.raises(ValueError, match="oversized"):
        asyncio.run(
            runtime.evidence_detail(
                {
                    "receipt_id": entry["execution_id"],
                    "path": "$.rows[0]",
                    "offset": 0,
                    "limit": 100,
                }
            )
        )
    view = runtime.model_receipt(entry)
    assert view["result"] == result
    assert "omitted_collections" not in view


@pytest.mark.parametrize("nested", [[], [3, 4, None]])
def test_retrievable_nested_list_item_uses_the_actual_child_paths(tmp_path, nested):
    runtime = runtime_at(tmp_path)
    result = {"rows": [nested] + [{"payload": "x" * 240, "count": index} for index in range(100)]}
    entry = receipt(runtime, result)
    view = runtime.model_receipt(entry)
    navigation = view["omitted_collections"][0]
    assert navigation["path"] == "$.rows" and navigation["item_type"] == "mixed"
    base, selected = asyncio.run(
        runtime.evidence_detail({"receipt_id": entry["execution_id"], **navigation["first_item"]})
    )
    assert selected == [(f"$.rows[0][{index}]", value) for index, value in enumerate(nested)]
    selected_view = runtime.model_receipt(base, selected)
    assert [number["path"] for number in selected_view["numeric_evidence"]] == (
        ["$.rows[0][0]", "$.rows[0][1]"] if nested else []
    )
    assert all(
        result_at_path(result, number["path"]) == number["value"]
        for number in selected_view["numeric_evidence"]
    )


def test_small_source_metadata_and_failed_or_explicit_results_stay_intact(tmp_path):
    runtime = runtime_at(tmp_path)
    result = {
        "metadata": {"notes": [{"meaning": "raw issue", "count": 7}]},
        "quality": {"issues": [{"code": "missing", "affected_count": 3}]},
        "qa": {"checks": [{"check": "source", "passed": True}]},
        "recommendations": [{"title": "Review", "priority": 1}],
        "rules": {"exceptions": ["A", "B"]},
        "empty": [],
        "unavailable": None,
    }
    entry = receipt(runtime, result)
    assert runtime.model_receipt(entry)["result"] == result
    large = [{"value": index} for index in range(120)]
    failed = receipt(runtime, {"ok": False, "rows": large})
    view = runtime.model_receipt(failed)
    assert view["result"] == failed["result"] and not view["numeric_evidence"]
    _, selected = runtime.detail_selection(
        entry, {"path": "$.quality.issues", "offset": 0, "limit": 100}
    )
    assert (
        runtime.model_receipt(entry, selected)["result"]["selected"][0]["value"]
        == result["quality"]["issues"][0]
    )


@pytest.mark.parametrize("tool", ["local_sql_engine", "local_python_repl"])
def test_small_generic_results_remain_directly_readable(tmp_path, tool):
    runtime = runtime_at(tmp_path)
    result = {
        "ok": True,
        "rows": [{"group": name, "events": index} for index, name in enumerate(("A", "B", "C"))],
    }
    entry = runtime.toolkit.ledger.record(tool, "# small structured result", result)
    view = runtime.model_receipt(entry)
    assert view["result"] == result
    assert "omitted_collections" not in view
    assert [item["value"] for item in view["numeric_evidence"]] == [0, 1, 2]


def test_factored_contexts_reconstruct_exact_labels_with_scope_and_execution_separate(tmp_path):
    runtime = runtime_at(tmp_path)
    result = {
        "name": "outer",
        "code": "excluded",
        "stdout": "excluded",
        "report": "excluded",
        "long": "x" * 501,
        "analysis": {"name": "inner", "rows": [{"name": "row", "value": 7, "other": 8}]},
    }
    tokens = []
    entries = []
    for line in ("Line-1", "Line-2"):
        entry = runtime.toolkit.ledger.record(
            "local_python_repl",
            "# independent result",
            result,
            analysis_context={
                "context_id": f"synthetic-projection-{line}",
                "snapshot_id": "synthetic-projection-snapshot",
                "dataset_version": "synthetic-projection-dataset",
                "policy_version": "synthetic-projection-policy",
                "scenario": "",
                "filters": {"production_line": line},
            },
        )
        entries.append(entry)
        _, selected = runtime.detail_selection(
            entry, {"path": "$.analysis.rows", "offset": 0, "limit": 100}
        )
        tokens.extend(
            item["id"] for item in runtime.model_receipt(entry, selected)["numeric_evidence"]
        )
    report = runtime.resolve_report(
        ReportDraft(report="\n".join("[value:" + identity + "]" for identity in tokens))
    )
    before = runtime.toolkit.ledger.path.read_bytes()
    support, errors = runtime.qa_evidence(report)
    assert not errors
    contexts = {item["id"]: item for item in support["ancestor_contexts"]}
    assert len(contexts) == 6 and len(support["numeric_support"]) == 4
    for item in support["numeric_support"]:
        ancestors = [contexts[identity] for identity in item["ancestor_context_ids"]]
        assert [context["path"] for context in ancestors] == [
            "$",
            "$.analysis",
            "$.analysis.rows[0]",
        ]
        assert len({context["execution_id"] for context in ancestors}) == 1
        labels = {}
        for context in ancestors:
            labels.update(context["labels"])
        assert labels == {"name": "row"}
        assert item["scope"] in ({"production_line": "Line-1"}, {"production_line": "Line-2"})
        assert item["labels"] == labels
        assert "ancestor_labels" not in item
        assert item["execution_id"] == ancestors[0]["execution_id"]
        source = next(entry for entry in entries if entry["execution_id"] == item["execution_id"])
        assert item["scope"] == source["analysis_context"]["filters"]
        assert item["source_parent_path"] == "$.analysis.rows[0]"
        assert item["value"] == result_at_path(source["result"], item["path"])
    assert runtime.toolkit.ledger.path.read_bytes() == before


def test_exact_runner_input_is_internal_and_never_becomes_measurement_evidence(tmp_path):
    runtime = runtime_at(tmp_path)
    text = json.dumps({"report": "Đúng\nGiữ nguyên", "source": "A\u2028B"}, ensure_ascii=False)
    requested = runtime.record_report_qa_input(text)
    stored = requested["result"]
    path = tmp_path / stored["runner_input_path"]
    assert path.read_bytes() == text.encode("utf-8")
    assert stored["runner_input_sha256"] == sha256(text)
    assert stored["phase"] == "requested" and "provider request envelope" in stored["input_scope"]
    assert runtime.toolkit.ledger.artifacts() == []
    support, errors = runtime.qa_evidence(
        AnalystReport(report="A qualitative explanation.", claims=[])
    )
    assert not errors and not support["numeric_support"] and not support["qualitative_support"]
    assert not support["delivery"]["artifacts"]
    assert text not in canonical(runtime.toolkit.ledger.records())
