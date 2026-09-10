import asyncio
import sqlite3
import threading

import pytest

import bot as app


V1_COMPLETED_ORDERS_SQL = (
    "CREATE TABLE completed_orders ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "user_id INTEGER NOT NULL,"
    "order_id TEXT NOT NULL UNIQUE,"
    "btc_txid TEXT,"
    "notified INTEGER DEFAULT 0,"
    "completed_at INTEGER,"
    "FOREIGN KEY(user_id) REFERENCES dca_plans(user_id))"
)


def run_init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def create_exact_v1(db_path, monkeypatch, *, populated=True, sequence=50):
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE completed_orders")
        db.execute(V1_COMPLETED_ORDERS_SQL)
        if populated:
            db.execute(
                "INSERT INTO dca_plans (id,user_id,deleted) VALUES (1,10001,0)"
            )
            db.execute(
                "INSERT INTO dca_plans (id,user_id,deleted) VALUES (2,10001,1)"
            )
            db.execute(
                "INSERT INTO dca_plans (id,user_id,deleted) VALUES (3,20002,1)"
            )
            db.execute(
                "INSERT INTO completed_orders "
                "(id,user_id,order_id,btc_txid,notified,completed_at) "
                "VALUES (5,10001,'order-with-values',?,1.5,'legacy-time')",
                (sqlite3.Binary(b"\x00\xffbtc"),),
            )
            db.execute(
                "INSERT INTO completed_orders "
                "(id,user_id,order_id,btc_txid,notified,completed_at) "
                "VALUES (9,10001,'order-with-nulls',NULL,NULL,NULL)"
            )
            db.execute(
                "INSERT INTO completed_orders "
                "(id,user_id,order_id,btc_txid,notified,completed_at) "
                "VALUES (12,20002,'order-for-deleted-plan',NULL,0,1700000000)"
            )
        if sequence is None:
            db.execute(
                "DELETE FROM sqlite_sequence WHERE name='completed_orders'"
            )
        else:
            db.execute(
                "UPDATE sqlite_sequence SET seq=? WHERE name='completed_orders'",
                (sequence,),
            )
            if db.execute("SELECT changes()").fetchone()[0] == 0:
                db.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES "
                    "('completed_orders',?)",
                    (sequence,),
                )
        db.execute("PRAGMA user_version=1")


def completed_rows_and_types(db):
    return db.execute(
        "SELECT id,user_id,order_id,btc_txid,notified,completed_at,"
        "typeof(id),typeof(user_id),typeof(order_id),typeof(btc_txid),"
        "typeof(notified),typeof(completed_at) "
        "FROM completed_orders ORDER BY id"
    ).fetchall()


def completed_sequence(db):
    return db.execute(
        "SELECT seq,typeof(seq) FROM sqlite_sequence "
        "WHERE name='completed_orders'"
    ).fetchall()


def completed_index_semantics(db):
    return (
        db.execute("PRAGMA index_list(completed_orders)").fetchall(),
        db.execute(
            "PRAGMA index_xinfo('sqlite_autoindex_completed_orders_1')"
        ).fetchall(),
    )


def database_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "ORDER BY type,name"
            ).fetchall(),
            db.execute("PRAGMA user_version").fetchone()[0],
            completed_rows_and_types(db),
            db.execute("SELECT name,seq,typeof(seq) FROM sqlite_sequence "
                       "ORDER BY name").fetchall(),
        )


def install_trace(monkeypatch, statements):
    real_connect = app.aiosqlite.connect

    class TracedConnection:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            db = await self.connection.__aenter__()
            await db.set_trace_callback(statements.append)
            return db

        async def __aexit__(self, exc_type, exc_value, traceback):
            return await self.connection.__aexit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(
        app.aiosqlite,
        "connect",
        lambda *args, **kwargs: TracedConnection(real_connect(*args, **kwargs)),
    )


class FaultingConnectionProxy:
    def __init__(self, connection, predicate):
        self._connection = connection
        self._predicate = predicate

    def execute(self, sql, *args, **kwargs):
        normalized = " ".join(str(sql).split()).upper()
        if self._predicate(normalized):
            raise RuntimeError("injected completed_orders migration failure")
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class FaultingConnectContext:
    def __init__(self, connect_context, predicate):
        self._connect_context = connect_context
        self._predicate = predicate

    async def __aenter__(self):
        connection = await self._connect_context.__aenter__()
        return FaultingConnectionProxy(connection, self._predicate)

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self._connect_context.__aexit__(exc_type, exc_value, traceback)


def install_execute_fault(monkeypatch, predicate):
    real_connect = app.aiosqlite.connect
    monkeypatch.setattr(
        app.aiosqlite,
        "connect",
        lambda *args, **kwargs: FaultingConnectContext(
            real_connect(*args, **kwargs), predicate
        ),
    )


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


def assert_write_lock_released(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def test_populated_exact_v1_migrates_to_exact_v2_and_preserves_values(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "populated-v1.sqlite3"
    create_exact_v1(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        before_rows = completed_rows_and_types(db)
        before_sequence = completed_sequence(db)

    run_init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_list(completed_orders)").fetchall() == []
        assert completed_rows_and_types(db) == before_rows
        assert completed_sequence(db) == before_sequence == [(50, "integer")]
        indexes = db.execute("PRAGMA index_list(completed_orders)").fetchall()
        assert len(indexes) == 1
        assert indexes[0][1:] == (
            "sqlite_autoindex_completed_orders_1", 1, "u", 0
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO completed_orders(user_id,order_id) "
                "VALUES (10001,'order-with-values')"
            )


def test_v1_relationship_edge_cases_are_preserved_without_new_fk(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "relationship-edges.sqlite3"
    create_exact_v1(db_path, monkeypatch)
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dca_plans WHERE user_id=10001"
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT deleted FROM dca_plans WHERE id=3 AND user_id=20002"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sent_transactions").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM completed_orders WHERE user_id=20002 "
            "AND order_id='order-for-deleted-plan'"
        ).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM completed_orders").fetchone()[0] == 3


def test_fresh_and_current_unstamped_databases_end_as_v2(tmp_path, monkeypatch):
    fresh_path = tmp_path / "fresh-v2.sqlite3"
    run_init(fresh_path, monkeypatch)
    with sqlite3.connect(fresh_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_list(completed_orders)").fetchall() == []

    with sqlite3.connect(fresh_path) as db:
        db.execute("PRAGMA user_version=0")
    before = database_snapshot(fresh_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(fresh_path, monkeypatch)
    after = database_snapshot(fresh_path)
    assert (after[0], after[2], after[3]) == (before[0], before[2], before[3])
    assert after[1] == 2
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for sql in statements
    )


def test_repeated_v2_init_is_mutation_free(tmp_path, monkeypatch):
    db_path = tmp_path / "repeated-v2.sqlite3"
    run_init(db_path, monkeypatch)
    before = database_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before
    assert not any(
        sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
        ) or "PRAGMA USER_VERSION =" in sql.upper()
        for sql in statements
    )


@pytest.mark.parametrize("source_sequence", [73, None], ids=["present", "absent"])
def test_empty_completed_orders_preserves_sequence_state(
    tmp_path, monkeypatch, source_sequence
):
    db_path = tmp_path / f"empty-sequence-{source_sequence}.sqlite3"
    create_exact_v1(
        db_path, monkeypatch, populated=False, sequence=source_sequence
    )
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        expected = [] if source_sequence is None else [(source_sequence, "integer")]
        assert completed_sequence(db) == expected
        cursor = db.execute(
            "INSERT INTO completed_orders(user_id,order_id) VALUES (1,'next-order')"
        )
        assert cursor.lastrowid == (1 if source_sequence is None else source_sequence + 1)


def test_autoincrement_high_water_mark_above_max_id_is_preserved(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "high-water.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        cursor = db.execute(
            "INSERT INTO completed_orders(user_id,order_id) VALUES (10001,'next')"
        )
        assert cursor.lastrowid == 501


def test_autoincrement_sequence_equal_to_max_id_is_preserved(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sequence-equals-max.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=12)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT MAX(id) FROM completed_orders").fetchone()[0] == 12
        assert completed_sequence(db) == [(12, "integer")]

    run_init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        assert completed_sequence(db) == [(12, "integer")]
        cursor = db.execute(
            "INSERT INTO completed_orders(user_id,order_id) "
            "VALUES (10001,'contiguous-next-order')"
        )
        assert cursor.lastrowid == 13
        assert completed_sequence(db) == [(13, "integer")]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO completed_orders(user_id,order_id) "
                "VALUES (10001,'contiguous-next-order')"
            )

    before_repeat = database_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before_repeat
    assert not any(
        sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
        ) or "PRAGMA USER_VERSION =" in sql.upper()
        for sql in statements
    )


@pytest.mark.parametrize("bad_sequence", [1, "not-an-integer"])
def test_malformed_sequence_fails_before_rebuild_ddl(
    tmp_path, monkeypatch, bad_sequence
):
    db_path = tmp_path / "malformed-sequence.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=50)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='completed_orders'",
            (bad_sequence,),
        )
    before = database_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    with pytest.raises(RuntimeError, match="sqlite_sequence"):
        run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for sql in statements
    )


def test_foreign_key_check_for_completed_orders_is_clean_after_migration(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "foreign-key-check.sqlite3"
    create_exact_v1(db_path, monkeypatch)
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute(
            "PRAGMA foreign_key_check(completed_orders)"
        ).fetchall() == []


@pytest.mark.parametrize("drift", ["index", "trigger", "constraint"])
def test_malformed_v1_fails_before_rebuild_ddl(tmp_path, monkeypatch, drift):
    db_path = tmp_path / f"malformed-v1-{drift}.sqlite3"
    create_exact_v1(db_path, monkeypatch, populated=False, sequence=None)
    with sqlite3.connect(db_path) as db:
        if drift == "index":
            db.execute("CREATE INDEX unexpected_completed_idx ON completed_orders(user_id)")
        elif drift == "trigger":
            db.execute(
                "CREATE TRIGGER unexpected_completed_trigger "
                "AFTER INSERT ON completed_orders BEGIN SELECT 1; END"
            )
        else:
            db.execute("DROP TABLE completed_orders")
            db.execute(
                V1_COMPLETED_ORDERS_SQL[:-1] + ",CHECK(length(order_id)>0))"
            )
    before = database_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    with pytest.raises(RuntimeError):
        run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for sql in statements
    )


@pytest.mark.parametrize(
    "fault_predicate",
    [
        lambda sql: sql.startswith("CREATE TABLE COMPLETED_ORDERS_NEW"),
        lambda sql: sql.startswith("INSERT INTO COMPLETED_ORDERS_NEW"),
        lambda sql: sql == "DROP TABLE COMPLETED_ORDERS",
        lambda sql: sql.startswith(
            "ALTER TABLE COMPLETED_ORDERS_NEW RENAME TO COMPLETED_ORDERS"
        ),
        lambda sql: sql.startswith("DELETE FROM SQLITE_SEQUENCE WHERE NAME ="),
        lambda sql: sql == "PRAGMA USER_VERSION = 2",
    ],
    ids=[
        "before-copy",
        "during-copy",
        "after-copy-before-drop",
        "after-drop",
        "after-rename",
        "before-stamp",
    ],
)
def test_v1_migration_faults_restore_exact_source(
    tmp_path, monkeypatch, fault_predicate
):
    db_path = tmp_path / "fault-v1.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    before = database_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    install_execute_fault(monkeypatch, fault_predicate)
    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(app.init_db())
    assert database_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_cancellation_after_drop_restores_exact_v1_source(tmp_path, monkeypatch):
    db_path = tmp_path / "cancel-after-drop.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    before = database_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def cancel_before_rename(sql):
        if sql.startswith(
            "ALTER TABLE COMPLETED_ORDERS_NEW RENAME TO COMPLETED_ORDERS"
        ):
            raise asyncio.CancelledError
        return False

    install_execute_fault(monkeypatch, cancel_before_rename)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.init_db())
    assert database_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_pending_cancellation_before_commit_rolls_back_v1_migration(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cancel-before-commit.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    before = database_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    submitted = install_worker_statement_hooks(monkeypatch, {})
    original_control = app._execute_sqlite_control_statement_to_completion

    async def cancel_before_commit(db, statement, **kwargs):
        if statement == "COMMIT;":
            asyncio.current_task().cancel()
        return await original_control(db, statement, **kwargs)

    monkeypatch.setattr(
        app, "_execute_sqlite_control_statement_to_completion", cancel_before_commit
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.init_db())
    assert "COMMIT;" not in submitted
    assert "ROLLBACK;" in submitted
    assert database_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_cancellation_after_commit_enqueue_acknowledges_committed_v2(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "cancel-running-commit.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    with sqlite3.connect(db_path) as db:
        before_rows = completed_rows_and_types(db)
        before_indexes = completed_index_semantics(db)
        before_sequence = completed_sequence(db)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    commit_running = threading.Event()
    release_commit = threading.Event()

    def delay_commit(function, args, kwargs):
        commit_running.set()
        if not release_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to release COMMIT")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": delay_commit}
    )

    async def scenario():
        init_task = asyncio.create_task(app.init_db())
        assert await asyncio.to_thread(commit_running.wait, 5)
        init_task.cancel()
        await asyncio.sleep(0)
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await init_task

    asyncio.run(scenario())
    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_list(completed_orders)").fetchall() == []
        assert completed_rows_and_types(db) == before_rows
        assert completed_index_semantics(db) == before_indexes
        assert completed_sequence(db) == before_sequence == [(500, "integer")]
    assert_write_lock_released(db_path)

    before_repeat = database_snapshot(db_path)
    repeated_statements = []
    install_trace(monkeypatch, repeated_statements)
    run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before_repeat
    assert not any(
        sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
        ) or "PRAGMA USER_VERSION =" in sql.upper()
        for sql in repeated_statements
    )


def test_commit_error_rolls_back_exact_v1(tmp_path, monkeypatch):
    db_path = tmp_path / "commit-error.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    before = database_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("injected v2 COMMIT failure")

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit}
    )
    with pytest.raises(sqlite3.OperationalError, match="COMMIT failure"):
        asyncio.run(app.init_db())
    assert "ROLLBACK;" in submitted
    assert database_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_rollback_error_keeps_commit_error_primary(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback-error.sqlite3"
    create_exact_v1(db_path, monkeypatch, sequence=500)
    before = database_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary v2 COMMIT failure")

    def fail_rollback(_function, _args, _kwargs):
        raise sqlite3.OperationalError("secondary v2 ROLLBACK failure")

    install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": fail_rollback}
    )
    with pytest.raises(
        sqlite3.OperationalError, match="primary v2 COMMIT failure"
    ) as raised:
        asyncio.run(app.init_db())
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert "secondary v2 ROLLBACK failure" in str(raised.value.__cause__)
    assert database_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_stamped_v2_completed_orders_drift_fails_without_mutation(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "v2-drift.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE completed_orders")
        db.execute(
            "CREATE TABLE completed_orders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,"
            "order_id TEXT NOT NULL UNIQUE,btc_txid TEXT,"
            "notified INTEGER DEFAULT 0,completed_at INTEGER,"
            "CHECK(length(order_id)>0))"
        )
    before = database_snapshot(db_path)
    with pytest.raises(RuntimeError):
        run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before


def test_too_new_version_fails_without_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "too-new.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version=3")
    before = database_snapshot(db_path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        run_init(db_path, monkeypatch)
    assert database_snapshot(db_path) == before
