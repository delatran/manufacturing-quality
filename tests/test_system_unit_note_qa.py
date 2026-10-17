from __future__ import annotations

import asyncio
import json
from dataclasses import fields

import pytest
from agents import ModelResponse, Usage
from analyst.case_study.analytics import Scope
from analyst.config import Settings
from analyst.evidence import canonical, sha256
from analyst.managed_agents import ManagedAgentsModel
from analyst.model import dump
from analyst.runtime import AnalystRuntime, _message
from analyst.tools import AnalystTools
from openai.types.responses import ResponseFunctionToolCall
from test_runtime import FrozenAnalysisService, canonical_context


@pytest.mark.parametrize("language,value", [("en", 12), ("vi", 12), ("en", 13)])
def test_native_two_bullet_steering_reviews_authored_answer_with_owned_unit_legend(
    tmp_path, monkeypatch, language, value
):
    async def scenario():
        tools = AnalystTools(tmp_path / "run", [])
        events, qa_inputs = [], []

        async def emit(kind, data):
            events.append((kind, data))

        payload = canonical_context(tmp_path)
        runtime = AnalystRuntime(
            Settings(mode="live", api_key="offline-test", qa_max_retries=0, unbounded_runs=False),
            tools,
            emit,
            language=language,
            analysis_context=payload,
            analysis_service=FrozenAnalysisService(payload),
        )
        instruction = "Keep final answer to two bullets"

        class ScriptedManagedModel(ManagedAgentsModel):
            def __init__(self, role):
                self.role, self.step, self.history, self.usage = role, 0, [], []
                self.session_id = None

            async def steer(self, text, *, instruction_id):
                assert text == instruction and instruction_id == "two-bullets"
                return {"accepted": True, "mode": "native_inflight"}

            async def get_response(
                self,
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                **kwargs,
            ):
                self.step += 1
                if self.role == "QA Verifier":
                    candidate = json.loads(
                        input if isinstance(input, str) else input[-1]["content"]
                    )
                    qa_inputs.append(candidate)
                    assert instruction in candidate["user_request"]
                    assert "authored report alone" in system_instructions
                    assert "correctness against rendered_report" in system_instructions
                    paragraphs = candidate["report"].splitlines()
                    format_ok = len(paragraphs) == 2 and all(
                        line.startswith("- ") for line in paragraphs
                    )
                    errors = candidate["audit"]["errors"] + (
                        [] if format_ok else ["The requested two-bullet format was violated"]
                    )
                    output = [
                        _message(
                            canonical({"verified": not errors, "errors": errors, "suggestions": []})
                        )
                    ]
                elif self.step == 1:
                    receipt = await runtime.steer(instruction, instruction_id="two-bullets")
                    assert receipt["accepted"] and receipt["mode"] == "native_inflight"
                    output = [
                        ResponseFunctionToolCall(
                            id="fc_evidence",
                            call_id="call_evidence",
                            name="analysis_query",
                            arguments=canonical(
                                {
                                    "section": "kpis",
                                    "filters": {field.name: None for field in fields(Scope)},
                                }
                            ),
                            type="function_call",
                            status="completed",
                        )
                    ]
                else:
                    identity = next(
                        claim.id
                        for claim in runtime.claim_catalog.values()
                        if claim.path == "$.analysis.kpis.event_count"
                    )
                    report = (
                        f"- Recorded {value} logged defect events [claim:{identity}].\n"
                        "- Causes remain unverified."
                        if language == "en"
                        else f"- Đã ghi nhận {value} sự kiện lỗi [claim:{identity}].\n"
                        "- Nguyên nhân chưa được xác minh."
                    )
                    output = [_message(canonical({"report": report}))]
                self.history = (
                    [{"role": "user", "content": input}] if isinstance(input, str) else dump(input)
                ) + dump(output)
                return ModelResponse(
                    output=output,
                    usage=Usage(requests=0),
                    response_id=f"offline_{self.role}_{self.step}",
                )

            async def stream_response(self, *args, **kwargs):
                raise NotImplementedError
                yield

        def model(role):
            instance = ScriptedManagedModel(role)
            runtime.models.append(instance)
            return instance

        monkeypatch.setattr(runtime, "model", model)
        try:
            result = await runtime.run("Summarize the observed event count.")
            assert len(qa_inputs) == 1, result["qa"]
            candidate = qa_inputs[0]
            legend = candidate["system_unit_legend"]
            assert legend and legend not in candidate["report"]
            assert candidate["report"] in candidate["rendered_report"]
            assert candidate["rendered_report"].count(legend) == 1
            reviews = [
                entry
                for entry in tools.ledger.records()
                if entry["tool"] == "report_semantic_review"
                and entry["result"]["phase"] == "requested"
            ]
            assert len(reviews) == 1
            receipt = reviews[0]["result"]
            retained = tools.run_dir / receipt["runner_input_path"]
            assert retained.read_text(encoding="utf-8") == canonical(candidate)
            assert receipt["runner_input_sha256"] == sha256(canonical(candidate))
            if value == 12:
                assert result["status"] == "completed", result["qa"]
                assert result["qa"]["verified"]
                assert candidate["rendered_report"] == result["report"]
                assert result["report"].count(legend) == 1
                assert (
                    len([line for line in result["report"].splitlines() if line.startswith("- ")])
                    == 2
                )
                assert not any(kind == "qa_retry" for kind, _ in events)
                assert any(kind == "steering_applied" for kind, _ in events)
            else:
                assert result["status"] == "unverified"
                assert not result["qa"]["verified"]
                assert candidate["audit"]["errors"]
        finally:
            await tools.close()

    asyncio.run(scenario())
