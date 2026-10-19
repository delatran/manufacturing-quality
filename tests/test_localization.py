"""Response language is display metadata, never a change to canonical evidence."""

import asyncio
import json
import threading
import time
from copy import deepcopy

import pytest
from analyst.app import create_app
from analyst.config import Settings
from analyst.localization import (
    PHRASES,
    VI_NUMBER_NOTE,
    explicit_response_language,
    localize_report,
    response_language,
    text_language,
    vietnamese_metadata,
)
from analyst.runtime import AnalystReport, AnalystRuntime, audit_report
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from fastapi.testclient import TestClient
from test_runtime import FrozenAnalysisService, canonical_context


def test_translation_preserves_numbers_claim_identifiers_code_and_links():
    source = "Observed repair cost is 123,456.90 [claim:analysis.monthly[0].known_repair_cost]. `Critical 3.14` [evidence](report2024.md)"
    translated = localize_report(source, "vi")
    assert VI_NUMBER_NOTE not in translated
    assert "123,456.90 [claim:analysis.monthly[0].known_repair_cost]" in translated
    assert "`Critical 3.14`" in translated and "(report2024.md)" in translated
    assert localize_report(source) == source
    assert vietnamese_metadata("2024-06").startswith("tháng Sáu năm hai nghìn")
    assert vietnamese_metadata("Line-3") == "Dây chuyền ba"
    assert vietnamese_metadata("Category 2") == "Nhóm hai"
    assert vietnamese_metadata("Moderate") == "Trung bình"
    assert vietnamese_metadata("Visual Inspection") == "Kiểm tra trực quan"


def test_readable_metadata_translation_keeps_observed_values_and_protected_tokens():
    source = (
        "Part Three: trend\nLine Three: 123,456.90 [claim:known_3]. "
        "January through June of 2024. `Line Three: 999` [source](report2024.md)"
    )
    translated = localize_report(source, "vi")
    assert "Phần 3: xu hướng" in translated
    assert "Chuyền 3: 123,456.90 [claim:known_3]" in translated
    assert "tháng 1 – 6/2024" in translated
    assert "`Line Three: 999` [source](report2024.md)" in translated


def test_translation_rejects_changed_values_in_replacement_phrases(monkeypatch):
    monkeypatch.setitem(PHRASES, "Observed amount 123.45", "Chi phí ghi nhận 999.00")
    with pytest.raises(ValueError, match="changed numeric evidence tokens"):
        localize_report("Observed amount 123.45", "vi")
    monkeypatch.setitem(PHRASES, "Line Three", "Chuyền 4")
    with pytest.raises(ValueError, match="changed numeric evidence tokens"):
        localize_report("Line Three: 123.45", "vi")


def test_vietnamese_sdk_context_report_preserves_exact_canonical_receipt_and_claims(tmp_path):
    async def scenario():
        payload = canonical_context(tmp_path)
        original = deepcopy(payload)
        results = {}
        for language in ("en", "vi"):
            toolkit = AnalystTools(tmp_path / language, [], memory_enabled=False)
            events = []

            async def emit(kind, data, event_log=events):
                event_log.append((kind, data))

            runtime = AnalystRuntime(
                Settings(mode="mock", memory_enabled=False),
                toolkit,
                emit,
                language=language,
                analysis_context=payload,
                analysis_service=FrozenAnalysisService(payload),
            )
            try:
                with pytest.raises(AttributeError):
                    runtime.language = "vi" if language == "en" else "en"
                result = await runtime.run("Explain the selected Dashboard scope")
                assert result["status"] == "completed", result["qa"]
                assert result["language"] == language and result["qa"]["verified"]
                assert result["report_number_format"] == ("vi-VN" if language == "vi" else "en-US")
                assert result["analysis_context"] == original["context"]
                assert (
                    result["canonical_receipt"]["execution_id"]
                    == original["evidence"]["execution_id"]
                )
                assert (
                    result["canonical_receipt"]["code_sha256"]
                    == original["evidence"]["code_sha256"]
                )
                assert (
                    result["canonical_receipt"]["result_sha256"]
                    == original["evidence"]["result_sha256"]
                )
                assert toolkit.ledger.verify_claims(result["claims"])["verified"]
                if language == "vi":
                    assert "KPI theo định nghĩa chuẩn" in result["report"]
                    assert (
                        "Dây chuyền ba" in result["report"] and "tháng Một năm" in result["report"]
                    )
                    assert VI_NUMBER_NOTE not in result["report"]
                    assert any(
                        "đang kiểm tra bằng chứng" in data["delta"]
                        for kind, data in events
                        if kind == "reasoning_summary"
                    )
                    assert (
                        toolkit.todos[1]["title"] == "Đọc phạm vi chuẩn và kiểm tra bằng chứng KPI"
                    )
                    qa_files = list((toolkit.run_dir / "internal/qa-inputs").glob("*.json"))
                    assert len(qa_files) == 1
                    reviewed = json.loads(qa_files[0].read_text(encoding="utf-8"))
                    assert reviewed["language"] == "vi"
                    assert reviewed["report_number_format"] == "vi-VN"
                    assert result["artifacts"] == result["delivery"]["artifacts"] == []
                    assert not (toolkit.run_dir / "artifacts/verified_claims.json").exists()
                results[language] = result
            finally:
                await toolkit.close()
        assert results["en"]["claims"] == results["vi"]["claims"]
        assert payload == original

    asyncio.run(scenario())


def test_vietnamese_generic_sdk_report_runs_real_tools_and_rejects_changed_numbers(tmp_path):
    async def scenario():
        source = tmp_path / "disposable.csv"
        source.write_text("group,value\na,2\na,4\nb,6\n", encoding="utf-8")
        tools = AnalystTools(tmp_path / "run", [source], memory_enabled=False)

        async def emit(kind, data):
            pass

        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False), tools, emit, language="vi"
        )
        try:
            result = await runtime.run("Khảo sát cấu trúc dữ liệu và tạo thống kê mô tả")
            assert result["status"] == "completed", result["qa"]
            assert len(result["claims"]) == 6
            assert "Kết quả khảo sát dữ liệu" in result["report"] and "số dòng" in result["report"]
            assert tools.ledger.verify_claims(result["claims"])["verified"]
            invalid = AnalystReport.model_validate(
                {"report": "Số dòng: 999 [claim:observed_2]", "claims": result["claims"]}
            )
            assert not audit_report(invalid, tools.ledger)["verified"]
            assert {entry["tool"] for entry in tools.ledger.records()} >= {
                "dataset_profiler",
                "local_python_repl",
                "local_sql_engine",
            }
        finally:
            await tools.close()

    asyncio.run(scenario())


def test_durable_queued_language_never_steers_another_language_run(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid)
    original_event = store.emit(
        sid,
        rid,
        "message",
        {"role": "assistant", "text": "Original English history", "language": "en"},
    )
    store.queue_instruction(sid, "VI_PENDING", language="vi")
    store.queue_instruction(sid, "EN_STEERING", language="en")
    store.close()
    store = SessionStore(tmp_path)
    store.recover_interrupted()
    assert store.events(sid)[0] == original_event
    resumed, _ = store.begin_run(sid)
    assert store.take_instructions(sid, resumed, include_unresolved=True) == ["EN_STEERING"]
    store.finish_run(resumed, "completed")
    assert store.next_queued_run(sid) == {
        "analysis_context_id": None,
        "language": "vi",
        "action_intent": "none",
    }
    vietnamese, _ = store.begin_run(sid, language="vi")
    assert store.take_instructions(sid, vietnamese, language="vi") == ["VI_PENDING"]
    store.finish_run(vietnamese, "completed")
    assert [run["language"] for run in store.session(sid)["runs"]] == ["en", "en", "vi"]
    assert store.session(sid)["pending_instructions"] == 0
    store.close()


def test_language_change_waits_for_own_http_run_and_does_not_rewrite_history(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Runtime:
        calls = []

        def __init__(
            self, settings, toolkit, emit, steering=None, initial_items=None, language="en"
        ):
            self.language, self.emit, self.steering = language, emit, steering
            self.initial_items = initial_items or []

        async def run(self, prompt):
            call = {"language": self.language, "prompt": prompt}
            self.calls.append(call)
            if len(self.calls) == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            call["steering"] = await self.steering()
            report = (
                "Original English reply" if self.language == "en" else "Câu trả lời tiếng Việt mới"
            )
            await self.emit(
                "report",
                {
                    "report": report,
                    "claims": [],
                    "qa": {"verified": True},
                    "language": self.language,
                },
            )
            return {
                "status": "completed",
                "history": self.initial_items + [{"role": "assistant", "content": report}],
                "usage": [],
                "cost_usd": 0,
            }

    def wait(predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Language boundary was not reached")

    settings = Settings(
        mode="mock",
        memory_enabled=False,
        enable_file_search=False,
        state_dir=str(tmp_path / "state"),
    )
    with TestClient(create_app(settings, runtime_factory=Runtime)) as client:
        sid = client.post("/api/sessions").json()["id"]
        url = f"/api/sessions/{sid}/prompt"
        assert client.post(url, json={"text": "Explain the selected data."}).status_code == 200
        wait(entered.is_set)
        assert client.post(
            url, json={"text": "Tóm tắt dữ liệu đang chọn.", "language": "vi"}
        ).json()["queued"]
        assert client.post(
            url, json={"text": "Also compare the observed costs.", "language": "en"}
        ).json()["queued"]
        release.set()
        wait(lambda: len(Runtime.calls) == 2)
        wait(lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed")
        assert Runtime.calls == [
            {
                "language": "en",
                "prompt": "Explain the selected data.",
                "steering": ["Also compare the observed costs."],
            },
            {"language": "vi", "prompt": "Tóm tắt dữ liệu đang chọn.", "steering": []},
        ]
        snapshot = client.get(f"/api/sessions/{sid}").json()
        replies = [
            event["data"]
            for event in snapshot["events"]
            if event["type"] == "message" and event["data"]["role"] == "assistant"
        ]
        assert [(reply["language"], reply["text"]) for reply in replies] == [
            ("en", "Original English reply"),
            ("vi", "Câu trả lời tiếng Việt mới"),
        ]
        assert [run["language"] for run in snapshot["runs"]] == ["en", "vi"]


@pytest.mark.parametrize("language", ["fr", None, 1])
def test_invalid_language_is_rejected_before_prompt_or_run_is_persisted(tmp_path, language):
    settings = Settings(mode="mock", state_dir=str(tmp_path / "state"))
    with TestClient(create_app(settings)) as client:
        sid = client.post("/api/sessions").json()["id"]
        assert (
            client.post(
                f"/api/sessions/{sid}/prompt", json={"text": "Unaccepted", "language": language}
            ).status_code
            == 422
        )
        snapshot = client.get(f"/api/sessions/{sid}").json()
        assert snapshot["runs"] == snapshot["events"] == []
        assert snapshot["pending_instructions"] == 0


@pytest.mark.parametrize(
    "source",
    [
        "Sum of observed repair costs in unspecified local currency",
        "Observed repair cost is 123,456.90 in unspecified local currency, with ",
    ],
)
def test_currency_translation_does_not_infer_domestic_currency(source):
    translated = localize_report(source, "vi")
    assert "đơn vị tiền tệ của nguồn" in translated
    assert "chưa xác định loại tiền" in translated
    assert "nội tệ" not in translated
    assert "VND" not in translated
    if "123,456.90" in source:
        assert "123,456.90" in translated
    assert localize_report(source, "en") == source


@pytest.mark.parametrize(
    ("question", "preferred", "expected"),
    [
        ("What is the total observed repair cost?", "vi", "en"),
        ("Which line has the highest Critical share?", "vi", "en"),
        ("Summarize the available evidence.", "vi", "en"),
        ("Explain ‘Chi phí sửa chữa’ in the source.", "vi", "en"),
        ("Why does the report mention Nhân?", "vi", "en"),
        ("Tóm tắt chi phí và tỷ trọng lỗi theo tháng.", "en", "vi"),
        ("tom tat chi phi va ty trong loi theo thang", "en", "vi"),
        ("con Line 3 thi sao?", "en", "vi"),
        ("So sanh Line 3 voi Line 2", "en", "vi"),
        ("Dữ liệu hiện có nói gì? Trả lời bằng tiếng Anh.", "vi", "en"),
        ("Answer in Vietnamese using the current filters.", "en", "vi"),
        ("OK", "vi", "vi"),
        ("continue", "vi", "vi"),
        ("next?", "vi", "vi"),
        ("more", "vi", "vi"),
        ("tiep tuc", "en", "en"),
        ("Line 3", "vi", "vi"),
        ("`print('Tóm tắt dữ liệu')`", "en", "en"),
        ("https://example.com/du-lieu?lang=vi", "en", "en"),
    ],
)
def test_question_language_uses_current_prose_and_prior_language_for_ambiguity(
    question, preferred, expected
):
    assert response_language(question, preferred) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Please answer in English.", "en"),
        ("Can you respond entirely in Vietnamese?", "vi"),
        ("Hãy trả lời bằng tiếng Anh.", "en"),
        ("tra loi tieng viet", "vi"),
        ("English, please.", "en"),
        ("Do not answer in English; answer in Vietnamese.", "vi"),
        ("What does ‘answer in Vietnamese’ mean?", None),
        ("Explain `answer in Vietnamese`.", None),
        ("Don't answer in Vietnamese.", None),
        ("Which line has the highest Critical share?", None),
    ],
)
def test_explicit_response_language_distinguishes_directives_from_ordinary_steering(
    question, expected
):
    assert explicit_response_language(question) == expected


@pytest.mark.parametrize("prefix", ["> ", " > ", "  >> ", "   > > ", "    ", "\t", "  \t"])
@pytest.mark.parametrize(
    ("authored", "quoted", "expected"),
    [
        ("What does this note mean?", "Please answer in Vietnamese.", "en"),
        ("Giải thích thông báo sau:", "Answer in English.", "vi"),
    ],
)
def test_markdown_source_directives_do_not_choose_the_question_language(
    prefix, authored, quoted, expected
):
    question = authored + "\n" + prefix + quoted
    assert text_language(question) == expected
    assert response_language(question, "vi" if expected == "en" else "en") == expected
    assert explicit_response_language(question) is None


@pytest.mark.parametrize("prefix", ["> ", "   >> ", "    ", "\t"])
@pytest.mark.parametrize("preferred", ["en", "vi"])
def test_markdown_source_only_keeps_the_prior_question_language(prefix, preferred):
    question = prefix + "Answer in English. Tra loi tieng Viet."
    assert text_language(question) is None
    assert explicit_response_language(question) is None
    assert response_language(question, preferred) == preferred


@pytest.mark.parametrize("prefix", ["> ", "   > > ", "    ", "\t"])
@pytest.mark.parametrize(
    ("authored", "quoted", "expected"),
    [
        ("Please answer in English.", "Answer in Vietnamese.", "en"),
        ("Hãy trả lời bằng tiếng Việt.", "Answer in English.", "vi"),
    ],
)
def test_authored_directive_outside_markdown_source_remains_effective(
    prefix, authored, quoted, expected
):
    question = "Review this note:\n" + prefix + quoted + "\n\n" + authored
    assert explicit_response_language(question) == expected
    assert response_language(question, "vi" if expected == "en" else "en") == expected


@pytest.mark.parametrize(
    "identifier",
    [
        "READ_ONLY",
        "READ_ONLY_STEERING",
        "repair_cost",
        "event_count",
        "analysis.monthly[0].event_count",
        "$.analysis.monthly[0].event_count",
        "analysis.monthly",
        'analysis["monthly"][0]["event_count"]',
        "artifacts/analysis/monthly.csv",
        r"C:\analysis\monthly\event_count.json",
        "repairCost",
        "EventCount",
        "analysis2024",
        "answer_in_vietnamese",
        "AnswerInEnglish",
        "[claim:analysis.monthly[0].event_count]",
    ],
)
def test_code_identifiers_and_paths_are_not_question_language_evidence(identifier):
    assert text_language(identifier) is None
    assert explicit_response_language(identifier) is None
    assert response_language(identifier, "vi") == "vi"
    assert response_language(identifier, "en") == "en"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is repair_cost?", "en"),
        ("What changed in $.analysis.monthly[0].event_count?", "en"),
        ("Explain artifacts/analysis/monthly.csv.", "en"),
        ("Ý nghĩa của event_count là gì?", "vi"),
        ("Kiểm tra số liệu trong analysis.monthly.", "vi"),
        ("tom tat chi phi trong repairCost", "vi"),
    ],
)
def test_authored_prose_around_identifiers_still_selects_the_question_language(question, expected):
    assert text_language(question) == expected
    assert explicit_response_language(question) is None
    assert response_language(question, "vi" if expected == "en" else "en") == expected


@pytest.mark.parametrize("preferred", ["fr", None, 1])
def test_question_language_validates_the_fallback(preferred):
    with pytest.raises(ValueError, match="language must"):
        response_language("OK", preferred)


def test_report_language_does_not_accept_a_vietnamese_unit_legend_as_vietnamese_prose():
    assert text_language("The selected line has the highest observed repair cost.") == "en"
    assert text_language("Đã ghi nhận các sự kiện lỗi trong phạm vi đang chọn.") == "vi"
    assert text_language("CSV [claim:event_count] `print('du lieu')`") is None


@pytest.mark.parametrize("language", ["en", "vi"])
def test_public_summary_display_uses_full_part_snapshots_and_preserves_raw_audit(language):
    async def scenario():
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        runtime = AnalystRuntime(Settings(mode="mock"), object(), emit, language=language)
        identity = {
            "provider_api": "agents",
            "session_id": "session-public",
            "turn_id": "turn-public",
            "item_id": "reasoning-public",
            "summary_index": 0,
        }
        text = (
            "Reviewing the request and checking evidence."
            if language == "en"
            else "Đang kiểm tra dữ liệu và bằng chứng."
        )
        await runtime.emit("reasoning_part", {**identity, "phase": "added", "text": ""})
        # A chunk may split in the middle of a word. Keep the actual provider
        # text visible while the full part supplies a language signal.
        await runtime.emit("reasoning_summary", {**identity, "delta": text[:2]})
        await runtime.emit(
            "reasoning_summary", {**identity, "output_index": 3, "delta": text[2:11]}
        )
        await runtime.emit("reasoning_summary", {**identity, "delta": text[11:]})
        await runtime.emit("reasoning_summary", {**identity, "text": text, "done": True})
        await runtime.emit("reasoning_part", {**identity, "phase": "done", "text": text})
        assert "".join(data.get("delta", "") for _, data in events) == text
        assert all(data["language"] == language for _, data in events)
        assert all(data["display_mode"] == "snapshot" for _, data in events)
        assert all(data["item_id"] == identity["item_id"] for _, data in events)
        assert events[1][1]["display_source"] == "provider_summary"
        assert events[1][1]["display_text"] == text[:2]
        assert events[1][1]["display_localized"] is False
        for _, data in events[-3:]:
            assert data["display_text"] == text
            assert data["display_source"] == "provider_summary"
            assert data["display_localized"] is False

        other = (
            "Đang kiểm tra dữ liệu và bằng chứng."
            if language == "en"
            else "Reviewing the request and checking evidence."
        )
        await runtime.emit(
            "reasoning_summary",
            {**identity, "summary_index": 1, "text": other, "done": True},
        )
        data = events[-1][1]
        assert data["text"] == other
        assert data["display_text"] == other
        assert data["display_language"] == ("vi" if language == "en" else "en")
        assert data["display_source"] == "provider_summary"
        assert data["display_localized"] is False
        assert data["translation_state"] == "pending"

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["en", "vi"])
def test_unavailable_run_keeps_the_question_language_without_a_provider_call(language):
    async def scenario():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="auto", api_key=""),
            object(),
            lambda kind, data: events.append((kind, data)),
            language=language,
        )
        result = await runtime.run("Explain the current data.")
        assert result["status"] == "unavailable"
        assert result["language"] == language
        assert result["report_number_format"] == ("vi-VN" if language == "vi" else "en-US")
        assert len(events) == 1
        assert events[0][0] == "status"
        assert events[0][1]["language"] == language
        assert text_language(events[0][1]["label"]) == language
        assert runtime.models == []

    asyncio.run(scenario())
