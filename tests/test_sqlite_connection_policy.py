import ast
import asyncio
import sqlite3
from pathlib import Path

import bot as app


def test_async_factory_applies_busy_timeout_and_foreign_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "async-policy.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    with sqlite3.connect(db_path) as seed:
        journal_mode_before = seed.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous_before = seed.execute("PRAGMA synchronous").fetchone()[0]

    async def inspect_policy():
        async with app.open_db() as db:
            values = {
                "busy_timeout": (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0],
                "foreign_keys": (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0],
                "journal_mode": (await (await db.execute("PRAGMA journal_mode")).fetchone())[0],
                "synchronous": (await (await db.execute("PRAGMA synchronous")).fetchone())[0],
                "isolation_level": db.isolation_level,
            }
            await db.execute("CREATE TABLE policy_probe (id INTEGER)")
            await db.execute("INSERT INTO policy_probe VALUES (1)")
            values["in_transaction_after_dml"] = db.in_transaction
            await db.rollback()
            return values

    values = asyncio.run(inspect_policy())

    assert values == {
        "busy_timeout": app.DB_BUSY_TIMEOUT_MS,
        "foreign_keys": 1,
        "journal_mode": journal_mode_before,
        "synchronous": synchronous_before,
        "isolation_level": "",
        "in_transaction_after_dml": True,
    }
    with sqlite3.connect(db_path) as verify:
        assert verify.execute("SELECT COUNT(*) FROM policy_probe").fetchone()[0] == 0
        assert verify.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode_before


def test_sync_factory_applies_busy_timeout_and_foreign_keys(tmp_path, monkeypatch):
    db_path = tmp_path / "sync-policy.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    with sqlite3.connect(db_path) as seed:
        journal_mode_before = seed.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous_before = seed.execute("PRAGMA synchronous").fetchone()[0]

    with app.open_db_sync() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == app.DB_BUSY_TIMEOUT_MS
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode_before
        assert db.execute("PRAGMA synchronous").fetchone()[0] == synchronous_before
        assert db.isolation_level == ""
        db.execute("CREATE TABLE policy_probe (id INTEGER)")
        db.execute("INSERT INTO policy_probe VALUES (1)")
        assert db.in_transaction is True
        db.rollback()

    with sqlite3.connect(db_path) as verify:
        assert verify.execute("SELECT COUNT(*) FROM policy_probe").fetchone()[0] == 0
        assert verify.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode_before


def test_init_db_uses_factory_for_exact_v2_validation(tmp_path, monkeypatch):
    db_path = tmp_path / "init-policy.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    asyncio.run(app.init_db())
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == app.CURRENT_SCHEMA_VERSION
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_production_connect_calls_are_confined_to_factories():
    source_path = Path(app.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    direct_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        call_name = f"{owner.id}.{node.func.attr}"
        if call_name not in {"aiosqlite.connect", "sqlite3.connect"}:
            continue
        parent = node
        while parent is not None and not isinstance(
            parent, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            parent = parents.get(parent)
        direct_calls.append((call_name, parent.name if parent else None, node.lineno))

    assert [(call_name, function_name) for call_name, function_name, _ in direct_calls] == [
        ("aiosqlite.connect", "open_db"),
        ("sqlite3.connect", "open_db_sync"),
    ]
