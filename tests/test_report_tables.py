"""Source row/cell binding, inert strings and exact selected artifact identity."""

import asyncio
import json
import sys
from html.parser import HTMLParser
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from analyst.config import Settings
from analyst.evidence import EvidenceLedger, sha256
from analyst.runtime import (
    AnalystReport,
    AnalystRuntime,
    ReportDraft,
    audit_exported_reports,
    audit_report,
    audit_report_links,
    render_business_report,
    source_table_catalog,
)


async def emit(*_):
    pass


def runtime_at(path, language="en"):
    ledger = EvidenceLedger(path)
    return AnalystRuntime(
        Settings(mode="mock"),
        SimpleNamespace(ledger=ledger, run_dir=path, input_paths=[], schemas=[]),
        emit,
        language=language,
    )


def source(runtime, rows=None, *, tool="local_python_repl", **metadata):
    if rows is None:
        rows = [
            {"category": "Structural", "before": 66, "after": 77, "delta": 11},
            {"category": "Functional", "before": 77, "after": 78, "delta": 1},
        ]
    return runtime.toolkit.ledger.record(
        tool, "# actual synthetic calculation", {"ok": True, "rows": rows}, **metadata
    )


def issue(runtime, entry, *, columns=None, path="$.rows", offset=0, limit=100):
    columns = columns or ["category", "before", "after", "delta"]
    result = json.loads(
        asyncio.run(
            runtime._call(
                "report_table",
                {
                    "receipt_id": entry["execution_id"],
                    "path": path,
                    "columns": columns,
                    "offset": offset,
                    "limit": limit,
                },
            )
        )
    )
    return result["result"]


def resolve(runtime, issued, **kwargs):
    return runtime.resolve_report(ReportDraft(report=issued["placeholder"], **kwargs))


def selected_file(runtime, name, text):
    path = runtime.toolkit.run_dir / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return runtime.toolkit.ledger.register_artifact(path)


class Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells, self.current, self.links = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.current = ""
        if tag == "a":
            self.links.append(dict(attrs).get("href"))

    def handle_data(self, value):
        if self.current is not None:
            self.current += value

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.cells.append(self.current)
            self.current = None


def test_table_preserves_original_rows_columns_and_claim_paths(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = source(runtime)
    issued = issue(runtime, entry)
    report = resolve(runtime, issued)
    audit = audit_report(report, runtime.toolkit.ledger)
    assert audit["verified"], audit
    assert report.report == issued["markdown"]
    assert "Structural | 66 " in report.report and "Functional | 77 " in report.report
    assert len(report.claims) == 6
    assert all(claim.execution_id == entry["execution_id"] for claim in report.claims)
    assert {claim.path for claim in report.claims} == {
        f"$.rows[{index}].{key}" for index in range(2) for key in ("before", "after", "delta")
    }
    assert audit["source_tables"] == [issued["table"]]


def test_canonical_numeric_identifier_columns_keep_typed_source_identity(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = source(
        runtime,
        [{"defect_id": 12, "product_id": 86, "event_count": 1}],
        tool="analysis_records",
        execution_kind="bounded_analysis",
    )
    issued = issue(runtime, entry, columns=["defect_id", "product_id", "event_count"])
    report = resolve(runtime, issued)
    checked = audit_report(report, runtime.toolkit.ledger)
    assert checked["verified"], checked
    assert "| 12 [claim:" in report.report
    assert "defect_id 12" not in report.report
    unsupported = AnalystReport(
        report=report.report + "\nThe count was 86 events.", claims=report.claims
    )
    assert not audit_report(unsupported, runtime.toolkit.ledger)["verified"]


@pytest.mark.parametrize(
    "mutation",
    [
        "swap_cells",
        "rename_row",
        "rename_header",
        "reorder_rows",
        "remove_start",
        "remove_end",
        "unknown_id",
        "trailing_marker_space",
        "duplicate_end",
    ],
)
def test_marked_table_mutations_fail_even_when_every_scalar_claim_is_valid(tmp_path, mutation):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    report = resolve(runtime, issued)
    text = report.report
    lines = text.splitlines()
    if mutation == "swap_cells":
        cells = lines[3].split(" | ")
        cells[1], cells[2] = cells[2], cells[1]
        lines[3] = " | ".join(cells)
    elif mutation == "rename_row":
        lines[3] = lines[3].replace("Structural", "Functional")
    elif mutation == "rename_header":
        lines[1] = lines[1].replace("before", "after")
    elif mutation == "reorder_rows":
        lines[3], lines[4] = lines[4], lines[3]
    elif mutation == "remove_start":
        lines.pop(0)
    elif mutation == "remove_end":
        lines.pop()
    elif mutation == "unknown_id":
        lines = [line.replace(issued["table"]["id"], "f" * 24) for line in lines]
    elif mutation == "trailing_marker_space":
        lines[0] += " "
    else:
        lines.append(lines[-1])
    report.report = "\n".join(lines)
    assert runtime.toolkit.ledger.verify_claims([claim.model_dump() for claim in report.claims])[
        "verified"
    ]
    audit = audit_report(report, runtime.toolkit.ledger)
    assert not audit["verified"] and any("source-table" in error for error in audit["errors"])
    with pytest.raises(ValueError, match="Source-table verification"):
        render_business_report(report.report, runtime.toolkit.ledger)


def test_removing_both_markers_is_ordinary_prose_without_structural_certification(tmp_path):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    report = resolve(runtime, issued)
    report.report = "\n".join(report.report.splitlines()[1:-1])
    audit = audit_report(report, runtime.toolkit.ledger)
    assert audit["verified"] and audit["source_tables"] == []


@pytest.mark.parametrize(
    "text",
    [
        "[table:unknown]",
        "[table:" + "f" * 24 + "]",
        "See [table:" + "f" * 24 + "]",
        "[TABLE:bad]",
        "[table:incomplete",
    ],
)
def test_unresolved_unknown_inline_and_malformed_table_tokens_fail(tmp_path, text):
    runtime = runtime_at(tmp_path)
    report = runtime.resolve_report(ReportDraft(report=text))
    assert not audit_report(report, runtime.toolkit.ledger)["verified"]
    with pytest.raises(ValueError, match="Source-table verification"):
        render_business_report(report.report, runtime.toolkit.ledger)


def test_cross_run_tokens_and_copied_binding_receipts_are_rejected(tmp_path):
    first, second = runtime_at(tmp_path / "first"), runtime_at(tmp_path / "second")
    entry = source(first)
    issued = issue(first, entry)
    assert not audit_report(resolve(second, issued), second.toolkit.ledger)["verified"]
    second.toolkit.ledger.path.write_bytes(first.toolkit.ledger.path.read_bytes())
    assert second.toolkit.ledger.verify_integrity() == []
    assert not audit_report(resolve(second, issued), second.toolkit.ledger)["verified"]


def test_source_or_binding_drift_fails_before_masking_source_numbers(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = source(runtime, [{"label": "00123", "value": 8}])
    issued = issue(runtime, entry, columns=["label", "value"])
    report = resolve(runtime, issued)
    records = runtime.toolkit.ledger.records()
    records[0]["result"]["rows"][0]["label"] = "00999"
    runtime.toolkit.ledger.path.write_text(
        "\n".join(json.dumps(row) for row in records), encoding="utf-8"
    )
    audit = audit_report(report, runtime.toolkit.ledger)
    assert not audit["verified"]
    assert any("mismatch" in error for error in audit["errors"])
    assert any("source-table" in error for error in audit["errors"])


@pytest.mark.parametrize(
    "tool", ["operations_create_draft", "draft_semantic_review", "report_table", "todo_planner"]
)
def test_noncomputational_receipts_cannot_become_source_tables(tmp_path, tool):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime, tool=tool))
    assert issued["ok"] is False
    assert source_table_catalog(runtime.toolkit.ledger) == {}


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "object",
        "missing_key",
        "nested_cell",
        "duplicate_column",
        "alias",
        "wildcard",
        "negative_offset",
        "oversize_page",
        "bool_offset",
        "controls",
        "oversize_text",
        "large_int",
    ],
)
def test_invalid_selection_never_issues_a_table(tmp_path, change):
    runtime = runtime_at(tmp_path)
    entry = source(runtime)
    arguments = {
        "receipt_id": entry["execution_id"],
        "path": "$.rows",
        "columns": ["category", "before"],
        "offset": 0,
        "limit": 100,
    }
    if change == "failed":
        entry = runtime.toolkit.ledger.record(
            "local_python_repl", "# failure", {"ok": False, "rows": entry["result"]["rows"]}
        )
        arguments["receipt_id"] = entry["execution_id"]
    elif change == "object":
        arguments["path"] = "$"
    elif change == "missing_key":
        arguments["columns"] = ["does_not_exist"]
    elif change in {"nested_cell", "controls", "oversize_text", "large_int"}:
        value = (
            {"nested": 1}
            if change == "nested_cell"
            else "A\nB"
            if change == "controls"
            else "x" * 24001
            if change == "oversize_text"
            else 2**53 + 1
        )
        arguments["receipt_id"] = source(runtime, [{"category": value, "before": 1}])[
            "execution_id"
        ]
    elif change == "duplicate_column":
        arguments["columns"] = ["before", "before"]
    elif change == "alias":
        arguments["columns"] = [{"key": "before", "label": "After"}]
    elif change == "wildcard":
        arguments["path"] = "$.rows[*]"
    elif change == "negative_offset":
        arguments["offset"] = -1
    elif change == "oversize_page":
        arguments["limit"] = 101
    else:
        arguments["offset"] = True
    issued = json.loads(asyncio.run(runtime._call("report_table", arguments)))["result"]
    assert issued["ok"] is False
    assert source_table_catalog(runtime.toolkit.ledger) == {}


@pytest.mark.parametrize("separator", ["\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_unicode_separator_receipts_remain_readable_and_table_rejection_is_recorded(
    tmp_path, separator, line_ending
):
    runtime = runtime_at(tmp_path)
    label = "A" + separator + "B"
    entry = source(runtime, [{"label": label, "count": 1}])
    ledger = runtime.toolkit.ledger
    # Exercise existing physical LF and CRLF JSONL without changing source text.
    before = ledger.path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", line_ending)
    ledger.path.write_bytes(before)
    assert ledger.records() == [entry]
    assert ledger.records()[0]["result"]["rows"][0]["label"] == label
    assert ledger.verify_integrity() == []
    issued = issue(runtime, entry, columns=["label", "count"])
    assert issued["ok"] is False and "single line without control" in issued["error"]
    records = ledger.records()
    assert len(records) == 2 and records[0] == entry
    assert records[1]["tool"] == "report_table" and records[1]["result"] == issued
    assert ledger.path.read_bytes().startswith(before)
    assert ledger.verify_integrity() == []
    assert source_table_catalog(ledger) == {}


@pytest.mark.parametrize("language", ["en", "vi"])
def test_tricky_source_strings_are_inert_exact_typed_html_cells(tmp_path, language):
    values = [
        "00123",
        "2024-01-02",
        "A|B",
        r"A\|B",
        "`code` **bold**",
        "[claim:fake] [value:fake] [table:fake]",
        "<!-- evidence-table:" + "a" * 24 + ":start -->",
        "<script>alert(1)</script>",
        "&lt;tag&gt; &#91;literal&#93;",
        "[click](https://example.org/unsafe)",
        "https://example.org/unsafe",
        "a@example.org",
        "  leading and trailing  ",
        "Tiếng Việt chính xác",
    ]
    runtime = runtime_at(tmp_path, language)
    entry = source(runtime, [{"source_label": value, "count": 12} for value in values])
    issued = issue(runtime, entry, columns=["source_label", "count"])
    report = resolve(runtime, issued)
    assert audit_report(report, runtime.toolkit.ledger)["verified"]
    assert audit_report_links(report.report, []) == []
    assert "[claim:fake]" not in report.report and "<script>" not in report.report
    parser = Cells()
    html = render_business_report(
        report.report, runtime.toolkit.ledger, language=language, claims=report.claims
    )
    parser.feed(html)
    assert parser.cells == ["source_label", "count"] + [
        cell for value in values for cell in (value, "12")
    ]
    assert parser.links == [] and "<script>" not in html
    packet, errors = runtime.begin_delivery_proposal(report)
    assert not errors and packet["artifacts"] == []
    assert packet["source_tables"] == [issued["table"]]
    assert not (tmp_path / "artifacts/business_report.html").exists()


def test_source_string_digits_mask_only_inside_an_exact_verified_block(tmp_path):
    runtime = runtime_at(tmp_path)
    issued = issue(
        runtime, source(runtime, [{"label": "00123", "cost": None}]), columns=["label", "cost"]
    )
    report = resolve(runtime, issued)
    assert report.claims == [] and audit_report(report, runtime.toolkit.ledger)["verified"]
    report.report += "\nThere were 00123 events and 0 costs."
    audit = audit_report(report, runtime.toolkit.ledger)
    assert not audit["verified"]
    assert any("00123" in error for error in audit["errors"]) and any(
        "'0'" in error for error in audit["errors"]
    )


def test_selected_markdown_requires_exact_complete_block_and_is_never_rewritten(tmp_path):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    artifact = selected_file(runtime, "summary.md", "# Summary\n\n" + issued["markdown"])
    report = AnalystReport(
        report="See the summary.", claims=[], deliverable_paths=[artifact["path"]]
    )
    before = (tmp_path / artifact["path"]).read_bytes()
    packet, errors = runtime.begin_delivery_proposal(report)
    assert not errors
    audit = audit_exported_reports(
        report,
        runtime.toolkit.ledger,
        artifacts=packet["artifacts"],
        claim_catalog=runtime.available_claims(),
    )
    assert audit["verified"] and audit["artifacts"][0]["source_tables"] == [issued["table"]]
    packet, errors = runtime.delivery_packet(report, narrative_audit=audit)
    assert not errors and packet["source_tables"] == [issued["table"]]
    assert (tmp_path / artifact["path"]).read_bytes() == before
    changed = before.decode().replace("Structural", "Changed")
    changed_artifact = selected_file(runtime, "summary.md", changed)
    assert runtime.delivery_packet(report)[1]
    audit = audit_exported_reports(
        report,
        runtime.toolkit.ledger,
        artifacts=[changed_artifact],
        claim_catalog=runtime.available_claims(),
    )
    assert (
        not audit["verified"]
        and (tmp_path / artifact["path"]).read_text(encoding="utf-8") == changed
    )


@pytest.mark.parametrize("replacement,verified", [("\r\n", True), ("\r", False), (" \n", False)])
def test_markdown_only_normalizes_crlf_while_preserving_actual_file_bytes(
    tmp_path, replacement, verified
):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    body = issued["markdown"].replace("\n", replacement)
    artifact = selected_file(runtime, "windows.md", body)
    before = (tmp_path / artifact["path"]).read_bytes()
    report = AnalystReport(
        report="Requested summary.", claims=[], deliverable_paths=[artifact["path"]]
    )
    audit = audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.available_claims()
    )
    assert audit["verified"] is verified
    assert (tmp_path / artifact["path"]).read_bytes() == before
    assert runtime.toolkit.ledger.artifacts()[0]["sha256"] == sha256(before)


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises the native Windows text writer")
@pytest.mark.parametrize("mutation", [None, "cell", "whitespace", "lone_cr"])
def test_windows_text_written_tables_preserve_native_bytes_and_reject_mutations(tmp_path, mutation):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    text = issued["markdown"]
    if mutation == "cell":
        text = text.replace("Structural", "Changed")
    elif mutation == "whitespace":
        text = text.replace("Structural", "Structural ")
    elif mutation == "lone_cr":
        text = text.replace("\n", "\r")
    path = tmp_path / "artifacts" / "native-windows.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    artifact = runtime.toolkit.ledger.register_artifact(path)
    before = path.read_bytes()
    assert (b"\r\n" in before) is (mutation != "lone_cr")
    report = AnalystReport(
        report="Requested summary.", claims=[], deliverable_paths=[artifact["path"]]
    )
    audit = audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.available_claims()
    )
    assert audit["verified"] is (mutation is None)
    assert path.read_bytes() == before
    assert runtime.toolkit.ledger.artifacts()[0]["sha256"] == sha256(before)


@pytest.mark.parametrize("suffix", ["md", "html", "docx", "pdf"])
def test_custom_files_do_not_expand_unresolved_table_tokens(tmp_path, suffix):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime))
    text = issued["placeholder"]
    path = tmp_path / "artifacts" / ("summary." + suffix)
    path.parent.mkdir()
    if suffix in {"md", "html"}:
        path.write_text(text if suffix == "md" else "<p>" + text + "</p>", encoding="utf-8")
    elif suffix == "docx":
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                + text
                + "</w:t></w:r></w:p></w:body></w:document>",
            )
    else:
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=900, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 10 Tf 20 150 Td (" + text + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(path)
    artifact = runtime.toolkit.ledger.register_artifact(path)
    before = path.read_bytes()
    report = AnalystReport(
        report="Selected output.", claims=[], deliverable_paths=[artifact["path"]]
    )
    audit = audit_exported_reports(
        report, runtime.toolkit.ledger, claim_catalog=runtime.available_claims()
    )
    assert not audit["verified"] and path.read_bytes() == before


def test_text_null_boolean_table_contributes_provenance_but_unused_table_does_not(tmp_path):
    runtime = runtime_at(tmp_path)
    unused = issue(runtime, source(runtime))
    scope = {"start_month": "2024-01", "end_month": "2024-06"}
    entry = source(
        runtime,
        [{"label": "00123", "missing": None, "enabled": False}],
        tool="dashboard_context",
        execution_kind="canonical_receipt_import",
        analysis_context={
            "filters": scope,
            "snapshot_id": "snapshot",
            "source_sha256": "source",
            "master_sha256": "master",
            "kpi_version": "version",
            "context_id": "synthetic-table-context",
            "dataset_version": "synthetic-table-dataset",
            "policy_version": "synthetic-table-policy",
            "scenario": "",
        },
    )
    issued = issue(runtime, entry, columns=["label", "missing", "enabled"])
    report = resolve(runtime, issued)
    assert report.claims == []
    delivery, errors = runtime.begin_delivery_proposal(report)
    assert not errors and delivery["scope"] == scope
    assert delivery["source_execution_ids"] == [entry["execution_id"]]
    assert (
        delivery["source_tables"] == [issued["table"]]
        and unused["table"] not in delivery["source_tables"]
    )
    evidence, errors = runtime.qa_evidence(report, delivery=delivery)
    assert not errors and len(evidence["table_support"]) == 1
    assert evidence["table_support"][0]["selected_rows"][0]["values"] == {
        "label": "00123",
        "missing": None,
        "enabled": False,
    }
    assert evidence["delivery"]["source_tables"] == [issued["table"]]
    assert issued["table"]["id"] in runtime.toolkit.ledger.path.read_text(encoding="utf-8")
    assert not (tmp_path / "artifacts/verification_scope.json").exists()
    assert not (tmp_path / "artifacts/verified_evidence.zip").exists()


def test_qa_has_complete_small_source_comparators_for_a_partial_display_page(tmp_path):
    runtime = runtime_at(tmp_path)
    issued = issue(runtime, source(runtime), offset=0, limit=1)
    report = resolve(runtime, issued)
    evidence, errors = runtime.qa_evidence(report)
    assert not errors
    support = evidence["table_support"][0]
    assert len(support["selected_rows"]) == 1 and support["total_source_rows"] == 2
    assert support["comparison_rows_complete"] and support["comparison_rows"][1]["before"] == 77
    assert support["selection"]["columns"] == ["category", "before", "after", "delta"]


def test_qa_marks_large_source_comparators_incomplete_without_inventing_an_extremum(tmp_path):
    runtime = runtime_at(tmp_path)
    entry = source(runtime, [{"label": "row", "value": index} for index in range(101)])
    issued = issue(runtime, entry, columns=["label", "value"], offset=10, limit=1)
    evidence, errors = runtime.qa_evidence(resolve(runtime, issued))
    assert not errors
    support = evidence["table_support"][0]
    assert support["total_source_rows"] == 101 and support["comparison_rows_complete"] is False
    assert support["comparison_rows"] == [] and support["selected_rows"][0]["path"] == "$.rows[10]"


def test_report_table_tool_is_available_in_standalone_and_dashboard_modes(tmp_path):
    for context in (None, {"context": {"filters": {}}}):
        runtime = runtime_at(tmp_path)
        runtime._analysis_context_json = json.dumps(context) if context else None
        names = {tool.name for tool in runtime.sdk_tools()}
        assert "report_table" in names


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("field", ["title", "description", "proposed_owner", "action_plan"])
@pytest.mark.parametrize("content", ["placeholder", "block"])
def test_report_tables_never_enter_draft_fields_or_trigger_prewrite_qa(
    tmp_path, monkeypatch, operation, field, content
):
    from test_draft_evidence import database_rows, draft, make_runtime

    async def scenario():
        runtime, operations, ids = await make_runtime(tmp_path / "creator")
        saved = None
        if operation == "update":
            saved = (await runtime.operation("operations_create_draft", draft(ids)))["case"]
            runtime, _, ids = await make_runtime(tmp_path / "updater", "draft_update")
            runtime.operations_service = operations
        issued = json.loads(
            await runtime._call(
                "report_table",
                {
                    "receipt_id": runtime.context_entry["execution_id"],
                    "path": "$.monthly",
                    "columns": ["month", "event_count"],
                    "offset": 0,
                    "limit": 1,
                },
            )
        )["result"]
        text = issued["placeholder"] if content == "placeholder" else issued["markdown"]
        before = database_rows(operations)

        async def no_review(*_):
            raise AssertionError("A report table in a draft must fail before semantic QA")

        monkeypatch.setattr(runtime, "review_draft_proposal", no_review)
        if operation == "create":
            values = draft(ids)
            values[field] = text
        else:
            values = {
                "case_id": saved["id"],
                "expected_version": saved["version"],
                "changes": [{"field": field, "value": text}],
            }
        result = json.loads(await runtime._call("operations_" + operation + "_draft", values))[
            "result"
        ]
        assert result["ok"] is False and result["write_occurred"] is False
        assert "belong in reports" in result["error"]
        assert database_rows(operations) == before

    asyncio.run(scenario())
