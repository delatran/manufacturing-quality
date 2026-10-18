"""Fixture programs must read every format they select without changing row counts."""

import asyncio

import pandas as pd
import pytest
from analyst.config import Settings
from analyst.runtime import AnalystRuntime
from analyst.tools import AnalystTools


@pytest.mark.parametrize("extension", ["jsonl", "ndjson", "xlsm"])
def test_fixture_program_reads_selected_format(tmp_path, extension):
    source = tmp_path / f"records.{extension}"
    frame = pd.DataFrame({"amount": [1, 2], "label": ["first", "second"]})
    if extension == "xlsm":
        frame.to_excel(source, index=False, engine="openpyxl")
    else:
        frame.to_json(source, orient="records", lines=True)
    original = source.read_bytes()

    async def run():
        toolkit = AnalystTools(tmp_path / "run", [source], memory_enabled=False)
        runtime = AnalystRuntime(
            Settings(mode="mock", memory_enabled=False), toolkit, lambda *_: None
        )
        runtime.prompt = "Inspect the supplied dataset."
        try:
            arguments = next(
                arguments
                for name, arguments in runtime.fixture_sequence()
                if name == "local_python_repl"
            )
            receipt = await toolkit.call("local_python_repl", arguments)
            assert receipt["result"]["ok"], receipt
            summary = receipt["result"]["value"]
            assert summary["row_count"] == 2 and summary["column_count"] == 2
            assert summary["numeric_summary"]["amount"]["count"] == 2
            assert source.read_bytes() == original
        finally:
            await toolkit.close()

    asyncio.run(run())
