"""Dashboard-to-agent handoff must preserve one scope per running analysis."""

import asyncio
import threading
import time

from analyst.app import create_app
from analyst.config import Settings
from analyst.store import SessionStore
from fastapi.testclient import TestClient

A, B = "a" * 64, "b" * 64


class AnalysisFixture:
    def __init__(self, root):
        self.inputs = {}
        for context_id, value in ((A, 2), (B, 9)):
            path = root / f"scope-{context_id[0]}.csv"
            path.write_text(f"value\n{value}\n", encoding="utf-8")
            self.inputs[context_id] = path

    async def context_by_id(self, context_id):
        if context_id not in self.inputs:
            raise KeyError(context_id)
        return {
            "context": {
                "context_id": context_id,
                "snapshot_id": "synthetic",
                "filters": {"category": context_id[0]},
            },
            "evidence": {"execution_id": f"receipt-{context_id[0]}"},
            "links": {},
        }

    async def input_paths_for_context(self, context_id):
        return [self.inputs[context_id]]


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected synthetic analysis boundary was not reached")


def test_context_change_is_a_separate_run_and_inputs_reset(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Runtime:
        calls = []

        def __init__(
            self,
            settings,
            toolkit,
            emit,
            steering=None,
            initial_items=None,
            analysis_context=None,
            analysis_service=None,
        ):
            self.toolkit, self.steering = toolkit, steering
            self.context = analysis_context["context"]["context_id"]

        async def run(self, prompt):
            call = {
                "prompt": prompt,
                "context": self.context,
                "toolkit": self.toolkit,
                "inputs": list(self.toolkit.input_paths),
            }
            self.calls.append(call)
            if len(self.calls) == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            call["steering"] = await self.steering()
            return {"status": "completed", "usage": [], "cost_usd": 0}

    service = AnalysisFixture(tmp_path)
    settings = Settings(
        state_dir=str(tmp_path / "state"),
        mode="mock",
        memory_enabled=False,
        enable_file_search=False,
    )
    app = create_app(settings, runtime_factory=Runtime, analysis_service=service)
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        url = f"/api/sessions/{sid}/prompt"
        assert (
            client.post(url, json={"text": "FIRST_SCOPE", "analysis_context_id": A}).status_code
            == 200
        )
        wait_for(entered.is_set)
        assert client.post(url, json={"text": "SECOND_SCOPE", "analysis_context_id": B}).json()[
            "queued"
        ]
        assert client.post(
            url, json={"text": "SAME_SCOPE_UPDATE", "analysis_context_id": A}
        ).json()["queued"]
        release.set()
        wait_for(lambda: len(Runtime.calls) == 2)
        wait_for(
            lambda: client.get(f"/api/sessions/{sid}").json()["runs"][-1]["state"] == "completed"
        )
        first, second = Runtime.calls
        assert first["context"] == A and first["steering"] == ["SAME_SCOPE_UPDATE"]
        assert second["context"] == B and second["prompt"] == "SECOND_SCOPE"
        assert second["steering"] == []
        assert first["toolkit"] is not second["toolkit"]
        assert first["inputs"] == [service.inputs[A]]
        assert second["inputs"] == [service.inputs[B]]
        session = client.get(f"/api/sessions/{sid}").json()
        assert [run["analysis_context_id"] for run in session["runs"]] == [A, B]
        assert session["pending_instructions"] == 0
        assert [
            event["data"]["context_id"]
            for event in session["events"]
            if event["type"] == "analysis_context"
        ] == [A, B]


def test_invalid_or_missing_snapshot_never_falls_back_to_full_case(tmp_path):
    app = create_app(
        Settings(state_dir=str(tmp_path / "state"), mode="mock"),
        analysis_service=AnalysisFixture(tmp_path),
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions").json()["id"]
        for context_id in ("../private", "c" * 64, {}, ""):
            response = client.post(
                f"/api/sessions/{sid}/prompt",
                json={"text": "Analyze this view", "analysis_context_id": context_id},
            )
            assert response.status_code == 422
        assert client.get(f"/api/sessions/{sid}").json()["runs"] == []


def test_durable_context_instructions_never_merge_on_restart(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create_session()["id"]
    rid, _ = store.begin_run(sid, A)
    store.queue_instruction(sid, "SCOPE_A", A)
    store.queue_instruction(sid, "SCOPE_B", B)
    assert store.take_instructions(sid, rid, analysis_context_id=A) == ["SCOPE_A"]
    store.close()
    store = SessionStore(tmp_path)
    store.recover_interrupted()
    resumed, _ = store.begin_run(sid, A)
    assert store.take_instructions(
        sid, resumed, include_unresolved=True, analysis_context_id=A
    ) == ["SCOPE_A"]
    assert store.take_instructions(sid, resumed, analysis_context_id=A) == []
    store.finish_run(resumed, "completed")
    assert store.next_queued_context(sid) == {"analysis_context_id": B}
    other, _ = store.begin_run(sid, B)
    assert store.take_instructions(sid, other, analysis_context_id=B) == ["SCOPE_B"]
    store.finish_run(other, "completed")
    assert store.session(sid)["pending_instructions"] == 0
    store.close()
