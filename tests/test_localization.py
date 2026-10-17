"""Response language is display metadata, never a change to canonical evidence."""

import asyncio
import json
import threading
import time
from copy import deepcopy

import pytest
from analyst.app import create_app
from analyst.config import Settings
from analyst.localization import VI_NUMBER_NOTE, localize_report, vietnamese_metadata
from analyst.runtime import AnalystReport, AnalystRuntime, audit_report
from analyst.store import SessionStore
from analyst.tools import AnalystTools
from fastapi.testclient import TestClient
from test_runtime import FrozenAnalysisService, canonical_context


def test_translation_preserves_numbers_claim_identifiers_code_and_links():
    source = "Observed repair cost is 123,456.90 [claim:analysis.monthly[0].known_repair_cost]. `Critical 3.14` [evidence](report2024.md)"
    translated = localize_report(source, "vi")
    assert translated.startswith(VI_NUMBER_NOTE)
    assert "123,456.90 [claim:analysis.monthly[0].known_repair_cost]" in translated
    assert "`Critical 3.14`" in translated and "(report2024.md)" in translated
    assert localize_report(source) == source
    assert vietnamese_metadata("2024-06").startswith("tháng Sáu năm hai nghìn")
    assert vietnamese_metadata("Line-3") == "Dây chuyền ba"
    assert vietnamese_metadata("Category 2") == "Nhóm hai"
    assert vietnamese_metadata("Moderate") == "Trung bình"
    assert vietnamese_metadata("Visual Inspection") == "Kiểm tra trực quan"


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
                    assert result["report"].count(VI_NUMBER_NOTE) == 1
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
        assert client.post(url, json={"text": "EN_INITIAL"}).status_code == 200
        wait(entered.is_set)
        assert client.post(url, json={"text": "VI_NEXT", "language": "vi"}).json()["queued"]
        assert client.post(url, json={"text": "EN_UPDATE", "language": "en"}).json()["queued"]
        release.set()
        wait(lambda: len(Runtime.calls) == 2)
        wait(lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed")
        assert Runtime.calls == [
            {"language": "en", "prompt": "EN_INITIAL", "steering": ["EN_UPDATE"]},
            {"language": "vi", "prompt": "VI_NEXT", "steering": []},
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
