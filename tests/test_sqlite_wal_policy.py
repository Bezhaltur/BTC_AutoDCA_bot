import ast
import asyncio
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import bot as app


def install_worker_statement_hooks(monkeypatch, worker_hooks):
    original_execute = app.aiosqlite.Connection._execute
    submitted = []
    hooked = set()

    async def intercept_execute(connection, function, *args, **kwargs):
        normalized_sql = " ".join(str(args[0]).split()).upper() if args else ""
        submitted.append(normalized_sql)
        worker_hook = worker_hooks.get(normalized_sql)
        if worker_hook is not None and normalized_sql not in hooked:
            hooked.add(normalized_sql)

            def hooked_function(*worker_args, **worker_kwargs):
                return worker_hook(function, worker_args, worker_kwargs)

            return await original_execute(
                connection, hooked_function, *args, **kwargs
            )
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept_execute)
    return submitted


async def wait_until_submitted(submitted, statement):
    for _ in range(1000):
        if statement in submitted:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"statement was not submitted: {statement}")


async def assert_wal_connection_policy(db):
    assert (await (await db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
    assert (await (await db.execute("PRAGMA synchronous")).fetchone())[0] == 2
    assert (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0] == 1


def assert_wal_and_write_lock_released(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def run_init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def schema_and_data_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall(),
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT * FROM wallets ORDER BY user_id").fetchall(),
            db.execute("SELECT * FROM dca_plans ORDER BY id").fetchall(),
            db.execute("SELECT * FROM sent_transactions ORDER BY id").fetchall(),
            db.execute("SELECT * FROM completed_orders ORDER BY id").fetchall(),
        )


def test_async_factory_synchronous_readback_mismatch_fails_closed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "async-full-mismatch.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    real_connect = app.aiosqlite.connect

    class FalseCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetchone(self):
            return (1,)

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.split()).upper() == "PRAGMA SYNCHRONOUS":
                return FalseCursor()
            return self.db.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.db, name)

    class Context:
        def __init__(self, inner):
            self.inner = inner

        async def __aenter__(self):
            return Proxy(await self.inner.__aenter__())

        async def __aexit__(self, *args):
            return await self.inner.__aexit__(*args)

    monkeypatch.setattr(
        app.aiosqlite,
        "connect",
        lambda *args, **kwargs: Context(real_connect(*args, **kwargs)),
    )

    async def open_once():
        async with app.open_db():
            pass

    with pytest.raises(RuntimeError, match="synchronous mismatch"):
        asyncio.run(open_once())


def test_sync_factory_synchronous_readback_mismatch_fails_closed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sync-full-mismatch.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    real_connect = app.sqlite3.connect

    class FalseResult:
        def fetchone(self):
            return (1,)

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.split()).upper() == "PRAGMA SYNCHRONOUS":
                return FalseResult()
            return self.db.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.db, name)

    class Context:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return Proxy(self.inner.__enter__())

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    monkeypatch.setattr(
        app.sqlite3,
        "connect",
        lambda *args, **kwargs: Context(real_connect(*args, **kwargs)),
    )
    with pytest.raises(RuntimeError, match="synchronous mismatch"):
        with app.open_db_sync():
            pass


def test_fresh_database_enables_wal_then_reopens_with_full_sync(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fresh-wal.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    asyncio.run(app.ensure_wal_mode())

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    async def inspect_async():
        async with app.open_db() as db:
            return (
                (await (await db.execute("PRAGMA journal_mode")).fetchone())[0],
                (await (await db.execute("PRAGMA synchronous")).fetchone())[0],
                (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0],
                (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0],
            )

    assert asyncio.run(inspect_async()) == (
        "wal",
        2,
        1,
        app.DB_BUSY_TIMEOUT_MS,
    )
    with app.open_db_sync() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_existing_v2_wal_conversion_and_repeated_setup_preserve_state(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "existing-v2-wal.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO wallets(user_id,wallet_address) VALUES(10001,'0xwallet')"
        )
    before = schema_and_data_snapshot(db_path)
    statements = []
    production_open_db = app.open_db

    @asynccontextmanager
    async def traced_open_db():
        async with production_open_db() as db:
            await db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(app, "open_db", traced_open_db)

    asyncio.run(app.ensure_wal_mode())
    after_first = schema_and_data_snapshot(db_path)
    asyncio.run(app.ensure_wal_mode())
    after_second = schema_and_data_snapshot(db_path)

    assert after_first == after_second == before
    assert sum(
        " ".join(sql.split()).upper() == "PRAGMA JOURNAL_MODE = WAL"
        for sql in statements
    ) == 2
    assert not any(
        sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
        )
        for sql in statements
    )
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "responses,match",
    ((["delete"], "journal_mode mismatch"), (["wal", "delete"], "confirmation mismatch")),
)
def test_wal_readback_mismatch_fails_closed(monkeypatch, responses, match):
    class Cursor:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetchone(self):
            return (self.value,)

    class FakeDb:
        in_transaction = False

        def execute(self, _sql):
            return Cursor(responses.pop(0))

    @asynccontextmanager
    async def fake_open_db():
        yield FakeDb()

    monkeypatch.setattr(app, "open_db", fake_open_db)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(app.ensure_wal_mode())


def test_malformed_schema_fails_before_wal_persistent_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "malformed-before-wal.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE unknown_table(id INTEGER)")
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    with pytest.raises(RuntimeError):
        asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("unknown_table",)
        ]


def test_startup_orders_wal_after_init_and_before_passwords():
    source = Path(app.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    awaited_calls = [
        name
        for _, name in sorted(
            (node.lineno, node.value.func.id)
            for node in ast.walk(main)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        )
    ]
    assert awaited_calls.count("init_db") == 1
    assert awaited_calls.count("ensure_wal_mode") == 1
    assert awaited_calls.index("init_db") < awaited_calls.index("ensure_wal_mode")
    assert awaited_calls.index("ensure_wal_mode") < awaited_calls.index(
        "load_passwords_at_startup"
    )


def test_wal_reader_does_not_block_writer_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "wal-reader-writer.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())

    async def scenario():
        async with app.open_db() as reader, app.open_db() as writer:
            await reader.execute("BEGIN")
            await (await reader.execute("SELECT COUNT(*) FROM wallets")).fetchone()
            await writer.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(20001,'0xreader')"
            )
            await writer.commit()
            assert reader.in_transaction is True
            await reader.rollback()

    asyncio.run(scenario())
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1


def test_two_writers_honor_busy_timeout_and_recover_cleanly(tmp_path, monkeypatch):
    db_path = tmp_path / "wal-two-writers.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    monkeypatch.setattr(app, "DB_BUSY_TIMEOUT_MS", 100)

    async def scenario():
        async with app.open_db() as first, app.open_db() as second:
            await first.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await second.execute("BEGIN IMMEDIATE")
            elapsed = time.monotonic() - started
            assert elapsed >= 0.08
            assert (await (await second.execute("PRAGMA busy_timeout")).fetchone())[0] == 100
            await first.rollback()
            await second.execute("BEGIN IMMEDIATE")
            await second.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(30001,'0xwriter')"
            )
            await second.commit()

    asyncio.run(scenario())
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1


def test_acknowledged_commit_cancellation_under_wal_never_rolls_back(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-cancelled-commit.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    commit_started = threading.Event()
    release_commit = threading.Event()
    statements = []
    original_execute = app.aiosqlite.Connection._execute

    async def intercept(connection, function, *args, **kwargs):
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        statements.append(sql)
        if sql == "COMMIT;":
            def delayed(*worker_args, **worker_kwargs):
                commit_started.set()
                if not release_commit.wait(timeout=5):
                    raise RuntimeError("timed out waiting to release WAL COMMIT")
                return function(*worker_args, **worker_kwargs)

            return await original_execute(connection, delayed, *args, **kwargs)
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)

    async def scenario():
        async with app.open_db() as db:
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(40001,'0xcancel')"
            )
            task = asyncio.create_task(app._commit_scheduler_transaction(db))
            assert await asyncio.to_thread(commit_started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert db.in_transaction is False

    asyncio.run(scenario())
    assert statements.count("COMMIT;") == 1
    assert "ROLLBACK;" not in statements
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_failed_commit_is_rolled_back_under_wal(tmp_path, monkeypatch):
    db_path = tmp_path / "wal-failed-commit.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    statements = []
    original_execute = app.aiosqlite.Connection._execute

    async def intercept(connection, function, *args, **kwargs):
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        statements.append(sql)
        if sql == "COMMIT;":
            def fail_commit(*_worker_args, **_worker_kwargs):
                raise sqlite3.OperationalError("injected WAL COMMIT failure")

            return await original_execute(connection, fail_commit, *args, **kwargs)
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)

    async def scenario():
        async with app.open_db() as db:
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(45001,'0xrollback')"
            )
            with pytest.raises(sqlite3.OperationalError, match="WAL COMMIT failure"):
                await app._commit_scheduler_transaction(db)
            assert db.in_transaction is False

    asyncio.run(scenario())
    assert statements.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_true_queued_commit_cancellation_is_acknowledged_under_wal(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-queued-cancelled-commit.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    commit_started = threading.Event()
    blocker_sql = "SELECT 1 /* WAL COMMIT QUEUE BLOCKER */"

    def block_worker(function, args, kwargs):
        blocker_started.set()
        if not release_blocker.wait(timeout=5):
            raise RuntimeError("timed out waiting to release WAL COMMIT blocker")
        return function(*args, **kwargs)

    def observe_commit(function, args, kwargs):
        commit_started.set()
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {blocker_sql: block_worker, "COMMIT;": observe_commit},
    )

    async def scenario():
        async with app.open_db() as db:
            await assert_wal_connection_policy(db)
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(41001,'0xqueued')"
            )
            blocker_task = asyncio.create_task(db.execute(blocker_sql))
            assert await asyncio.to_thread(blocker_started.wait, 5)

            commit_task = asyncio.create_task(app._commit_scheduler_transaction(db))
            await wait_until_submitted(submitted, "COMMIT;")
            assert commit_started.is_set() is False
            commit_task.cancel()
            await asyncio.sleep(0)
            assert commit_started.is_set() is False
            release_blocker.set()

            blocker_cursor = await blocker_task
            await blocker_cursor.close()
            with pytest.raises(asyncio.CancelledError):
                await commit_task
            assert commit_started.is_set() is True
            assert db.in_transaction is False
            await assert_wal_connection_policy(db)

    asyncio.run(scenario())

    assert submitted.index(blocker_sql) < submitted.index("COMMIT;")
    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1
    assert_wal_and_write_lock_released(db_path)


def test_running_rollback_cancellation_is_acknowledged_under_wal(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-running-cancelled-rollback.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    rollback_started = threading.Event()
    release_rollback = threading.Event()
    commit_error = sqlite3.OperationalError("primary WAL COMMIT failure")

    def fail_commit(_function, _args, _kwargs):
        raise commit_error

    def delay_rollback(function, args, kwargs):
        rollback_started.set()
        if not release_rollback.wait(timeout=5):
            raise RuntimeError("timed out waiting to release running WAL ROLLBACK")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": delay_rollback}
    )

    async def transaction_task():
        async with app.open_db() as db:
            await assert_wal_connection_policy(db)
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(46001,'0xrunning')"
            )
            try:
                await app._commit_scheduler_transaction(db)
            except sqlite3.OperationalError as error:
                assert error is commit_error
                assert isinstance(
                    error.scheduler_transaction_rollback_cancellation,
                    asyncio.CancelledError,
                )
                assert db.in_transaction is False
                await assert_wal_connection_policy(db)
                raise

    async def scenario():
        transaction = asyncio.create_task(transaction_task())
        assert await asyncio.to_thread(rollback_started.wait, 5)
        transaction.cancel()
        await asyncio.sleep(0)
        release_rollback.set()
        with pytest.raises(
            sqlite3.OperationalError, match="primary WAL COMMIT failure"
        ):
            await transaction

    asyncio.run(scenario())

    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_wal_and_write_lock_released(db_path)


def test_true_queued_rollback_cancellation_is_acknowledged_under_wal(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-queued-cancelled-rollback.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())
    commit_started = threading.Event()
    release_commit_error = threading.Event()
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    rollback_started = threading.Event()
    blocker_sql = "SELECT 1 /* WAL ROLLBACK QUEUE BLOCKER */"
    commit_error = sqlite3.OperationalError("primary queued WAL COMMIT failure")

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail queued WAL COMMIT")
        raise commit_error

    def block_worker(function, args, kwargs):
        blocker_started.set()
        if not release_blocker.wait(timeout=5):
            raise RuntimeError("timed out waiting to release WAL ROLLBACK blocker")
        return function(*args, **kwargs)

    def observe_rollback(function, args, kwargs):
        rollback_started.set()
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {
            "COMMIT;": delayed_commit_error,
            blocker_sql: block_worker,
            "ROLLBACK;": observe_rollback,
        },
    )
    transaction_db = None

    async def transaction_task():
        nonlocal transaction_db
        async with app.open_db() as db:
            transaction_db = db
            await assert_wal_connection_policy(db)
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(47001,'0xqueuedrollback')"
            )
            try:
                await app._commit_scheduler_transaction(db)
            except sqlite3.OperationalError as error:
                assert error is commit_error
                assert isinstance(
                    error.scheduler_transaction_rollback_cancellation,
                    asyncio.CancelledError,
                )
                assert db.in_transaction is False
                await assert_wal_connection_policy(db)
                raise

    async def scenario():
        transaction = asyncio.create_task(transaction_task())
        assert await asyncio.to_thread(commit_started.wait, 5)
        assert transaction_db is not None
        blocker_task = asyncio.create_task(transaction_db.execute(blocker_sql))
        await wait_until_submitted(submitted, blocker_sql)
        release_commit_error.set()
        assert await asyncio.to_thread(blocker_started.wait, 5)
        await wait_until_submitted(submitted, "ROLLBACK;")
        assert rollback_started.is_set() is False

        transaction.cancel()
        await asyncio.sleep(0)
        assert rollback_started.is_set() is False
        release_blocker.set()

        blocker_cursor = await blocker_task
        await blocker_cursor.close()
        with pytest.raises(
            sqlite3.OperationalError, match="primary queued WAL COMMIT failure"
        ):
            await transaction
        assert rollback_started.is_set() is True

    asyncio.run(scenario())

    assert submitted.index("COMMIT;") < submitted.index(blocker_sql)
    assert submitted.index(blocker_sql) < submitted.index("ROLLBACK;")
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_wal_and_write_lock_released(db_path)


def test_wal_pragma_operational_error_stops_startup_fail_closed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-startup-operational-error.sqlite3"
    run_init(db_path, monkeypatch)
    before = schema_and_data_snapshot(db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    wal_error = sqlite3.OperationalError("injected journal_mode WAL failure")

    def fail_wal_pragma(_function, _args, _kwargs):
        raise wal_error

    submitted = install_worker_statement_hooks(
        monkeypatch, {"PRAGMA JOURNAL_MODE = WAL": fail_wal_pragma}
    )
    forbidden_startup_calls = []

    async def forbidden_startup(*_args, **_kwargs):
        forbidden_startup_calls.append(True)

    monkeypatch.setattr(app, "run_startup_checks", lambda: None)
    monkeypatch.setattr(app, "ensure_runtime_directories", lambda: None)
    monkeypatch.setattr(app, "acquire_instance_lock", lambda _path: True)
    monkeypatch.setattr(app, "release_instance_lock", lambda: None)
    monkeypatch.setattr(app, "is_test_mode", lambda: False)
    for name in (
        "load_passwords_at_startup",
        "setup_bot_commands",
        "update_network_codes",
        "recovery_scan_pending_transactions",
        "recover_stale_plan_claims",
        "recover_dca_confirmations",
        "notify_offline_startup_status",
        "dca_scheduler",
        "order_monitor",
    ):
        monkeypatch.setattr(app, name, forbidden_startup)
    monkeypatch.setattr(app.dp, "start_polling", forbidden_startup)

    with pytest.raises(
        sqlite3.OperationalError, match="journal_mode WAL failure"
    ) as raised:
        asyncio.run(app.main())

    assert raised.value is wal_error
    assert submitted.count("PRAGMA JOURNAL_MODE = WAL") == 1
    assert forbidden_startup_calls == []
    assert not any(
        sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
        )
        or "PRAGMA USER_VERSION =" in sql.upper()
        for sql in submitted
    )
    assert schema_and_data_snapshot(db_path) == before
    with sqlite3.connect(db_path, timeout=0.1) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_backup_api_copies_committed_uncheckpointed_wal_data(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "wal-backup-source.sqlite3"
    backup_path = tmp_path / "wal-backup-destination.sqlite3"
    run_init(db_path, monkeypatch)
    asyncio.run(app.ensure_wal_mode())

    async def populate_and_backup():
        async with app.open_db() as db:
            await db.execute("PRAGMA wal_autocheckpoint = 0")
            await db.execute(
                "INSERT INTO wallets(user_id,wallet_address) VALUES(50001,'0xbackup')"
            )
            await db.commit()
            assert Path(f"{db_path}-wal").exists()
            with sqlite3.connect(db_path) as source, sqlite3.connect(
                backup_path
            ) as destination:
                source.backup(destination)

    asyncio.run(populate_and_backup())

    with sqlite3.connect(backup_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute(
            "SELECT user_id,wallet_address FROM wallets"
        ).fetchall() == [(50001, "0xbackup")]
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_production_wal_policy_has_no_forbidden_modes_or_sidecar_deletion():
    source = Path(app.__file__).read_text(encoding="utf-8").lower()
    assert source.count("pragma journal_mode = wal") == 1
    assert "pragma synchronous = normal" not in source
    for mode in ("delete", "truncate", "persist", "memory", "off"):
        assert f"pragma journal_mode = {mode}" not in source
    assert "-wal')" not in source
    assert '"-wal"' not in source
    assert "-shm')" not in source
    assert '"-shm"' not in source
