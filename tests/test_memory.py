import sqlite3

import pytest
from analyst.memory import AnalystMemory


def test_curated_memory_persists_and_updates(tmp_path):
    memory = AnalystMemory(tmp_path)
    memory.store("cost", "Known cost excludes missing cost values.", "kpi")
    memory.store("schema", "Events join to master using product identifier.", "schema")
    assert memory.recall("missing cost")["items"][0]["key"] == "cost"
    memory.store("cost", "Known cost sums nonmissing values.", "kpi", update=True)
    assert "sums nonmissing" in AnalystMemory(tmp_path).recall("cost")["items"][0]["content"]
    assert "offline hashed" in memory.markdown_path.read_text()


def test_memory_rejects_raw_records(tmp_path):
    memory = AnalystMemory(tmp_path)
    with pytest.raises(ValueError, match="Raw rows"):
        memory.store("raw", '[{"name":"person","amount":1}]', "schema")
    with pytest.raises(ValueError):
        memory.store("raw", "a,b\n1,2\n3,4\n", "data_quality")


def test_every_connection_closes_and_failed_transaction_rolls_back(tmp_path, monkeypatch):
    import analyst.memory as module

    connect = sqlite3.connect
    opened = []

    def tracking_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", tracking_connect)
    memory = AnalystMemory(tmp_path)
    memory.store("schema", "The amount column is numeric.", "schema")
    memory.recall("amount")
    with pytest.raises(RuntimeError, match="abort"):
        with memory._connect() as connection:
            connection.execute(
                "INSERT INTO knowledge VALUES (?,?,?,?,?)",
                ("rollback", "schema", "note", "[]", "now"),
            )
            raise RuntimeError("abort")
    with memory._connect() as connection:
        assert (
            connection.execute("SELECT key FROM knowledge WHERE key='rollback'").fetchone() is None
        )
        assert connection.execute("SELECT key FROM knowledge WHERE key='schema'").fetchone() == (
            "schema",
        )
    assert len(opened) >= 6
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
