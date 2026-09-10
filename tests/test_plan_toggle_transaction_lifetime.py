import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import bot as app


USER_ID = 10001


class FakeMessage:
    def __init__(self, text, transaction_is_active=None):
        self.text = text
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers = []
        self.transaction_is_active = transaction_is_active or (lambda: False)

    async def answer(self, text, **kwargs):
        assert not self.transaction_is_active()
        self.answers.append((text, kwargs))


@pytest.fixture
def plan_db(tmp_path, monkeypatch):
    db_path = tmp_path / "plan-toggle.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(user_id, from_asset, amount, interval_hours, btc_address, next_run, active) "
            "VALUES (?, 'USDT-ARB', 25, 24, 'bc1qtest', 4000000000, 1)",
            (USER_ID,),
        )
    return db_path


def read_active(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT active FROM dca_plans WHERE id = 1").fetchone()[0]


def install_connection_lifetime_probe(monkeypatch):
    real_open_db = app.open_db
    live_connections = []
    nested_opened_during_write = []
    events = []

    @asynccontextmanager
    async def tracked_open_db():
        nested_opened_during_write.append(
            any(db.in_transaction for db in live_connections)
        )
        async with real_open_db() as db:
            await db.set_trace_callback(
                lambda statement: events.append(" ".join(statement.split()))
            )
            live_connections.append(db)
            try:
                yield db
            finally:
                live_connections.remove(db)

    monkeypatch.setattr(app, "open_db", tracked_open_db)
    return live_connections, nested_opened_during_write, events


@pytest.mark.parametrize(
    ("handler_name", "command", "initial_active", "expected_active", "message_fragment"),
    [
        ("cmd_pause", "/pause_1", 1, 0, "⏸ План #1 приостановлен"),
        ("cmd_resume", "/resume_1", 0, 1, "▶️ План #1 возобновлён"),
    ],
)
def test_plan_toggle_display_lookup_precedes_write_and_never_nests_under_writer(
    plan_db,
    monkeypatch,
    handler_name,
    command,
    initial_active,
    expected_active,
    message_fragment,
):
    with sqlite3.connect(plan_db) as db:
        db.execute("UPDATE dca_plans SET active = ? WHERE id = 1", (initial_active,))

    live, nested_during_write, events = install_connection_lifetime_probe(monkeypatch)
    real_display_lookup = app.get_plan_display_number

    async def tracked_display_lookup(user_id, plan_id):
        assert live
        assert not any(db.in_transaction for db in live)
        events.append("DISPLAY_LOOKUP")
        return await real_display_lookup(user_id, plan_id)

    monkeypatch.setattr(app, "get_plan_display_number", tracked_display_lookup)
    message = FakeMessage(command, lambda: any(db.in_transaction for db in live))

    asyncio.run(getattr(app, handler_name)(message))

    update_index = next(
        index for index, event in enumerate(events)
        if event.startswith("UPDATE dca_plans SET active =")
    )
    assert events.index("DISPLAY_LOOKUP") < update_index
    assert not any(nested_during_write)
    assert read_active(plan_db) == expected_active
    assert message_fragment in message.answers[-1][0]


@pytest.mark.parametrize(
    ("handler_name", "command"),
    [("cmd_pause", "/pause_1"), ("cmd_resume", "/resume_1")],
)
def test_plan_toggle_display_lookup_error_happens_before_mutation(
    plan_db, monkeypatch, handler_name, command
):
    initial_active = read_active(plan_db)

    async def fail_display_lookup(_user_id, _plan_id):
        raise RuntimeError("injected display lookup failure")

    monkeypatch.setattr(app, "get_plan_display_number", fail_display_lookup)

    with pytest.raises(RuntimeError, match="display lookup failure"):
        asyncio.run(getattr(app, handler_name)(FakeMessage(command)))

    assert read_active(plan_db) == initial_active
    with sqlite3.connect(plan_db, timeout=0.1) as probe:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()


@pytest.mark.parametrize(
    ("handler_name", "command"),
    [("cmd_pause", "/pause_1"), ("cmd_resume", "/resume_1")],
)
def test_plan_toggle_commit_error_rolls_back_and_releases_writer_lock(
    plan_db, monkeypatch, handler_name, command
):
    initial_active = read_active(plan_db)
    real_open_db = app.open_db

    class CommitFailureConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        async def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    @asynccontextmanager
    async def fail_commit_open_db():
        async with real_open_db() as db:
            yield CommitFailureConnection(db)

    monkeypatch.setattr(app, "open_db", fail_commit_open_db)

    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        asyncio.run(getattr(app, handler_name)(FakeMessage(command)))

    assert read_active(plan_db) == initial_active
    with sqlite3.connect(plan_db, timeout=0.1) as probe:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()


@pytest.mark.parametrize(
    ("handler_name", "command"),
    [("cmd_pause", "/pause_1"), ("cmd_resume", "/resume_1")],
)
def test_plan_toggle_missing_plan_preserves_failure_path(
    plan_db, monkeypatch, handler_name, command
):
    initial_active = read_active(plan_db)

    async def missing_plan(_message, _display_index):
        return 999

    monkeypatch.setattr(app, "resolve_display_plan_id", missing_plan)
    message = FakeMessage(command)

    asyncio.run(getattr(app, handler_name)(message))

    assert read_active(plan_db) == initial_active
    assert "План не найден" in message.answers[-1][0]
