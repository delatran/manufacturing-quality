"""Exact verified URL identifiers do not hide neighboring numeric assertions."""

from html import escape
from zipfile import ZipFile

import pytest
from analyst.evidence import EvidenceLedger
from analyst.runtime import (
    AnalystReport,
    audit_exported_reports,
    audit_report,
    audit_report_links,
    verified_source_links,
)

SOURCE = "https://raw.example/research-2026/main/records_2024.json"


def source_ledger(tmp_path, *, succeeded=True):
    ledger = EvidenceLedger(tmp_path)
    ledger.record(
        "source_retrieval",
        "# synthetic source receipt",
        {"ok": succeeded},
        sources=[{"url": SOURCE}],
    )
    return ledger


@pytest.mark.parametrize(
    "text",
    [f"Source: {SOURCE}", f"Source: <{SOURCE}>.", f"[Source]({SOURCE})", f"Source ({SOURCE})."],
)
def test_exact_verified_source_urls_are_identifiers(tmp_path, text):
    ledger = source_ledger(tmp_path)
    assert audit_report(AnalystReport(report=text, claims=[]), ledger)["verified"]
    assert audit_report_links(text, verified_source_links(ledger)) == []


@pytest.mark.parametrize(
    "text",
    [
        f"Source {SOURCE}; 999 events.",
        f"[999 events]({SOURCE})",
        f"Source {SOURCE} USD999.",
        f"Source {SOURCE} event999.",
        f"Source {SOURCE}; 0 events.",
        f"Source {SOURCE}; cost 0.",
        f"Source {SOURCE}; 0%.",
    ],
)
def test_source_url_masking_keeps_visible_and_adjacent_measurements(tmp_path, text):
    ledger = source_ledger(tmp_path)
    audit = audit_report(AnalystReport(report=text, claims=[]), ledger)
    assert not audit["verified"] and any(
        "lacks matching cited evidence" in error for error in audit["errors"]
    )


@pytest.mark.parametrize(
    "target",
    [
        SOURCE + "999",
        SOURCE + "/999",
        SOURCE + "?count=999",
        SOURCE + "#999",
        SOURCE.replace("research-", "research%2D"),
        SOURCE.replace("raw.example", "other.example"),
        SOURCE.replace("records_", "other_"),
    ],
)
def test_near_source_urls_are_never_masked_by_prefix_or_alias(tmp_path, target):
    ledger = source_ledger(tmp_path)
    text = "Source: " + target
    assert not audit_report(AnalystReport(report=text, claims=[]), ledger)["verified"]
    assert audit_report_links(text, verified_source_links(ledger)) == [
        "Unverified source or download link: " + target
    ]


def test_failed_source_receipt_does_not_allow_exact_url(tmp_path):
    ledger = source_ledger(tmp_path, succeeded=False)
    assert verified_source_links(ledger) == []
    assert not audit_report(AnalystReport(report=SOURCE, claims=[]), ledger)["verified"]
    assert audit_report_links(SOURCE, [])


@pytest.mark.parametrize("suffix", [".md", ".html", ".docx", ".pdf"])
@pytest.mark.parametrize("numeric_suffix", ["", " 999 events."])
def test_selected_narrative_files_use_same_exact_url_and_numeric_rules(
    tmp_path, suffix, numeric_suffix
):
    ledger = source_ledger(tmp_path)
    path = tmp_path / ("artifacts/source_report" + suffix)
    path.parent.mkdir()
    text = "Source: " + SOURCE + numeric_suffix
    if suffix == ".md":
        path.write_text(text, encoding="utf-8")
    elif suffix == ".html":
        path.write_text("<p>" + escape(text) + "</p>", encoding="utf-8")
    elif suffix == ".docx":
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>"
                + escape(text)
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
    artifact = ledger.register_artifact(path)
    before = path.read_bytes()
    report = AnalystReport(
        report="Requested source report.", claims=[], deliverable_paths=[artifact["path"]]
    )
    audit = audit_exported_reports(report, ledger)
    assert audit["verified"] is (not numeric_suffix), audit
    assert path.read_bytes() == before
