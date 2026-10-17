"""Declared source context reaches analyst and QA without invented evidence.

The scripted SDK model verifies input plumbing only. It is not a live semantic
acceptance test, and no provider may be constructed by these tests.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agents import Model, ModelResponse, Usage
from analyst import prompts
from analyst.agent_views import (
    VIEW_BYTES,
    bounded_result,
    qa_comparisons,
    source_projection,
    view_weight,
)
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, canonical, numeric_values, sha256
from analyst.model import compact_items
from analyst.product_tools import SECTIONS
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    QAReview,
    ReportDraft,
    _message,
    result_at_path,
)
from test_claim_feedback import evidence_subject as evidence_subject
from test_claim_feedback import filters
from test_report_output import context, payload

WARNING = "No events were logged after the observed reporting cutoff; completeness is unknown."
UNSUPPORTED = "The whole month is complete and no records are missing."


async def no_events(*_args):
    pass


@pytest.fixture
def subject(tmp_path, monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Source projection tests cannot construct a provider")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", deny)
    ledger = EvidenceLedger(tmp_path)
    tools = SimpleNamespace(ledger=ledger, run_dir=tmp_path, input_paths=[], schemas=[], todos=[])
    return AnalystRuntime(
        Settings(mode="live", api_key="offline-invalid-key", memory_enabled=False), tools, no_events
    )


def issue(runtime, tool, result, **metadata):
    return runtime.toolkit.ledger.record(
        tool,
        "# Explicitly synthetic source for projection tests",
        result,
        execution_kind="bounded_analysis",
        **metadata,
    )


def compare_result():
    first = payload(context("january", end="2024-01"), 4, 20, 100)
    last = payload(context("june", start="2024-06"), 9, 30, 90)
    for side in (first, last):
        side["analysis"]["metadata"]["limitations"] = [WARNING]
        side["analysis"]["anomalies"] = {"limitations": [WARNING]}
    return {
        "ok": True,
        "baseline": first,
        "comparison": last,
        "decomposition": {
            "dimension": "product_id",
            "event_count_change": 5,
            "definitions": {"contribution": "An observed contribution, not a cause."},
            "rows": [
                {"name": index, "event_count_change": value, "known_repair_cost_change": None}
                for index, value in enumerate([3, 3, 0, 0, -1] + [0] * 10, start=1)
            ],
        },
    }


def rank_result():
    first = payload(context("canonical"), 9, 60, 100)
    last = payload(context("financial", scenario="exclude_cost_10"), 9, 50, 90)
    before = list(range(101, 126))
    after = before.copy()
    after[12], after[13] = after[13], after[12]
    after[20], after[21] = after[21], after[20]
    return {
        "ok": True,
        "canonical": first,
        "selected": last,
        "comparison": {
            "rankings": {
                "products_by_known_cost": {
                    "canonical": before,
                    "selected": after,
                    "changed": True,
                },
                "lines_by_reference_intensity": {
                    "canonical": ["Line-1", "Line-2"],
                    "selected": ["Line-1", "Line-2"],
                    "changed": False,
                },
            },
            "limitations": ["The financial scenario is an assumption, not realized savings."],
        },
    }


def warning_paths(projected):
    output = {}
    for record in projected["declared_metadata"]:
        values = record["value"]
        if not isinstance(values, list) or WARNING not in values:
            continue
        for binding in record["sources"]:
            output[binding["path"] + f"[{values.index(WARNING)}]"] = binding["context_ids"]
    return output


def test_query_wrapper_declarations_reach_both_readers_with_original_paths(subject):
    bound = context("whole")
    entry = issue(
        subject,
        "analysis_query",
        {
            "ok": True,
            "context": bound,
            "analysis": {"products": {"top": [{"product_id": 17, "event_count": 9}]}},
            "limitations": [WARNING],
            "definitions": {"event_count": "Selected logged events, not defect prevalence."},
        },
    )
    before = subject.toolkit.ledger.path.read_bytes()
    view = subject.model_receipt(entry)
    support, errors = subject.qa_evidence(
        AnalystReport(report="A qualitative explanation.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert not errors
    qa = support["qualitative_support"][0]["source_projection"]
    assert qa == view["source_projection"] == source_projection(entry)
    assert warning_paths(qa) == {"$.limitations[0]": ["whole"]}
    assert qa["contexts"][0]["filters"] == bound["filters"]
    assert UNSUPPORTED not in canonical(qa)
    assert subject.toolkit.ledger.path.read_bytes() == before
    assert all(
        not row["path"].startswith("$.source_projection") for row in view["numeric_evidence"]
    )


def test_comparison_declarations_preserve_each_side_scope_and_all_duplicate_paths(subject):
    entry = issue(subject, "analysis_compare", compare_result())
    projection = source_projection(entry)
    assert warning_paths(projection) == {
        "$.baseline.analysis.metadata.limitations[0]": ["january"],
        "$.baseline.analysis.anomalies.limitations[0]": ["january"],
        "$.comparison.analysis.metadata.limitations[0]": ["june"],
        "$.comparison.analysis.anomalies.limitations[0]": ["june"],
    }
    assert len([row for row in projection["declared_metadata"] if row["value"] == [WARNING]]) == 1
    assert {row["context_id"]: row["filters"]["start_month"] for row in projection["contexts"]} == {
        "january": "2024-01",
        "june": "2024-06",
    }
    model = subject.model_receipt(entry)
    evidence, errors = subject.qa_evidence(
        AnalystReport(report="The observations have reporting limitations.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert not errors
    assert evidence["qualitative_support"][0]["source_projection"] == model["source_projection"]


@pytest.mark.parametrize("tool", ["investigation_update", "evidence_detail", "local_python_repl"])
def test_authored_or_generic_text_does_not_become_declared_source_metadata(subject, tool):
    entry = issue(subject, tool, {"ok": True, "limitations": [UNSUPPORTED]})
    assert source_projection(entry) == {}


def test_failed_and_unexposed_sources_cannot_establish_a_warning(subject):
    entry = issue(
        subject,
        "analysis_query",
        {"ok": False, "context": context("failed"), "limitations": [UNSUPPORTED]},
    )
    assert source_projection(entry) == {}
    issue(
        subject,
        "analysis_query",
        {"ok": True, "context": context("unexposed"), "limitations": [WARNING]},
    )
    support, errors = subject.qa_evidence(
        AnalystReport(report="A qualitative answer.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert not errors and not support["qualitative_support"]


@pytest.mark.parametrize("damage", ["result", "code", "entry_scope"])
def test_projection_rejects_source_seal_changes(subject, damage):
    entry = issue(subject, "analysis_compare", compare_result(), analysis_context=context("bound"))
    changed = deepcopy(entry)
    if damage == "result":
        changed["result"]["baseline"]["analysis"]["metadata"]["limitations"] = [UNSUPPORTED]
    elif damage == "code":
        changed["code"] += " tampered"
    else:
        changed["analysis_context"]["filters"]["severity"] = "Critical"
    with pytest.raises(ValueError, match="seal"):
        source_projection(changed)


@pytest.mark.parametrize("damage", ["missing_side", "scenario", "dataset", "identity_conflict"])
def test_metadata_cannot_borrow_another_side_or_fallback_context(subject, damage):
    result = compare_result()
    if damage == "missing_side":
        del result["baseline"]["context"]
    elif damage == "scenario":
        result["baseline"]["context"]["scenario"] = "exclude_cost_10"
    elif damage == "dataset":
        result["comparison"]["context"]["dataset_version"] = "a-different-dataset"
    else:
        result["comparison"]["context"]["context_id"] = "january"
    entry = issue(subject, "analysis_compare", result, analysis_context=context("fallback"))
    with pytest.raises(ValueError, match="context|dataset"):
        source_projection(entry)
    subject.exposed_receipts.add(entry["execution_id"])
    support, errors = subject.qa_evidence(
        AnalystReport(report="An unverified explanation.", claims=[]),
        delivery={"source_tables": [], "artifacts": []},
    )
    assert errors and not support["qualitative_support"]


def test_every_rank_change_remains_visible_without_issuing_navigation_as_measurements(subject):
    result = rank_result()
    entry = issue(subject, "scenario_compare", result)
    original = canonical(result)
    projection = source_projection(entry)
    ranks = {group["path"].rsplit(".", 1)[-1]: group for group in projection["ranking_changes"]}
    products = ranks["products_by_known_cost"]
    assert products["columns"] == ["position", "canonical", "selected"]
    assert [row[0] for row in products["changed_positions"]] == [13, 14, 21, 22]
    assert products["complete_source_arrays"] is True
    assert products["canonical_size"] == products["selected_size"] == 25
    assert products["context_ids"] == ["canonical", "financial"]
    assert not ranks["lines_by_reference_intensity"]["changed_positions"]
    for position, before, after in products["changed_positions"]:
        index = position - 1
        assert before == result_at_path(result, products["path"] + f".canonical[{index}]")
        assert after == result_at_path(result, products["path"] + f".selected[{index}]")
    view = subject.model_receipt(entry)
    visible = view["result"]["comparison"]["rankings"]["products_by_known_cost"]
    for side in ("canonical", "selected"):
        expected = result["comparison"]["rankings"]["products_by_known_cost"][side]
        assert visible[side] == expected[: len(visible[side])]
    assert all("position" not in value["path"] for value in view["numeric_evidence"])
    assert canonical(entry["result"]) == original
    assert len(canonical(view).encode()) <= VIEW_BYTES
    for number in view["numeric_evidence"]:
        assert result_at_path(result, number["path"]) == number["value"]


@pytest.mark.parametrize("damage", ["missing_array", "duplicate", "flag", "preview"])
def test_sampled_or_invalid_rank_source_never_proves_exclusivity(subject, damage):
    result = rank_result()
    product = result["comparison"]["rankings"]["products_by_known_cost"]
    if damage == "missing_array":
        del product["selected"]
    elif damage == "duplicate":
        product["selected"][0] = product["selected"][1]
    elif damage == "flag":
        product["changed"] = False
    else:
        entry = issue(subject, "evidence_detail", {"ok": True, "selected": product})
        assert source_projection(entry) == {}
        return
    with pytest.raises(ValueError, match="Ranking"):
        source_projection(issue(subject, "scenario_compare", result))


def test_projection_does_not_change_nulls_ties_extrema_or_source_rows(subject):
    entry = issue(subject, "analysis_compare", compare_result())
    paths = ["$.decomposition.rows[14].event_count_change"]
    before = qa_comparisons(entry, paths)
    source_projection(entry)
    after = qa_comparisons(entry, paths)
    assert after == before
    group = next(group for group in after if group["path"] == "$.decomposition.rows")
    extreme = next(row for row in group["extrema"] if row["field"] == "$.event_count_change")
    assert extreme["maximum"]["value"] == 3
    assert extreme["maximum"]["matching_rows"] == 2
    assert group["population_size"] == 15
    assert all(row["known_repair_cost_change"] is None for row in group["rows"])
    value, omitted = bounded_result(entry)
    assert (
        view_weight(value, numeric_values(value), omitted)
        + len(canonical(source_projection(entry)).encode())
        <= VIEW_BYTES
    )


class CaptureQA(Model):
    def __init__(self, runtime, role, seen):
        self.runtime, self.role, self.seen = runtime, role, seen
        self.usage = []

    async def get_response(self, system_instructions, input, **_kwargs):
        if self.role == "QA Verifier":
            import json

            raw = input if isinstance(input, str) else input[-1]["content"]
            data = json.loads(raw)
            self.seen.append((system_instructions, data))
            output = QAReview(verified=True, errors=[], suggestions=["Scripted transport fixture."])
        else:
            output = ReportDraft(
                report="The retrieved source warns that reporting completeness is unknown."
            )
        return ModelResponse(
            output=[_message(output.model_dump_json())],
            usage=Usage(requests=0),
            response_id="offline-projection-v8-" + self.role.replace(" ", "_"),
        )

    async def stream_response(self, *_args, **_kwargs):
        raise NotImplementedError
        yield


@pytest.mark.asyncio
async def test_real_sdk_qa_receives_the_same_source_declarations(subject, monkeypatch):
    entry = issue(subject, "analysis_compare", compare_result())
    view = subject.model_receipt(entry)
    seen = []

    def model(role):
        scripted = CaptureQA(subject, role, seen)
        subject.models.append(scripted)
        return scripted

    monkeypatch.setattr(subject, "model", model)
    result = await subject.run("Explain the reporting limitation, without claiming a cause.")
    assert result["status"] == "completed", result.get("qa")
    assert len(seen) == 1
    instructions, qa = seen[0]
    assert qa["qualitative_support"][0]["source_projection"] == view["source_projection"]
    assert "false rankings or exclusivity" in instructions
    assert "Weak qualified wording" in instructions
    assert "causal assertions remain blocking" in instructions
    assert "source_projection" in prompts.DRAFT_QA


@pytest.mark.parametrize("variant", ["exact", "different", "missing"])
def test_definition_reference_requires_the_actual_same_map(subject, variant):
    result = compare_result()
    definitions = result["baseline"]["analysis"]["definitions"]
    supplied = (
        definitions
        if variant == "exact"
        else {"event_count": "A different definition"}
        if variant == "different"
        else None
    )
    projection = source_projection(
        issue(subject, "analysis_compare", result), canonical_definitions=supplied
    )
    records = [
        record
        for record in projection["declared_metadata"]
        if record["field"] == "definitions"
        and any("baseline.analysis.definitions" in source["path"] for source in record["sources"])
    ]
    assert len(records) == 1
    record = records[0]
    if variant == "exact":
        assert record["value_reference"] == "canonical_definitions"
        assert record["value_sha256"] == sha256(canonical(supplied))
        assert "value" not in record
    else:
        assert record["value"] == definitions and "value_reference" not in record


@pytest.mark.asyncio
async def test_every_scalar_array_item_remains_citable_through_bounded_detail(subject):
    import json

    result = rank_result()
    entry = issue(subject, "scenario_compare", result)
    original = canonical(result)
    view = subject.model_receipt(entry)
    for side in ("canonical", "selected"):
        path = "$.comparison.rankings.products_by_known_cost." + side
        offset, recovered = 0, []
        while True:
            encoded = await subject._call(
                "evidence_detail",
                {"receipt_id": entry["execution_id"], "path": path, "offset": offset, "limit": 100},
            )
            assert len(encoded.encode("utf-8")) <= VIEW_BYTES
            detail = json.loads(encoded)
            assert detail["status"] == "completed", detail
            recovered.extend(item["value"] for item in detail["result"]["selected"])
            for number in detail["numeric_evidence"]:
                assert result_at_path(result, number["path"]) == number["value"]
            offset = detail["page"]["next_offset"]
            if offset is None:
                break
        assert recovered == result_at_path(result, path)
    assert view["source_projection"] == source_projection(entry)
    assert canonical(entry["result"]) == original


@pytest.mark.asyncio
async def test_definition_references_resolve_on_current_sdk_calls_after_compaction(
    evidence_subject, monkeypatch
):
    import json

    from openai.types.responses import ResponseFunctionToolCall

    runtime = evidence_subject[0]
    definitions = runtime.source_definitions()
    seen = []
    runtime.initial_items = compact_items(
        [
            {"role": "user", "content": "old definitions that must not be reused"},
            {"type": "compaction", "id": "cmp_offline", "encrypted_content": "synthetic-opaque"},
        ]
    )

    class DefinitionModel(Model):
        def __init__(self, role):
            self.role, self.step, self.usage = role, 0, []

        async def get_response(self, system_instructions, input, **kwargs):
            self.step += 1
            if self.role == "QA Verifier":
                data = json.loads(input if isinstance(input, str) else input[-1]["content"])
                assert data["canonical_definitions"] == definitions
                references = [
                    record
                    for item in data["qualitative_support"]
                    for record in item.get("source_projection", {}).get("declared_metadata", [])
                    if "value_reference" in record
                ]
                assert references and all(
                    record["value_sha256"] == sha256(canonical(definitions))
                    for record in references
                )
                seen.append("qa")
                output = [_message(QAReview(verified=True, errors=[]).model_dump_json())]
            else:
                assert canonical({"canonical_definitions": definitions}) in system_instructions
                assert "old definitions that must not be reused" not in canonical(input)
                for item in input if isinstance(input, list) else []:
                    if isinstance(item, dict) and item.get("type") == "function_call_output":
                        assert len(item["output"].encode("utf-8")) <= VIEW_BYTES
                        current = json.loads(item["output"])
                        for record in current.get("source_projection", {}).get(
                            "declared_metadata", []
                        ):
                            if "value_reference" in record:
                                assert record["value_sha256"] == sha256(canonical(definitions))
                seen.append("lead")
                if self.step == 1:
                    output = [
                        ResponseFunctionToolCall(
                            id="fc_definition",
                            call_id="call_definition",
                            name="analysis_query",
                            arguments=canonical(
                                {"section": "products", "filters": filters(), "plan": None}
                            ),
                            type="function_call",
                            status="completed",
                        )
                    ]
                else:
                    output = [
                        _message(
                            ReportDraft(
                                report="The selected observations describe logged events and retain their frozen source definitions."
                            ).model_dump_json()
                        )
                    ]
            return ModelResponse(
                output=output,
                usage=Usage(requests=0),
                response_id=f"offline-definitions-{self.role}-{self.step}",
            )

        async def stream_response(self, *args, **kwargs):
            raise NotImplementedError
            yield

    models = {role: DefinitionModel(role) for role in ("Lead Analyst", "QA Verifier")}
    monkeypatch.setattr(runtime, "model", lambda role: models[role])
    result = await runtime.run("Review the descriptive evidence, keeping the current definitions.")
    assert result["status"] == "completed", result.get("qa")
    assert seen == ["lead", "lead", "qa"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_every_advertised_section_and_table_fits_the_exact_tool_output(
    evidence_subject, selected
):
    runtime, tools, _, _, _, _, _ = evidence_subject
    requested = (
        filters(
            start_month="2024-06",
            end_month="2024-06",
            product_id="63",
            scenario="exclude_cost_10",
        )
        if selected
        else filters()
    )
    tables = []
    for section in SECTIONS:
        encoded = await runtime._call(
            "analysis_query", {"section": section, "filters": requested, "plan": None}
        )
        assert len(encoded.encode("utf-8")) <= VIEW_BYTES, section
        view = json.loads(encoded)
        assert view["status"] == "completed", (section, view)
        entry = next(
            entry for entry in tools.ledger.records() if entry["execution_id"] == view["receipt_id"]
        )
        for number in view["numeric_evidence"]:
            assert result_at_path(entry["result"], number["path"]) == number["value"]
        for record in view["source_projection"]["declared_metadata"]:
            original = (
                runtime.source_definitions()
                if record.get("value_reference") == "canonical_definitions"
                else record["value"]
            )
            for source in record["sources"]:
                assert result_at_path(entry["result"], source["path"]) == original
        if section == "monthly":
            tables.append((view["receipt_id"], "$.analysis.monthly"))
    dashboard = await runtime._call("dashboard_context", {})
    assert len(dashboard.encode("utf-8")) <= VIEW_BYTES
    view = json.loads(dashboard)
    assert view["status"] == "completed", view
    tables.append((view["receipt_id"], "$.monthly"))
    for receipt_id, path in tables:
        for limit in (1, 100):
            encoded = await runtime._call(
                "report_table",
                {
                    "receipt_id": receipt_id,
                    "path": path,
                    "columns": ["month", "event_count"],
                    "offset": 0,
                    "limit": limit,
                },
            )
            assert len(encoded.encode("utf-8")) <= VIEW_BYTES
            view = json.loads(encoded)
            assert view["status"] == "completed" and view["result"]["ok"] is True, view
    assert tools.ledger.verify_integrity() == []


def test_opaque_category_keys_stay_visible_without_invented_numeric_paths(subject):
    categories = {"Category A": 7, "Line-1": 7, "Category.A": 7, "safe_key": 7}
    original = {
        "ok": True,
        "context": context("bound"),
        "analysis": {"quality": {"products_by_category": categories}},
    }
    entry = issue(subject, "analysis_query", original)
    view = subject.model_receipt(entry)
    assert view["result"]["analysis"] == original["analysis"]
    assert "scoped analysis query" in view["numeric_evidence_note"]
    assert "no direct claim IDs" in view["numeric_evidence_note"]
    assert [(number["path"], number["value"]) for number in view["numeric_evidence"]] == [
        ("$.analysis.quality.products_by_category.safe_key", 7)
    ]
    assert subject.toolkit.ledger.verify_claims(
        [claim.model_dump() for claim in subject.available_claims().values()]
    )["verified"]
    for number in view["numeric_evidence"]:
        assert result_at_path(entry["result"], number["path"]) == number["value"]
    assert entry["result"] == original


@pytest.mark.parametrize(
    "values,ambiguous",
    [({"A.B": 7, "A": {"B": 7}}, "$.analysis.A.B"), ({"A[0]": 7, "A": [7]}, "$.analysis.A[0]")],
)
def test_punctuation_alias_cannot_borrow_an_equal_nested_value(subject, values, ambiguous):
    original = {"ok": True, "context": context("bound"), "analysis": {**values, "safe_key": 7}}
    entry = issue(subject, "analysis_query", original)
    view = subject.model_receipt(entry)
    assert view["result"]["analysis"] == original["analysis"]
    assert "ambiguous literal keys" in view["numeric_evidence_note"]
    assert all(number["path"] != ambiguous for number in view["numeric_evidence"])
    assert [(number["path"], number["value"]) for number in view["numeric_evidence"]] == [
        ("$.analysis.safe_key", 7)
    ]
    assert entry["result"] == original


@pytest.mark.parametrize("changed", [7.0, 8])
def test_valid_path_with_changed_numeric_type_or_value_fails_before_issuing_ids(
    subject, monkeypatch, changed
):
    entry = issue(
        subject,
        "analysis_query",
        {"ok": True, "context": context("bound"), "analysis": {"event_count": 7}},
    )
    altered = deepcopy(entry["result"])
    altered["analysis"]["event_count"] = changed
    monkeypatch.setattr("analyst.runtime.bounded_result", lambda *_args, **_kwargs: (altered, []))
    with pytest.raises(ValueError, match="differs from original sealed evidence"):
        subject.model_receipt(entry)
    assert not subject.available_claims()
    assert subject.toolkit.ledger.verify_integrity() == []


def test_absent_selected_numeric_path_is_a_projection_error_not_an_opaque_key(subject):
    entry = issue(
        subject,
        "analysis_query",
        {"ok": True, "context": context("bound"), "analysis": {"event_count": 7}},
    )
    with pytest.raises(ValueError, match="not present in original sealed evidence"):
        subject.model_receipt(entry, [("$.analysis.not_in_the_source", 7)])
    assert not subject.available_claims()
    assert subject.toolkit.ledger.verify_integrity() == []
