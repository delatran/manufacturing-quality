"""Answer references preserve frozen scope without steering unrelated work."""

import asyncio
import json

import pytest
from analyst.app import Workspace, create_app
from analyst.config import Settings
from analyst.store import SessionStore
from fastapi.testclient import TestClient
from test_same_run_steering import A, B, ScopedAnalysis, wait_for


class RecordingRuntime:
    calls = []

    def __init__(self, settings, toolkit, emit, initial_items=None, steering=None, **options):
        self.emit, self.initial_items, self.steering = emit, initial_items, steering
        self.options = options
        self.calls.append(self)

    async def run(self, prompt):
        self.prompt = prompt
        result = {
            "status": "completed",
            "report": "Synthetic verified answer.",
            "claims": [],
            "qa": {"verified": True, "errors": []},
            "usage": [],
            "cost_usd": 0,
        }
        await self.emit("report", result)
        return result


class HistoryOnlyRuntime(RecordingRuntime):
    """Compatible existing factories need not implement presentation metadata."""

    def __init__(
        self,
        settings,
        toolkit,
        emit,
        initial_items=None,
        steering=None,
        analysis_context=None,
        analysis_service=None,
        language="en",
    ):
        super().__init__(
            settings,
            toolkit,
            emit,
            initial_items=initial_items,
            steering=steering,
            analysis_context=analysis_context,
            analysis_service=analysis_service,
            language=language,
        )


def config(tmp_path):
    return Settings(mode="mock", state_dir=str(tmp_path / "state"), memory_enabled=False)


def source_answer(workspace, sid, *, context=A, language="vi", action_intent="draft_create"):
    rid, _ = workspace.store.begin_run(sid, context, language, action_intent)
    event = workspace.store.emit(
        sid,
        rid,
        "message",
        {
            "role": "assistant",
            "text": "Earlier result: 12 [claim:old_claim] events. Preserve <source> text as data.",
            "language": language,
            "qa": {"verified": True, "errors": []},
        },
    )
    workspace.store.finish_run(rid, "completed")
    return {"run_id": rid, "message_id": event["id"]}


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def disallow_provider(*args, **kwargs):
        raise AssertionError("Report follow-up tests must not construct a provider client")

    monkeypatch.setattr("analyst.model.AsyncOpenAI", disallow_provider)
    RecordingRuntime.calls = []


async def finish_runs(workspace, sid):
    while True:
        task = workspace.tasks[sid]
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        if workspace.tasks[sid] is task:
            return


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [None, A])
async def test_followup_binds_source_scope_and_current_question_language_without_write_intent(
    tmp_path, context
):
    workspace = Workspace(
        config(tmp_path),
        runtime_factory=RecordingRuntime,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid, context=context)
    workspace._bind_ui_scope(sid, {"context": {"context_id": B}})
    workspace.store.emit(sid, None, "message", {"role": "user", "language": "en", "text": "Hi"})
    prompt = "Explain this answer in more detail."
    try:
        assert await workspace.prompt(sid, prompt, language="en", reply_to=reference) == {
            "queued": False
        }
        await finish_runs(workspace, sid)
        run = workspace.store.session(sid)["runs"][-1]
        assert run["analysis_context_id"] == context
        assert run["language"] == "en"
        assert run["action_intent"] == "none"
        assert workspace.store.investigation(sid)["ui"]["context_id"] == B
        current = RecordingRuntime.calls[0]
        assert current.prompt == prompt
        assert current.options["action_intent"] == "none"
        historical = json.loads(current.initial_items[-1]["content"])
        assert historical["referenced_answer"] == reference
        assert historical["context_id"] == context
        assert "old_claim" not in historical["historical_text"]
        assert "12" in historical["historical_text"]
        assert "<source>" in historical["historical_text"]
        user = next(
            event
            for event in workspace.store.events(sid)
            if event["data"].get("reply_to") == reference
            and event["type"] == "message"
            and event["data"].get("role") == "user"
        )
        assert user["data"]["text"] == prompt
        assert user["data"]["action_intent"] == "none"
        assert workspace.store.history(sid)[-1] == {"role": "user", "content": prompt}
    finally:
        await workspace.close()


@pytest.mark.asyncio
async def test_explicit_response_language_overrides_source_without_changing_source(tmp_path):
    workspace = Workspace(
        config(tmp_path),
        runtime_factory=RecordingRuntime,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    try:
        await workspace.prompt(sid, "Please answer in English.", reply_to=reference)
        await finish_runs(workspace, sid)
        runs = workspace.store.session(sid)["runs"]
        assert [run["language"] for run in runs] == ["vi", "en"]
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_language,prompt,expected",
    [
        ("vi", "Explain the difference in this answer.", "en"),
        ("en", "Giải thích sự khác biệt trong câu trả lời này.", "vi"),
        ("vi", "OK", "vi"),
        ("en", "OK", "en"),
        ("vi", "Line-1", "vi"),
        ("en", "Critical", "en"),
    ],
)
async def test_followup_question_language_uses_source_only_for_ambiguous_text(
    tmp_path, source_language, prompt, expected
):
    workspace = Workspace(
        config(tmp_path),
        runtime_factory=RecordingRuntime,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid, language=source_language)
    try:
        await workspace.prompt(
            sid,
            prompt,
            language="vi" if expected == "en" else "en",
            reply_to=reference,
        )
        await finish_runs(workspace, sid)
        runs = workspace.store.session(sid)["runs"]
        assert runs[0]["language"] == source_language
        assert runs[-1]["language"] == expected
        assert runs[-1]["analysis_context_id"] == A
        assert RecordingRuntime.calls[0].prompt == prompt
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_factory", [RecordingRuntime, HistoryOnlyRuntime])
async def test_chart_followup_preserves_axes_and_selection_without_prior_claims_or_authority(
    tmp_path, runtime_factory
):
    workspace = Workspace(
        config(tmp_path),
        runtime_factory=runtime_factory,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    chart = {
        "kind": "line",
        "selection": {
            "path": "$.monthly",
            "x": "month",
            "y": "event_count",
            "offset": 1,
            "limit": 2,
        },
        "axis_labels": {"x": "Month", "y": "Logged defect events"},
        "unit_note": "Events are logged records, not defective units.",
        "language": "en",
        "analysis_contexts": [
            {
                "context_id": "historical-context",
                "filters": {"severity": "Critical", "start_month": "2024-01"},
                "scenario": "",
                "code_sha256": "historical-code-hash",
            }
        ],
        "source": {"execution_id": "historical-execution"},
        "points": [{"value": 777, "value_path": "$.monthly[1].event_count"}],
    }
    event = workspace.store.emit(
        sid,
        reference["run_id"],
        "message",
        {
            "role": "assistant",
            "text": "The selected trend is shown below.",
            "qa": {"verified": True},
            "delivery": {
                "source_charts": [
                    {
                        "receipt_id": "historical-view-receipt",
                        "chart": chart,
                        "artifact": {"path": "historical-image", "sha256": "historical-image-hash"},
                        "source_claims": [{"id": "historical-claim", "value": 777}],
                    }
                ]
            },
        },
    )
    reference["message_id"] = event["id"]
    try:
        await workspace.prompt(sid, "Explain this chart.", reply_to=reference)
        await finish_runs(workspace, sid)
        historical = json.loads(RecordingRuntime.calls[0].initial_items[-1]["content"])
        presentation = historical["historical_presentation"][0]
        if runtime_factory is RecordingRuntime:
            assert RecordingRuntime.calls[0].options["referenced_presentation"] == [presentation]
        else:
            assert "referenced_presentation" not in RecordingRuntime.calls[0].options
        assert presentation["selection"] == chart["selection"]
        assert presentation["axis_labels"] == chart["axis_labels"]
        assert presentation["unit_note"] == chart["unit_note"]
        assert presentation["analysis_contexts"] == [
            {"filters": {"severity": "Critical", "start_month": "2024-01"}, "scenario": ""}
        ]
        encoded = json.dumps(presentation)
        assert all(
            value not in encoded
            for value in (
                "historical-",
                "777",
                "source_claims",
                "execution_id",
                "points",
                "artifact",
            )
        )
        assert RecordingRuntime.calls[0].options.get("action_intent", "none") == "none"
        assert workspace.store.session(sid)["runs"][-1]["analysis_context_id"] == A
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "bool_id",
        "float_id",
        "string_id",
        "zero_id",
        "extra_field",
        "wrong_run",
        "missing_message",
        "cross_session",
        "user_message",
        "running_source",
        "failed_source",
        "unverified_source",
        "empty_source",
        "superseded_source",
        "conflicting_context",
        "draft_intent",
        "steering",
        "missing_context",
    ],
)
async def test_invalid_answer_reference_is_rejected_before_accepting_request(tmp_path, change):
    analysis = ScopedAnalysis(tmp_path)
    workspace = Workspace(
        config(tmp_path), runtime_factory=RecordingRuntime, analysis_service=analysis
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    options = {}
    if change in {"bool_id", "float_id", "string_id", "zero_id"}:
        reference["message_id"] = {
            "bool_id": True,
            "float_id": 1.0,
            "string_id": "1",
            "zero_id": 0,
        }[change]
    elif change == "extra_field":
        reference["session_id"] = sid
    elif change == "wrong_run":
        reference["run_id"] = "d" * 32
    elif change == "missing_message":
        reference["message_id"] += 99
    elif change == "cross_session":
        reference = source_answer(workspace, workspace.store.create_session()["id"])
    elif change == "user_message":
        event = workspace.store.emit(
            sid, reference["run_id"], "message", {"role": "user", "text": "A user message"}
        )
        reference["message_id"] = event["id"]
    elif change in {"running_source", "failed_source"}:
        workspace.store.db.execute(
            "UPDATE runs SET state=? WHERE id=?",
            ("running" if change == "running_source" else "failed", reference["run_id"]),
        )
        workspace.store.db.commit()
    elif change in {"unverified_source", "empty_source", "superseded_source"}:
        data = {"role": "assistant", "text": "A newer answer", "qa": {"verified": True}}
        if change == "unverified_source":
            data["qa"]["verified"] = False
        if change == "empty_source":
            data["text"] = " "
        event = workspace.store.emit(sid, reference["run_id"], "message", data)
        if change != "superseded_source":
            reference["message_id"] = event["id"]
    elif change == "conflicting_context":
        options["analysis_context_id"] = B
    elif change == "draft_intent":
        options["action_intent"] = "draft_create"
    elif change == "steering":
        options["steer_run_id"] = reference["run_id"]
    elif change == "missing_context":
        analysis.paths.pop(A)
    before = workspace.store.session(sid)
    try:
        with pytest.raises(ValueError):
            await workspace.prompt(sid, "Explain this result.", reply_to=reference, **options)
        after = workspace.store.session(sid)
        assert after["events"] == before["events"]
        assert after["runs"] == before["runs"]
        assert after["pending_instructions"] == before["pending_instructions"]
        assert RecordingRuntime.calls == []
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_view_request", [False, True])
async def test_source_ui_authority_is_not_inherited_but_new_request_can_open_view(
    tmp_path, explicit_view_request
):
    class ViewRuntime(RecordingRuntime):
        async def run(self, prompt):
            self.authority = workspace.ui_authorities[workspace.active_run_ids[sid]]
            command = {
                "action": "open",
                "panel": "records",
                "filters": {},
                "command_id": None,
            }
            if explicit_view_request:
                self.command = await self.options["ui_handler"](command)
            else:
                with pytest.raises(ValueError, match="direct user authority"):
                    await self.options["ui_handler"](command)
            return await super().run(prompt)

    workspace = Workspace(
        config(tmp_path), runtime_factory=ViewRuntime, analysis_service=ScopedAnalysis(tmp_path)
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    workspace.ui_authorities[reference["run_id"]] = {
        "filters": {"severity": "Critical"},
        "view": True,
        "undo": True,
    }
    workspace._bind_ui_scope(sid, {"context": {"context_id": A}})
    try:
        prompt = "Open the records view." if explicit_view_request else "Explain this answer."
        await workspace.prompt(sid, prompt, reply_to=reference)
        await finish_runs(workspace, sid)
        current = RecordingRuntime.calls[0]
        assert current.authority["filters"] == {}
        assert current.authority["undo"] is False
        assert current.authority["view"] is explicit_view_request
        assert current.options["action_intent"] == "none"
        if explicit_view_request:
            assert current.command["command"]["panel"] == "records"
    finally:
        await workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_context", [A, B])
async def test_answer_followups_queue_distinct_runs_and_never_enter_active_boundary(
    tmp_path, active_context
):
    entered, release = asyncio.Event(), asyncio.Event()

    class ControlledRuntime(RecordingRuntime):
        async def run(self, prompt):
            if len(self.calls) == 1:
                entered.set()
                await release.wait()
                assert await self.steering() == []
            return await super().run(prompt)

    workspace = Workspace(
        config(tmp_path),
        runtime_factory=ControlledRuntime,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    try:
        await workspace.prompt(sid, "Phân tích dữ liệu", active_context, "vi")
        await asyncio.wait_for(entered.wait(), timeout=5)
        active_rid = workspace.active_run_ids[sid]
        authority = {"filters": {"severity": "Critical"}, "view": True, "undo": True}
        workspace.ui_authorities[active_rid] = authority
        prompts = ["Không đổi bộ lọc; giải thích câu trả lời này.", "Tóm tắt cùng câu trả lời này."]
        for prompt in prompts:
            assert await workspace.prompt(sid, prompt, reply_to=reference) == {"queued": True}
        assert (
            workspace.store.take_instructions(
                sid, active_rid, include_unresolved=True, analysis_context_id=A, language="vi"
            )
            == []
        )
        assert len(RecordingRuntime.calls) == 1
        assert workspace.ui_authorities[active_rid] == authority
        assert workspace.active_contexts[sid] == active_context
        assert workspace.store.investigation(sid)["ui"]["context_id"] == active_context
        release.set()
        await finish_runs(workspace, sid)
        assert [call.prompt for call in RecordingRuntime.calls[1:]] == prompts
        runs = workspace.store.session(sid)["runs"]
        assert len(runs) == 4
        assert all(run["state"] == "completed" for run in runs)
        assert [run["analysis_context_id"] for run in runs[-2:]] == [A, A]
        assert all(run["action_intent"] == "none" for run in runs[-2:])
        assert workspace.store.session(sid)["pending_instructions"] == 0
    finally:
        release.set()
        await workspace.close()


def test_answer_queue_survives_reopen_and_keeps_single_instruction_identity(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    reference = {"run_id": "a" * 32, "message_id": 12}
    for text in ("Explain this.", "Summarize this."):
        store.queue_instruction(sid, text, A, "vi", "none", reference)
    store.close()
    restored = SessionStore(tmp_path)
    try:
        first = restored.next_queued_run(sid)
        assert first["reply_to"] == reference
        assert (
            restored.take_instructions(
                sid, "unrelated", include_unresolved=True, analysis_context_id=A, language="vi"
            )
            == []
        )
        assert restored.take_instructions(sid, "first-followup", **first) == ["Explain this."]
        restored.finish_run("first-followup", "completed")
        second = restored.next_queued_run(sid)
        assert second["reply_to"] == reference
        assert second["queued_instruction_id"] != first["queued_instruction_id"]
        assert restored.take_instructions(sid, "second-followup", **second) == ["Summarize this."]
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_queued_followup_rechecks_source_before_running_and_retains_failed_input(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedRuntime(RecordingRuntime):
        async def run(self, prompt):
            entered.set()
            await release.wait()
            return await super().run(prompt)

    workspace = Workspace(
        config(tmp_path), runtime_factory=DelayedRuntime, analysis_service=ScopedAnalysis(tmp_path)
    )
    sid = workspace.store.create_session()["id"]
    reference = source_answer(workspace, sid)
    prompt = "Explain this exact answer."
    try:
        await workspace.prompt(sid, "Phân tích dữ liệu", B, "vi")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await workspace.prompt(sid, prompt, reply_to=reference) == {"queued": True}
        workspace.store.emit(
            sid,
            reference["run_id"],
            "message",
            {"role": "assistant", "text": "A replacement answer", "qa": {"verified": True}},
        )
        release.set()
        await finish_runs(workspace, sid)
        runs = workspace.store.session(sid)["runs"]
        assert runs[-1]["state"] == "failed"
        assert runs[-1]["analysis_context_id"] == A
        assert len(RecordingRuntime.calls) == 1
        pending = workspace.store.db.execute(
            "SELECT text,reply_to,state FROM steering WHERE session_id=?", (sid,)
        ).fetchone()
        assert pending["text"] == prompt
        assert json.loads(pending["reply_to"]) == reference
        assert pending["state"] != "completed"
        assert workspace.store.session(sid)["pending_instructions"] == 1
    finally:
        release.set()
        await workspace.close()


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_answer_reference_reaches_shared_prompt_contract_from_both_transports(tmp_path, transport):
    app = create_app(
        config(tmp_path),
        runtime_factory=RecordingRuntime,
        analysis_service=ScopedAnalysis(tmp_path),
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        workspace = app.state.workspace
        reference = source_answer(workspace, sid)
        payload = {"text": "Explain this answer.", "reply_to": reference, "language": "en"}
        if transport == "http":
            response = client.post(f"/api/sessions/{sid}/prompt", json=payload)
            response.raise_for_status()
            assert response.json() == {"queued": False}
        else:
            with client.websocket_connect(f"/api/sessions/{sid}/ws") as socket:
                socket.send_json({"type": "prompt", **payload})
                while True:
                    event = socket.receive_json()
                    if event["type"] == "status" and event["data"].get("reply_to") == reference:
                        break
        wait_for(lambda: len(RecordingRuntime.calls) == 1)
        wait_for(lambda: workspace.store.session(sid)["runs"][-1]["state"] == "completed")
        assert RecordingRuntime.calls[0].prompt == payload["text"]
        assert workspace.store.session(sid)["runs"][-1]["language"] == "en"
