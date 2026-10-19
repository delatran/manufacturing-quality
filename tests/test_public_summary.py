"""Public-summary localization through the real SDK and fake managed sessions."""

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from agents import ModelResponse, Usage
from analyst import runtime as runtime_module
from analyst.config import Settings
from analyst.managed_agents import ManagedAgentsModel
from analyst.managed_recovery import recover_managed_sessions
from analyst.model import AgentModel, ResponseProtocolError
from analyst.public_summary import PublicSummaryDisplay, validate_translation
from analyst.runtime import AnalystRuntime
from analyst.store import SessionStore
from openai import BadRequestError
from test_managed_agents import Sessions, Stream, event, message, session
from test_managed_recovery import Provider

IDENTITY = {
    "provider_api": "agents",
    "session_id": "summary-source",
    "turn_id": "source-turn",
    "item_id": "summary-original",
    "summary_index": 0,
    "event_id": "native-summary-event",
}
ENGLISH = "Reviewing the request and checking 12 records in `event_count`."
VIETNAMESE = "Đang kiểm tra yêu cầu và 12 dòng dữ liệu trong `event_count`."


def translator_sessions(translated, *, response=None):
    translations = translated if isinstance(translated, list) else [translated]
    final = session()
    final.spend_control = None
    return Sessions(
        [final],
        [
            [
                message(
                    response
                    if response is not None
                    else json.dumps(
                        {
                            "translations": [
                                {"index": index, "text": text}
                                for index, text in enumerate(translations)
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
            ]
        ],
        [
            Stream(
                [
                    event("agent.session.created", session=final),
                    event(
                        "agent.session.turn.reasoning_summary_text.done",
                        session_id=final.id,
                        turn_id="turn_real",
                        item_id="translator-reasoning",
                        summary_index=0,
                        text="Translator public text must stay separate.",
                    ),
                    event("agent.session.turn.completed", session_id=final.id, turn_id="turn_real"),
                    event("agent.session.idle", session=final),
                ]
            )
        ],
    )


def bind_translator(monkeypatch, sessions):
    sessions.client_closed = False

    async def close():
        sessions.client_closed = True

    def model(settings, emit, **kwargs):
        client = SimpleNamespace(
            beta=SimpleNamespace(agents=SimpleNamespace(sessions=sessions)), close=close
        )
        return ManagedAgentsModel(settings, emit, client=client, **kwargs)

    monkeypatch.setattr(runtime_module, "ManagedAgentsModel", model)


@pytest.mark.parametrize(
    ("language", "source", "translated"),
    [("vi", ENGLISH, VIETNAMESE), ("en", VIETNAMESE, ENGLISH)],
)
def test_completed_summary_translates_once_with_real_managed_accounting(
    monkeypatch, language, source, translated
):
    sessions = translator_sessions(translated)
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language=language,
        )
        for delta in (source[:2], source[2:10], source[10:]):
            await runtime.emit("reasoning_summary", {**IDENTITY, "delta": delta})
        assert not sessions.creates
        assert events[-1][1]["display_text"] == source
        assert events[-1][1]["display_localized"] is False
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": source, "done": True})
        await runtime.emit("reasoning_part", {**IDENTITY, "text": source, "phase": "done"})
        # Concurrent boundary hooks still share a single translation request.
        await asyncio.gather(runtime.drain_public_summaries(), runtime.drain_public_summaries())
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": source, "done": True})
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        create = sessions.creates[0]
        assert create["environment"] == {"type": "none"}
        assert "spend_control" not in create
        assert create["agent"]["model"] == "gpt-6-luna"
        assert create["agent"]["reasoning"] == {"effort": "max"}
        assert create["agent"]["tools"] == []
        assert create["agent"]["multi_agent"] == {"enabled": False}
        output_format = create["agent"]["text"]["format"]
        assert output_format["type"] == "json_schema"
        assert output_format["schema"]["required"] == ["translations"]
        assert output_format["schema"]["properties"]["translations"]["type"] == "array"
        item_schema = output_format["schema"]["$defs"]["PublicSummaryTranslation"]
        assert item_schema["required"] == ["index", "text"]
        assert item_schema["properties"]["index"]["type"] == "integer"
        assert item_schema["properties"]["text"]["type"] == "string"
        assert item_schema["additionalProperties"] is False
        assert output_format["schema"]["additionalProperties"] is False
        instructions = create["agent"]["instructions"]
        assert "PUBLIC provider summaries" in instructions
        assert "Do not reconstruct private reasoning" in instructions
        assert "exactly\none item for every supplied index" in instructions
        assert "Keep written-out numbers as target-language words" in instructions
        prompt = json.loads(create["input"][0]["content"][0]["text"])
        assert prompt == {
            "target_language": language,
            "public_summaries": [{"index": 0, "text": source}],
        }
        summaries = [
            data for kind, data in events if kind in {"reasoning_summary", "reasoning_part"}
        ]
        final = summaries[-1]
        assert final["text"] == source
        assert final["display_text"] == translated
        assert final["display_localized"] is True
        assert final["display_source"] == "translated_provider_summary"
        assert final["display_language"] == language
        assert final["translation_state"] == "translated"
        assert all(row["item_id"] == IDENTITY["item_id"] for row in summaries)
        assert "Translator public text" not in str(summaries)
        supplemental = [data for data in summaries if "source_provider_event_id" in data]
        assert len(supplemental) == 1
        assert supplemental[0]["source_provider_event_id"] == IDENTITY["event_id"]
        assert "event_id" not in supplemental[0]
        assert "text_delta" not in [kind for kind, _ in events]
        assert len(runtime.models) == 1
        model = runtime.models[0]
        assert model.role == "Summary Translator"
        assert model.settings.reasoning_summary == "off"
        assert model.budget is runtime.budget
        assert model.session_id == "sess_real"
        assert len(model.usage) == 1
        assert runtime.budget["cost_usd"] > 0
        assert runtime.budget["unresolved_generation_requests"] == 0
        assert sessions.client_closed is True
        assert any(kind == "usage" and row["role"] == "Summary Translator" for kind, row in events)

    asyncio.run(run())


def test_multiple_completed_parts_share_one_batch_and_keep_their_source_binding(monkeypatch):
    sources = [ENGLISH.replace("12", str(number)) for number in (12, 13, 14)]
    translations = [VIETNAMESE.replace("12", str(number)) for number in (12, 13, 14)]
    # Output order is independent of source identity; explicit indices bind it.
    response = json.dumps(
        {"translations": [{"index": index, "text": translations[index]} for index in (2, 0, 1)]},
        ensure_ascii=False,
    )
    sessions = translator_sessions(translations, response=response)
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        for source_index, source in zip((7, 9, 40), sources, strict=True):
            await runtime.emit(
                "reasoning_summary",
                {**IDENTITY, "summary_index": source_index, "text": source, "done": True},
            )
        await asyncio.gather(runtime.drain_public_summaries(), runtime.drain_public_summaries())
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        prompt = json.loads(sessions.creates[0]["input"][0]["content"][0]["text"])
        assert prompt["public_summaries"] == [
            {"index": index, "text": source} for index, source in enumerate(sources)
        ]
        final = {
            data["summary_index"]: data for kind, data in events if kind == "reasoning_summary"
        }
        assert list(final) == [7, 9, 40]
        for index, data in enumerate(final.values()):
            assert data["text"] == sources[index]
            assert data["display_text"] == translations[index]
            assert data["translation_state"] == "translated"
        assert len(runtime.models) == 1
        assert len(runtime.models[0].usage) == 1
        assert runtime.budget["unresolved_generation_requests"] == 0
        assert sessions.client_closed is True

    asyncio.run(run())


@pytest.mark.parametrize("indices", [(0, 0), (0,), (0, 1, 2), (0, 2), (), (False, 1)])
def test_batch_rejects_duplicate_missing_extra_empty_or_boolean_indices(monkeypatch, indices):
    response = json.dumps(
        {"translations": [{"index": index, "text": VIETNAMESE} for index in indices]},
        ensure_ascii=False,
    )
    sessions = translator_sessions(VIETNAMESE, response=response)
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        for index in range(2):
            await runtime.emit(
                "reasoning_summary",
                {**IDENTITY, "summary_index": index, "text": ENGLISH, "done": True},
            )
        await runtime.drain_public_summaries()
        final = {
            data["summary_index"]: data for kind, data in events if kind == "reasoning_summary"
        }
        assert len(final) == 2
        assert all(data["translation_state"] == "failed" for data in final.values())
        assert all(data["display_text"] == data["text"] == ENGLISH for data in final.values())
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        assert len(runtime.models[0].usage) == 1
        assert runtime.budget["unresolved_generation_requests"] == 0
        assert sessions.client_closed is True

    asyncio.run(run())


def test_one_invalid_batch_translation_keeps_valid_sibling_translations(monkeypatch):
    sources = [ENGLISH.replace("12", str(number)) for number in (12, 13, 14)]
    translated = [VIETNAMESE.replace("12", str(number)) for number in (12, 999, 14)]
    sessions = translator_sessions(translated)
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        for index, source in enumerate(sources):
            await runtime.emit(
                "reasoning_summary",
                {**IDENTITY, "summary_index": index, "text": source, "done": True},
            )
        await runtime.drain_public_summaries()
        final = {
            data["summary_index"]: data for kind, data in events if kind == "reasoning_summary"
        }
        assert [data["translation_state"] for data in final.values()] == [
            "translated",
            "failed",
            "translated",
        ]
        assert final[0]["display_text"] == translated[0]
        assert final[1]["display_text"] == sources[1]
        assert final[1]["display_localized"] is False
        assert final[2]["display_text"] == translated[2]
        assert all(data["text"] == sources[index] for index, data in final.items())
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        assert len(runtime.models[0].usage) == 1

    asyncio.run(run())


def test_structured_summary_translation_validates_only_the_authored_text_from_native_regression(
    monkeypatch,
):
    # Frozen public source and translation text from the observed plaintext
    # envelope failure. Keep the token validator strict; parse a defined schema.
    source = (
        "**Analyzing SQL Count Queries**\n\n"
        "I'm working on an SQL query to count null and zero values in `repair_cost` "
        "while grouping by `production_line`. I need to consider how to handle "
        "unknown values too. I'll compare nulls to zeros and see how `cost_status` "
        "plays into identifying valid `repair_cost` entries. I want to ensure I'm "
        "correctly counting known values and assessing the validity of `repair_cost`. "
        "The user's question about the difference between absent and zero costs is "
        "clear, so I need to clarify that in my results."
    )
    translated = (
        "**Phân tích các truy vấn đếm trong SQL**\n\n"
        "Tôi đang viết một truy vấn SQL để đếm các giá trị null và zero trong "
        "`repair_cost` khi nhóm theo `production_line`. Tôi cần cân nhắc cách xử lý "
        "cả những giá trị chưa biết. Tôi sẽ so sánh các giá trị null với zero và "
        "xem `cost_status` ảnh hưởng thế nào đến việc xác định các mục `repair_cost` "
        "hợp lệ. Tôi muốn đảm bảo mình đếm chính xác các giá trị đã biết và đánh "
        "giá tính hợp lệ của `repair_cost`. Câu hỏi của người dùng về sự khác nhau "
        "giữa chi phí không có và chi phí bằng zero đã rõ, vì vậy tôi cần làm rõ "
        "điều đó trong kết quả của mình."
    )
    invalid_plaintext_wrapper = json.dumps({"public_summary": translated}, ensure_ascii=False)
    with pytest.raises(ValueError, match="protected source tokens"):
        validate_translation(source, invalid_plaintext_wrapper, "vi")
    sessions = translator_sessions(translated)
    bind_translator(monkeypatch, sessions)

    async def run():
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda *args: None,
            language="vi",
        )
        assert await runtime._translate_public_summary(source) == translated
        assert len(sessions.creates) == 1
        assert sessions.client_closed is True
        assert runtime.budget["unresolved_generation_requests"] == 0

    asyncio.run(run())


def test_translation_schema_rejects_an_unexpected_json_wrapper_without_unwrapping(monkeypatch):
    sessions = translator_sessions(
        VIETNAMESE, response=json.dumps({"public_summary": VIETNAMESE}, ensure_ascii=False)
    )
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        assert events[-1][1]["translation_state"] == "failed"
        assert events[-1][1]["display_text"] == ENGLISH
        assert events[-1][1]["display_localized"] is False
        assert len(runtime.models[0].usage) == 1
        assert runtime.budget["unresolved_generation_requests"] == 0
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        assert sessions.client_closed is True

    asyncio.run(run())


def test_summary_alias_replay_reuses_a_prior_translation_without_another_request(monkeypatch):
    async def run():
        events, calls = [], []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )

        async def translate(texts):
            calls.extend(texts)
            return [VIETNAMESE]

        monkeypatch.setattr(runtime, "_translate_public_summaries", translate)
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        await runtime.emit(
            "reasoning_summary",
            {**IDENTITY, "item_id": "transient-replay", "text": ENGLISH, "done": True},
        )
        await runtime.emit(
            "reasoning_alias",
            {
                **IDENTITY,
                "item_id": "transient-replay",
                "canonical_item_id": IDENTITY["item_id"],
                "reconciled": True,
            },
        )
        await runtime.drain_public_summaries()
        assert calls == [ENGLISH]
        assert runtime._public_summaries.pending() == []
        assert events[-1][1]["item_id"] == IDENTITY["item_id"]
        assert events[-1][1]["display_text"] == VIETNAMESE
        assert events[-1][1]["translation_state"] == "translated"

    asyncio.run(run())


def test_native_language_empty_and_unknown_summary_parts_never_trigger_translation():
    display = PublicSummaryDisplay("en")
    for index, text in enumerate(("", ENGLISH, "CSV `event_count`")):
        result = display.project(
            "reasoning_summary", {**IDENTITY, "summary_index": index, "text": text, "done": True}
        )
        assert result["display_text"] == text
        assert result["display_localized"] is False
        assert result["display_source"] == "provider_summary"
        assert "translation_state" not in result
    assert display.pending() == []


@pytest.mark.parametrize(
    ("language", "source", "translated"),
    [("vi", "Validate totals.", "Xác thực tổng."), ("en", "Xác thực tổng.", "Validate totals.")],
)
def test_short_summary_prose_translates_without_requiring_request_classifier_vocabulary(
    monkeypatch, language, source, translated
):
    async def run():
        events, calls = [], []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language=language,
        )

        async def translate(texts):
            calls.extend(texts)
            return [validate_translation(texts[0], translated, language)]

        monkeypatch.setattr(runtime, "_translate_public_summaries", translate)
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": source, "done": True})
        assert events[-1][1]["display_text"] == source
        assert events[-1][1]["translation_state"] == "pending"
        await runtime.drain_public_summaries()
        assert calls == [source]
        assert events[-1][1]["display_text"] == translated
        assert events[-1][1]["display_language"] == language
        assert events[-1][1]["display_localized"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", [RuntimeError("translation unavailable"), ValueError("wrong language")]
)
def test_translation_failure_keeps_original_and_does_not_retry(monkeypatch, failure):
    async def run():
        events, calls = [], []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )

        async def fail(texts):
            calls.extend(texts)
            raise failure

        monkeypatch.setattr(runtime, "_translate_public_summaries", fail)
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        await runtime.drain_public_summaries()
        assert calls == [ENGLISH]
        final = events[-1][1]
        assert final["text"] == final["display_text"] == ENGLISH
        assert final["display_language"] == "en"
        assert final["display_localized"] is False
        assert final["translation_state"] == "failed"

    asyncio.run(run())


def test_failed_provider_translation_retains_unresolved_accounting_and_blocks_more_spend(
    monkeypatch,
):
    class FailedSessions(Sessions):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            raise ResponseProtocolError("Provider admission is uncertain")

    sessions = FailedSessions([], [], [])
    bind_translator(monkeypatch, sessions)

    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        runtime.last_qa = {"verified": True, "errors": []}
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        assert events[-1][1]["translation_state"] == "failed"
        assert events[-1][1]["display_text"] == ENGLISH
        assert sessions.client_closed is True
        assert runtime.budget["blocked"] is True
        assert runtime.budget["unresolved_generation_requests"] == 1
        assert runtime.budget["cost_usd"] == 0
        assert runtime.models[0].usage == []
        assert runtime.last_qa == {"verified": True, "errors": []}
        await runtime.emit(
            "reasoning_summary", {**IDENTITY, "summary_index": 1, "text": ENGLISH, "done": True}
        )
        await runtime.drain_public_summaries()
        assert events[-1][1]["translation_state"] == "skipped"
        assert len(sessions.creates) == 1
        assert len(runtime.models) == 1

    asyncio.run(run())


def test_rejected_translation_remains_resolved_after_durable_restart(monkeypatch, tmp_path):
    class RejectedSessions(Sessions):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            raise BadRequestError(
                "The provider rejected the translation schema",
                response=httpx2.Response(
                    400,
                    request=httpx2.Request("POST", "https://api.openai.com/v1/agents/sessions"),
                ),
                body={"code": "invalid_json_schema", "param": "agent.text.format"},
            )

    sessions = RejectedSessions([], [], [])
    bind_translator(monkeypatch, sessions)
    store = SessionStore(tmp_path)
    with store.db:
        store.db.execute("INSERT INTO sessions(id,created) VALUES ('visitor','fixture')")
    run_id, _ = store.begin_run("visitor")

    async def run():
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: store.emit("visitor", run_id, kind, data),
            language="vi",
        )
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        assert len(sessions.creates) == 1
        assert runtime.budget["unresolved_generation_requests"] == 0
        assert runtime.budget["cost_usd"] == 0
        assert sessions.client_closed
        return runtime

    runtime = asyncio.run(run())
    store.close()
    reopened = SessionStore(tmp_path)
    try:
        reopened.recover_interrupted()
        provider = Provider()
        asyncio.run(recover_managed_sessions(reopened, runtime.settings, client=provider))
        assert provider.calls == []
        assert not reopened.stopped("visitor")
        rejected = [
            entry["data"]
            for entry in reopened.events("visitor")
            if entry["type"] == "managed_session_rejected"
        ]
        assert len(rejected) == 1
        assert rejected[0]["role"] == "Summary Translator"
        assert rejected[0]["generation_dispatched"] is False
    finally:
        reopened.close()


@pytest.mark.parametrize("stop", ["blocked", "provider_stopped"])
def test_stopped_shared_budget_does_not_start_translation(monkeypatch, stop):
    async def run():
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        runtime.budget[stop] = True
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.drain_public_summaries()
        assert runtime.models == []
        assert events[-1][1]["translation_state"] == "skipped"
        assert events[-1][1]["display_text"] == ENGLISH

    asyncio.run(run())


def test_model_boundary_releases_shared_lock_before_summary_translation(monkeypatch):
    async def run():
        budget = {"model_lock": asyncio.Lock()}
        response = ModelResponse(output=[], usage=Usage(), response_id=None)
        called = []

        async def base_response(self, *args, **kwargs):
            async with self.budget["model_lock"]:
                return response

        async def boundary():
            assert not budget["model_lock"].locked()
            async with budget["model_lock"]:
                called.append(True)

        monkeypatch.setattr(AgentModel, "get_response", base_response)
        model = ManagedAgentsModel(
            Settings(api_key="offline-test"),
            lambda *args: None,
            budget=budget,
        )
        model.response_boundary = boundary
        assert await asyncio.wait_for(model.get_response(), timeout=1) is response
        assert called == [True]
        await model.client.close()

    asyncio.run(run())


def test_cancellation_cancels_and_joins_translator_session(monkeypatch):
    async def run():
        opened = asyncio.Event()
        live, final = session(status="in_progress"), session()
        live.spend_control = final.spend_control = None

        class WaitingStream(Stream):
            async def __aiter__(self):
                yield event("agent.session.created", session=live)
                opened.set()
                await asyncio.Event().wait()

        stream = WaitingStream([])
        sessions = Sessions([final], [[]], [stream])
        bind_translator(monkeypatch, sessions)
        events = []
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test"),
            object(),
            lambda kind, data: events.append((kind, data)),
            language="vi",
        )
        await runtime.emit("reasoning_summary", {**IDENTITY, "text": ENGLISH, "done": True})
        await runtime.emit(
            "reasoning_summary", {**IDENTITY, "summary_index": 1, "text": ENGLISH, "done": True}
        )
        task = asyncio.create_task(runtime.drain_public_summaries())
        await asyncio.wait_for(opened.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert sessions.client_closed is True
        assert sessions.inputs[0]["events"] == [{"type": "agent.session.input.cancel"}]
        assert runtime.models[0]._cancel_task.done()
        assert runtime.budget["blocked"] is True
        assert runtime.budget["unresolved_generation_requests"] == 1
        assert events[-1][1]["translation_state"] == "cancelled"
        assert events[-1][1]["display_text"] == ENGLISH
        final_parts = {
            data["summary_index"]: data for kind, data in events if kind == "reasoning_summary"
        }
        assert set(final_parts) == {0, 1}
        assert all(data["translation_state"] == "cancelled" for data in final_parts.values())
        assert len(sessions.creates) == 1
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


@pytest.mark.parametrize(
    "translated",
    [
        "Đang kiểm tra 13 dòng dữ liệu trong `event_count`.",
        "Đang kiểm tra 12 dòng dữ liệu trong `record_count`.",
        ENGLISH,
        "",
    ],
)
def test_translation_validation_rejects_changed_tokens_or_wrong_language(translated):
    with pytest.raises(ValueError):
        validate_translation(ENGLISH, translated, "vi")


def test_translation_keeps_written_number_words_out_of_numeric_tokens():
    source = "Checking null and zero values in `repair_cost`, then comparing nulls to zeros."
    translated = "Đang kiểm tra các giá trị null và không trong `repair_cost`, rồi so sánh các giá trị null với không."
    assert validate_translation(source, translated, "vi") == translated
    with pytest.raises(ValueError, match="protected source tokens"):
        validate_translation(source, translated.replace("không", "0"), "vi")
