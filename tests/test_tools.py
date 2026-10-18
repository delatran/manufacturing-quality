import asyncio

import pytest
from analyst.tools import AnalystTools


def test_isolated_python_worker_round_trips_vietnamese_paths_and_text(tmp_path):
    directory = tmp_path / "Dữ liệu chất lượng"
    directory.mkdir()
    source = directory / "sự kiện.csv"
    source.write_text("amount\n12\n", encoding="utf-8")

    async def run():
        tools = AnalystTools(directory / "kết quả", [source])
        try:
            receipt = await tools.call(
                "local_python_repl",
                {
                    "code": "print('Kiểm tra chất lượng')\nresult = {'paths': INPUT_PATHS, 'message': 'Dữ liệu tiếng Việt', 'count': int(pd.read_csv(INPUT_PATHS[0]).amount.sum())}"
                },
            )
            assert receipt["result"]["ok"], receipt["result"]
            assert receipt["result"]["value"] == {
                "paths": [str(source.resolve())],
                "message": "Dữ liệu tiếng Việt",
                "count": 12,
            }
            assert receipt["result"]["stdout"] == "Kiểm tra chất lượng\n"
        finally:
            await tools.close()

    asyncio.run(run())


def test_real_python_sql_profile_and_artifact(tmp_path):
    source = tmp_path / "input.csv"
    source.write_text("group,amount\na,3\na,4\nb,9\n")

    async def run():
        tools = AnalystTools(tmp_path / "run", [source])
        try:
            profile = await tools.call("dataset_profiler", {"path": str(source)})
            assert profile["result"]["datasets"]["input"]["row_count"] == 3
            sql = await tools.call(
                "local_sql_engine", {"query": "SELECT sum(amount) AS total FROM input"}
            )
            assert sql["result"]["rows"][0]["total"] == 16
            first = await tools.call(
                "local_python_repl",
                {
                    "code": "df = pd.read_csv(INPUT_PATHS[0])\nresult = {'total': int(df.amount.sum())}"
                },
            )
            assert first["result"]["value"]["total"] == 16, first
            second = await tools.call(
                "local_python_repl",
                {
                    "code": "df.groupby('group').amount.sum().plot.bar()\nplt.savefig(ARTIFACT_DIR + '/groups.png')\nresult = {'rows':len(df)}"
                },
            )
            assert second["result"]["value"]["rows"] == 3, second
            assert any(item["name"] == "groups.png" for item in second["artifacts"])
            assert not tools.ledger.verify_integrity()
        finally:
            await tools.close()

    asyncio.run(run())
    assert source.read_text() == "group,amount\na,3\na,4\nb,9\n"


def test_accidental_writes_and_external_sql_blocked(tmp_path):
    source = tmp_path / "input.csv"
    source.write_text("amount\n1\n")

    async def run():
        tools = AnalystTools(tmp_path / "run", [source])
        try:
            blocked = await tools.call(
                "local_python_repl", {"code": f"open({str(source)!r}, 'w').write('destroyed')"}
            )
            assert not blocked["result"]["ok"]
            for query in [
                "COPY (SELECT 1) TO 'escape.csv'",
                "SELECT * FROM read_csv('input.csv')",
                "SELECT 1; SELECT 2",
            ]:
                assert not (await tools.call("local_sql_engine", {"query": query}))["result"]["ok"]
            assert not (
                await tools.call("local_python_repl", {"code": "import os\nresult=os.environ"})
            )["result"]["ok"]
        finally:
            await tools.close()

    asyncio.run(run())
    assert source.read_text() == "amount\n1\n"


def test_time_limit_resets_session(tmp_path):
    async def run():
        tools = AnalystTools(tmp_path / "run", [], timeout_seconds=3)
        try:
            result = await tools.call("local_python_repl", {"code": "while True: pass"})
            assert not result["result"]["ok"]
            assert "time limit" in result["result"]["error"]
        finally:
            await tools.close()

    asyncio.run(run())


def test_new_run_preserves_python_variables_and_switches_artifacts(tmp_path):
    async def run():
        tools = AnalystTools(tmp_path / "first", [])
        try:
            first = await tools.call("local_python_repl", {"code": "retained=41\nresult=retained"})
            assert first["result"]["value"] == 41
            await tools.begin_run(tmp_path / "second")
            second = await tools.call(
                "local_python_repl",
                {"code": "open(ARTIFACT_DIR + '/new.txt','w').write('ok')\nresult=retained+1"},
            )
            assert second["result"]["value"] == 42, second
            assert (tmp_path / "second" / "artifacts" / "new.txt").read_text() == "ok"
            assert len(tools.ledger.records()) == 1
        finally:
            await tools.close()

    asyncio.run(run())


def test_disabled_memory_has_no_storage_or_tools(tmp_path):
    async def run():
        tools = AnalystTools(tmp_path / "run", [], memory_enabled=False)
        try:
            assert tools.memory is None
            assert not (tmp_path / "run" / "memory").exists()
            assert not any(schema["name"].startswith("memory_") for schema in tools.schemas)
            for name, args in [
                ("memory_recall", {"query": "schema"}),
                ("memory_store", {"key": "x", "content": "note", "category": "schema"}),
                ("memory_update", {"key": "x", "content": "note", "category": "schema"}),
            ]:
                with pytest.raises(ValueError, match="disabled"):
                    await tools.call(name, args)
            await tools.begin_run(tmp_path / "second")
            assert tools.memory is None
            assert not (tmp_path / "second" / "memory").exists()
        finally:
            await tools.close()

    asyncio.run(run())


def test_tool_execution_schemas_support_rationale_and_preserve_envelope(tmp_path):
    source = tmp_path / "data.csv"
    source.write_text("id,val\n1,10\n2,20\n", encoding="utf-8")

    async def run():
        tools = AnalystTools(tmp_path / "run", [source])
        try:
            exec_schemas = {
                s["name"]: s
                for s in tools.schemas
                if s["name"] in {"local_python_repl", "local_sql_engine", "dataset_profiler"}
            }
            for schema in exec_schemas.values():
                assert "rationale" in schema["parameters"]["properties"]
                assert schema["parameters"]["properties"]["rationale"]["description"] == (
                    "One concise sentence explaining why this tool is chosen and which analytical hypothesis or decision question it answers."
                )
                assert set(schema["parameters"]["required"]) == set(
                    schema["parameters"]["properties"]
                )

            repl_res = await tools.call(
                "local_python_repl",
                {
                    "code": "result = {'status': 'completed', 'scope': {'test': '1'}, 'dataset_version': 'v1', 'policy_version': 'v1', 'result_reference': 'ref1', 'result': {'x': 42}}",
                    "rationale": "Verify envelope preservation in repl",
                },
            )
            assert repl_res.get("rationale") == "Verify envelope preservation in repl"
            envelope_val = repl_res["result"]["value"]
            assert envelope_val["status"] == "completed"
            assert envelope_val["rationale"] == "Verify envelope preservation in repl"
            assert envelope_val["result"] == {"x": 42}

            sql_res = await tools.call(
                "local_sql_engine",
                {
                    "query": "SELECT sum(val) AS total FROM data",
                    "rationale": "Calculate aggregate total",
                },
            )
            assert sql_res.get("rationale") == "Calculate aggregate total"
            assert sql_res["result"]["rows"][0]["total"] == 30
        finally:
            await tools.close()

    asyncio.run(run())
