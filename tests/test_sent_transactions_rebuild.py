import asyncio
import sqlite3
import threading

import pytest

import bot as app


BASE_DEFINITIONS = [
    "id INTEGER PRIMARY KEY",
    "user_id INTEGER NOT NULL",
    "plan_id INTEGER",
    "order_id TEXT NOT NULL",
    "order_token TEXT",
    "network_key TEXT NOT NULL",
    "approve_tx_hash TEXT",
    "transfer_tx_hash TEXT NOT NULL",
    "amount REAL NOT NULL",
    "deposit_address TEXT NOT NULL",
    "state TEXT",
    "error_message TEXT",
    "sent_at INTEGER",
]
INTENT_DEFINITIONS = [
    "approve_tx_nonce INTEGER",
    "approve_raw_tx TEXT",
    "transfer_tx_nonce INTEGER",
    "transfer_raw_tx TEXT",
]
EXACT_AMOUNT_DEFINITIONS = [
    "amount_units TEXT",
    "token_decimals INTEGER",
]


def create_supported_companion_tables(db):
    db.execute(
        "CREATE TABLE dca_plans ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, from_asset TEXT, "
        "amount REAL, interval_hours INTEGER, btc_address TEXT, next_run INTEGER, "
        "active BOOLEAN DEFAULT 1, created_at INTEGER DEFAULT (strftime('%s','now')), "
        "active_order_id TEXT, active_order_token TEXT, active_order_address TEXT, "
        "active_order_amount TEXT, active_order_expires INTEGER, deleted BOOLEAN DEFAULT 0, "
        "execution_state TEXT DEFAULT 'scheduled', last_tx_hash TEXT, "
        "skip_notified INTEGER DEFAULT 0, skip_reason TEXT, missed_count INTEGER DEFAULT 0, "
        "last_missed_at INTEGER, last_execution_attempt_at INTEGER, "
        "confirmation_message_id INTEGER, confirmation_expires_at INTEGER, "
        "confirmation_scheduled_at INTEGER, order_expired_notified INTEGER DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE wallets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, "
        "wallet_address TEXT NOT NULL, created_at INTEGER DEFAULT (strftime('%s','now')))"
    )
    db.execute(
        "CREATE TABLE completed_orders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
        "order_id TEXT NOT NULL UNIQUE, btc_txid TEXT, notified INTEGER DEFAULT 0, "
        "completed_at INTEGER, FOREIGN KEY(user_id) REFERENCES dca_plans(user_id))"
    )


def create_legacy_transactions(
    db_path,
    *,
    include_intent=False,
    include_exact_amount=False,
    extra_definition=None,
    replace_definition=None,
    include_state_fields=True,
    include_order_token=True,
    reverse_definitions=False,
    table_suffix="",
    table_name="sent_transactions",
):
    definitions = list(BASE_DEFINITIONS)
    if not include_state_fields:
        definitions = [
            definition
            for definition in definitions
            if definition.split()[0] not in {"state", "error_message"}
        ]
    if not include_order_token:
        definitions = [
            definition
            for definition in definitions
            if definition.split()[0] != "order_token"
        ]
    if include_intent:
        definitions.extend(INTENT_DEFINITIONS)
    if include_exact_amount:
        definitions.extend(EXACT_AMOUNT_DEFINITIONS)
    if extra_definition:
        definitions.append(extra_definition)
    if replace_definition:
        column_name, replacement = replace_definition
        definitions = [
            replacement if definition.split()[0] == column_name else definition
            for definition in definitions
        ]
    if reverse_definitions:
        definitions.reverse()

    with sqlite3.connect(db_path) as db:
        create_supported_companion_tables(db)
        db.execute(
            f"CREATE TABLE {table_name} ({', '.join(definitions)})"
            f"{table_suffix}"
        )


def insert_legacy_row(db_path, **overrides):
    values = {
        "id": 1,
        "user_id": 10001,
        "plan_id": None,
        "order_id": "legacy-order-1",
        "order_token": "secret-token-1",
        "network_key": "USDT-ARB",
        "approve_tx_hash": "0xapprove-1",
        "transfer_tx_hash": "0xtransfer-1",
        "amount": 16.000002,
        "deposit_address": "0xdeposit-1",
        "state": "tx_pending",
        "error_message": "preserve-this-error",
        "sent_at": 1_700_000_001,
        "approve_tx_nonce": 41,
        "approve_raw_tx": "0x00ff-approve-raw",
        "transfer_tx_nonce": 42,
        "transfer_raw_tx": "0x00ff-transfer-raw",
        "amount_units": "16000002",
        "token_decimals": 6,
    }
    values.update(overrides)
    with sqlite3.connect(db_path) as db:
        table_columns = {
            row[1] for row in db.execute("PRAGMA table_info(sent_transactions)")
        }
        columns = [name for name in values if name in table_columns]
        placeholders = ", ".join("?" for _ in columns)
        db.execute(
            f"INSERT INTO sent_transactions ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [values[name] for name in columns],
        )


def transaction_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(sent_transactions)")]
        rows = db.execute(
            f"SELECT {', '.join(columns)} FROM sent_transactions ORDER BY id"
        ).fetchall()
        transfer_notnull = next(
            row[3]
            for row in db.execute("PRAGMA table_info(sent_transactions)")
            if row[1] == "transfer_tx_hash"
        )
        temp_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sent_transactions_new'"
        ).fetchone()
    return columns, rows, transfer_notnull, temp_table


def destructive_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sent_transactions'"
        ).fetchone()[0]
        xinfo = db.execute("PRAGMA table_xinfo(sent_transactions)").fetchall()
        column_names = [row[1] for row in xinfo]
        quoted_columns = ", ".join(
            '"' + name.replace('"', '""') + '"' for name in column_names
        )
        rows = db.execute(
            f"SELECT {quoted_columns} FROM sent_transactions ORDER BY id"
        ).fetchall()
        indexes = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'sent_transactions' ORDER BY name"
        ).fetchall()
        triggers = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'sent_transactions' ORDER BY name"
        ).fetchall()
        temp_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sent_transactions_new'"
        ).fetchone()
    return table_sql, xinfo, rows, indexes, triggers, temp_table


def run_init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


class FaultingConnectionProxy:
    def __init__(self, connection, predicate):
        self._connection = connection
        self._predicate = predicate

    def execute(self, sql, *args, **kwargs):
        if self._predicate(" ".join(str(sql).split()).upper()):
            raise RuntimeError("injected sent_transactions rebuild failure")
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
        normalized_sql = (
            " ".join(str(args[0]).split()).upper() if args else ""
        )
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


def install_worker_statement_hook(monkeypatch, target_sql, worker_hook):
    return install_worker_statement_hooks(
        monkeypatch, {target_sql: worker_hook}
    )


def assert_write_lock_released(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def assert_schema_fails_before_drop(db_path, monkeypatch):
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    dropped = []

    def observe_drop(sql):
        if sql == "DROP TABLE SENT_TRANSACTIONS":
            dropped.append(sql)
        return False

    install_execute_fault(monkeypatch, observe_drop)
    with pytest.raises(RuntimeError, match="refusing destructive rebuild"):
        asyncio.run(app.init_db())

    assert dropped == []
    assert destructive_snapshot(db_path) == before


def test_legacy_not_null_transfer_without_safety_columns_rebuilds(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-base.sqlite3"
    create_legacy_transactions(db_path)
    insert_legacy_row(db_path)

    run_init(db_path, monkeypatch)

    columns, rows, transfer_notnull, temp_table = transaction_snapshot(db_path)
    assert transfer_notnull == 0
    assert temp_table is None
    assert len(rows) == 1
    assert rows[0][columns.index("id")] == 1
    assert rows[0][columns.index("order_id")] == "legacy-order-1"
    assert rows[0][columns.index("transfer_tx_hash")] == "0xtransfer-1"
    for column_name in (
        "approve_tx_nonce",
        "approve_raw_tx",
        "transfer_tx_nonce",
        "transfer_raw_tx",
        "amount_units",
        "token_decimals",
    ):
        assert rows[0][columns.index(column_name)] is None


def test_rebuild_preserves_nonce_and_raw_transaction_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-intents.sqlite3"
    create_legacy_transactions(db_path, include_intent=True)
    insert_legacy_row(db_path)

    run_init(db_path, monkeypatch)

    columns, rows, transfer_notnull, _ = transaction_snapshot(db_path)
    assert transfer_notnull == 0
    row = dict(zip(columns, rows[0]))
    assert row["approve_tx_hash"] == "0xapprove-1"
    assert row["approve_tx_nonce"] == 41
    assert row["approve_raw_tx"] == "0x00ff-approve-raw"
    assert row["transfer_tx_hash"] == "0xtransfer-1"
    assert row["transfer_tx_nonce"] == 42
    assert row["transfer_raw_tx"] == "0x00ff-transfer-raw"


def test_rebuild_preserves_exact_payment_intent(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-exact.sqlite3"
    create_legacy_transactions(
        db_path,
        include_intent=True,
        include_exact_amount=True,
        include_state_fields=False,
        include_order_token=False,
    )
    insert_legacy_row(db_path)

    run_init(db_path, monkeypatch)

    columns, rows, _, _ = transaction_snapshot(db_path)
    row = dict(zip(columns, rows[0]))
    assert row["amount"] == 16.000002
    assert row["amount_units"] == "16000002"
    assert row["token_decimals"] == 6


def test_rebuild_preserves_null_and_non_null_values_and_all_rows(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-combinations.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    insert_legacy_row(
        db_path,
        id=2,
        order_id="legacy-order-2",
        order_token=None,
        approve_tx_hash=None,
        approve_tx_nonce=None,
        approve_raw_tx=None,
        transfer_tx_hash="0xtransfer-2",
        transfer_tx_nonce=0,
        transfer_raw_tx="",
        amount=7.25,
        amount_units=None,
        token_decimals=None,
        state="blocked",
        error_message=None,
        sent_at=None,
    )
    before_columns, before_rows, _, _ = transaction_snapshot(db_path)

    run_init(db_path, monkeypatch)

    after_columns, after_rows, transfer_notnull, _ = transaction_snapshot(db_path)
    projected_after = [
        tuple(row[after_columns.index(column)] for column in before_columns)
        for row in after_rows
    ]
    assert projected_after == before_rows
    assert [row[after_columns.index("id")] for row in after_rows] == [1, 2]
    assert transfer_notnull == 0


@pytest.mark.parametrize(
    "fault_predicate",
    [
        lambda sql: sql == "DROP TABLE SENT_TRANSACTIONS",
        lambda sql: sql.startswith(
            "ALTER TABLE SENT_TRANSACTIONS_NEW RENAME TO SENT_TRANSACTIONS"
        ),
    ],
    ids=["before-drop", "after-drop-before-rename"],
)
def test_rebuild_fault_rolls_back_to_complete_old_table(
    tmp_path, monkeypatch, fault_predicate
):
    db_path = tmp_path / "legacy-fault.sqlite3"
    create_legacy_transactions(
        db_path,
        include_intent=True,
        include_exact_amount=True,
        include_state_fields=False,
        include_order_token=False,
    )
    insert_legacy_row(db_path)
    before = transaction_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    install_execute_fault(monkeypatch, fault_predicate)

    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(app.init_db())

    assert transaction_snapshot(db_path) == before


@pytest.mark.parametrize(
    ("extra_definition", "include_intent", "replace_definition"),
    [
        ("unknown_recovery_artifact BLOB", False, None),
        (None, True, ("approve_tx_nonce", "approve_tx_nonce TEXT")),
    ],
    ids=["unknown-column", "conflicting-column"],
)
def test_unknown_or_conflicting_schema_fails_before_drop(
    tmp_path, monkeypatch, extra_definition, include_intent, replace_definition
):
    db_path = tmp_path / "legacy-unknown.sqlite3"
    create_legacy_transactions(
        db_path,
        include_intent=include_intent,
        extra_definition=extra_definition,
        replace_definition=replace_definition,
    )
    insert_legacy_row(db_path, unknown_recovery_artifact=b"evidence")
    before = transaction_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    dropped = []

    def observe_drop(sql):
        if sql == "DROP TABLE SENT_TRANSACTIONS":
            dropped.append(sql)
        return False

    install_execute_fault(monkeypatch, observe_drop)
    with pytest.raises(RuntimeError, match="refusing destructive rebuild"):
        asyncio.run(app.init_db())

    assert dropped == []
    assert transaction_snapshot(db_path) == before


@pytest.mark.parametrize(
    "unsupported_semantics",
    [
        "generated",
        "check",
        "collate",
        "strict",
        "without-rowid",
        "named-constraint",
        "sql-comment",
        "quoted-identifier",
    ],
)
def test_unsupported_table_semantics_fail_before_drop(
    tmp_path, monkeypatch, unsupported_semantics
):
    db_path = tmp_path / f"legacy-{unsupported_semantics}.sqlite3"
    kwargs = {}
    if unsupported_semantics == "generated":
        kwargs["extra_definition"] = (
            "generated_evidence TEXT GENERATED ALWAYS AS (transfer_tx_hash) VIRTUAL"
        )
    elif unsupported_semantics == "check":
        kwargs["extra_definition"] = "CHECK (length(order_id) > 0)"
    elif unsupported_semantics == "collate":
        kwargs["replace_definition"] = (
            "order_token",
            "order_token TEXT COLLATE NOCASE",
        )
    elif unsupported_semantics == "strict":
        kwargs["table_suffix"] = " STRICT"
    elif unsupported_semantics == "without-rowid":
        kwargs["table_suffix"] = " WITHOUT ROWID"
    elif unsupported_semantics == "named-constraint":
        kwargs["extra_definition"] = (
            "CONSTRAINT legacy_order_check CHECK (length(order_id) > 0)"
        )
    elif unsupported_semantics == "sql-comment":
        kwargs["replace_definition"] = (
            "order_token",
            "order_token /* legacy comment */ TEXT",
        )
    elif unsupported_semantics == "quoted-identifier":
        kwargs["table_name"] = '"sent_transactions"'
    create_legacy_transactions(db_path, **kwargs)
    insert_legacy_row(db_path)

    assert_schema_fails_before_drop(db_path, monkeypatch)


@pytest.mark.parametrize(
    "index_sql",
    [
        "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
        "ON sent_transactions(order_id COLLATE NOCASE)",
        "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
        "ON sent_transactions(order_id DESC)",
        "CREATE INDEX idx_sent_transactions_network "
        "ON sent_transactions(network_key)",
        "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
        "ON sent_transactions(order_id) WHERE order_id <> ''",
        "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
        "ON sent_transactions(lower(order_id))",
        "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
        "ON sent_transactions(order_id, id)",
    ],
    ids=[
        "nocase",
        "descending",
        "unexpected-index",
        "partial",
        "expression",
        "extra-key-column",
    ],
)
def test_unsupported_index_semantics_fail_before_drop(
    tmp_path, monkeypatch, index_sql
):
    db_path = tmp_path / "legacy-index-semantics.sqlite3"
    create_legacy_transactions(db_path)
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(index_sql)

    assert_schema_fails_before_drop(db_path, monkeypatch)


def test_unexpected_trigger_fails_before_drop(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-trigger.sqlite3"
    create_legacy_transactions(db_path)
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TRIGGER unexpected_sent_transaction_trigger "
            "AFTER INSERT ON sent_transactions BEGIN SELECT 1; END"
        )

    assert_schema_fails_before_drop(db_path, monkeypatch)


@pytest.mark.parametrize(
    "schema_kwargs",
    [
        {"extra_definition": "approve_tx_nonce INTEGER"},
        {"include_exact_amount": True},
        {"extra_definition": "amount_units TEXT"},
        {"extra_definition": "token_decimals INTEGER"},
    ],
    ids=[
        "partial-intent",
        "exact-without-intent",
        "partial-exact-amount-units",
        "partial-exact-token-decimals",
    ],
)
def test_partial_safety_generation_fails_before_drop(
    tmp_path, monkeypatch, schema_kwargs
):
    db_path = tmp_path / "legacy-partial-safety.sqlite3"
    create_legacy_transactions(db_path, **schema_kwargs)
    insert_legacy_row(db_path)

    assert_schema_fails_before_drop(db_path, monkeypatch)


def test_shuffled_physical_column_order_preserves_exact_mapping(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-shuffled.sqlite3"
    create_legacy_transactions(
        db_path,
        include_intent=True,
        include_exact_amount=True,
        reverse_definitions=True,
    )
    insert_legacy_row(db_path)
    before_columns, before_rows, _, _ = transaction_snapshot(db_path)

    run_init(db_path, monkeypatch)

    after_columns, after_rows, transfer_notnull, _ = transaction_snapshot(db_path)
    before_by_name = dict(zip(before_columns, before_rows[0]))
    after_by_name = dict(zip(after_columns, after_rows[0]))
    assert {name: after_by_name[name] for name in before_columns} == before_by_name
    assert transfer_notnull == 0


def test_cancelled_error_after_drop_rolls_back_source_table(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-cancelled.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def cancel_after_drop(sql):
        if sql.startswith(
            "ALTER TABLE SENT_TRANSACTIONS_NEW RENAME TO SENT_TRANSACTIONS"
        ):
            raise asyncio.CancelledError
        return False

    install_execute_fault(monkeypatch, cancel_after_drop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.init_db())

    assert destructive_snapshot(db_path) == before


def test_fault_after_rename_and_index_restore_rolls_back(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-post-rename-fault.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    install_execute_fault(monkeypatch, lambda sql: sql == "COMMIT;")

    with pytest.raises(RuntimeError, match="injected"):
        asyncio.run(app.init_db())

    assert destructive_snapshot(db_path) == before


def test_cancellation_before_commit_is_queued_rolls_back(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-cancel-before-commit.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    submitted = install_worker_statement_hooks(monkeypatch, {})
    real_schema_assertion = app._assert_rebuilt_sent_transactions_schema
    cancellation_requested = False

    async def assert_schema_then_cancel(*args, **kwargs):
        nonlocal cancellation_requested
        await real_schema_assertion(*args, **kwargs)
        if not cancellation_requested:
            cancellation_requested = True
            asyncio.current_task().cancel()

    monkeypatch.setattr(
        app,
        "_assert_rebuilt_sent_transactions_schema",
        assert_schema_then_cancel,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.init_db())

    assert "COMMIT;" not in submitted
    assert "ROLLBACK;" in submitted
    assert destructive_snapshot(db_path) == before
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert_write_lock_released(db_path)

    run_init(db_path, monkeypatch)
    assert transaction_snapshot(db_path)[2] == 0


def test_cancellation_while_queued_commit_finishes_with_known_commit(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-cancel-queued-commit.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    commit_running = threading.Event()
    release_commit = threading.Event()

    def delay_commit(function, args, kwargs):
        commit_running.set()
        if not release_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to release COMMIT")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hook(
        monkeypatch, "COMMIT;", delay_commit
    )

    async def cancel_while_commit_is_running():
        init_task = asyncio.create_task(app.init_db())
        assert await asyncio.to_thread(commit_running.wait, 5)
        init_task.cancel()
        await asyncio.sleep(0)
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await init_task

    asyncio.run(cancel_while_commit_is_running())

    columns, rows, transfer_notnull, temp_table = transaction_snapshot(db_path)
    assert transfer_notnull == 0
    assert temp_table is None
    assert len(rows) == 1
    assert rows[0][columns.index("order_id")] == "legacy-order-1"
    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_sent_transactions_order_id'"
        ).fetchone() is not None
    assert_write_lock_released(db_path)

    run_init(db_path, monkeypatch)
    assert transaction_snapshot(db_path) == (
        columns,
        rows,
        transfer_notnull,
        temp_table,
    )


def test_sqlite_commit_error_rolls_back_and_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-commit-error.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("injected worker COMMIT failure")

    submitted = install_worker_statement_hook(
        monkeypatch, "COMMIT;", fail_commit
    )
    with pytest.raises(sqlite3.OperationalError, match="COMMIT failure"):
        asyncio.run(app.init_db())

    assert "ROLLBACK;" in submitted
    assert destructive_snapshot(db_path) == before
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_cancellation_concurrent_with_worker_commit_error_preserves_error(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-cancel-with-commit-error.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    commit_running = threading.Event()
    release_commit = threading.Event()

    def delayed_commit_error(_function, _args, _kwargs):
        commit_running.set()
        if not release_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail COMMIT")
        raise sqlite3.OperationalError("primary worker COMMIT failure")

    submitted = install_worker_statement_hook(
        monkeypatch, "COMMIT;", delayed_commit_error
    )

    async def cancel_while_failing_commit_runs():
        init_task = asyncio.create_task(app.init_db())
        assert await asyncio.to_thread(commit_running.wait, 5)
        init_task.cancel()
        await asyncio.sleep(0)
        release_commit.set()
        with pytest.raises(
            sqlite3.OperationalError, match="primary worker COMMIT failure"
        ):
            await init_task

    asyncio.run(cancel_while_failing_commit_runs())

    assert "ROLLBACK;" in submitted
    assert destructive_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_cancellation_during_worker_rollback_preserves_commit_error(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-cancel-during-rollback.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    rollback_running = threading.Event()
    release_rollback = threading.Event()

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary worker COMMIT failure")

    def delay_rollback(function, args, kwargs):
        rollback_running.set()
        if not release_rollback.wait(timeout=5):
            raise RuntimeError("timed out waiting to release ROLLBACK")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {"COMMIT;": fail_commit, "ROLLBACK;": delay_rollback},
    )

    async def cancel_while_rollback_runs():
        init_task = asyncio.create_task(app.init_db())
        assert await asyncio.to_thread(rollback_running.wait, 5)
        init_task.cancel()
        await asyncio.sleep(0)
        release_rollback.set()
        with pytest.raises(
            sqlite3.OperationalError, match="primary worker COMMIT failure"
        ) as raised:
            await init_task
        assert isinstance(
            raised.value.sent_transactions_rollback_cancellation,
            asyncio.CancelledError,
        )

    asyncio.run(cancel_while_rollback_runs())

    assert submitted.count("ROLLBACK;") == 1
    assert destructive_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_rollback_error_keeps_primary_commit_error_and_reports_both(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-commit-and-rollback-error.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    before = destructive_snapshot(db_path)
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary worker COMMIT failure")

    def fail_rollback(_function, _args, _kwargs):
        raise sqlite3.OperationalError("secondary worker ROLLBACK failure")

    install_worker_statement_hooks(
        monkeypatch,
        {"COMMIT;": fail_commit, "ROLLBACK;": fail_rollback},
    )

    with pytest.raises(
        sqlite3.OperationalError, match="primary worker COMMIT failure"
    ) as raised:
        asyncio.run(app.init_db())

    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert "secondary worker ROLLBACK failure" in str(raised.value.__cause__)
    assert (
        raised.value.sent_transactions_rollback_error
        is raised.value.__cause__
    )
    assert destructive_snapshot(db_path) == before
    assert_write_lock_released(db_path)


def test_cancellation_after_commit_acknowledgement_keeps_committed_schema(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-cancel-after-commit.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    post_commit_acknowledged = None
    commit_paused = False
    control_statements = []
    submitted = install_worker_statement_hooks(monkeypatch, {})
    original_control = app._execute_sqlite_control_statement_to_completion

    async def pause_after_commit(db, statement, **kwargs):
        nonlocal commit_paused
        control_statements.append(statement)
        result = await original_control(db, statement, **kwargs)
        if statement == "COMMIT;" and not commit_paused:
            commit_paused = True
            post_commit_acknowledged.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(
        app, "_execute_sqlite_control_statement_to_completion", pause_after_commit
    )

    async def cancel_after_commit_acknowledgement():
        nonlocal post_commit_acknowledged
        post_commit_acknowledged = asyncio.Event()
        init_task = asyncio.create_task(app.init_db())
        await asyncio.wait_for(post_commit_acknowledged.wait(), timeout=5)
        init_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await init_task

    asyncio.run(cancel_after_commit_acknowledgement())

    assert control_statements.count("COMMIT;") == 1
    assert "ROLLBACK;" not in control_statements
    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    columns, rows, transfer_notnull, temp_table = transaction_snapshot(db_path)
    assert transfer_notnull == 0
    assert temp_table is None
    assert len(rows) == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_sent_transactions_order_id'"
        ).fetchone() is not None
    assert_write_lock_released(db_path)

    run_init(db_path, monkeypatch)
    assert transaction_snapshot(db_path) == (
        columns,
        rows,
        transfer_notnull,
        temp_table,
    )


def test_rebuild_is_idempotent_on_repeated_init(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-repeated.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    run_init(db_path, monkeypatch)
    after_first = transaction_snapshot(db_path)

    run_init(db_path, monkeypatch)

    assert transaction_snapshot(db_path) == after_first


def test_rebuild_preserves_existing_safe_order_id_index(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-indexed.sqlite3"
    create_legacy_transactions(
        db_path, include_intent=True, include_exact_amount=True
    )
    insert_legacy_row(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )

    run_init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        index = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_sent_transactions_order_id'"
        ).fetchone()
        assert index is not None
        assert "UNIQUE INDEX" in index[0].upper()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sent_transactions ("
                "user_id, order_id, network_key, transfer_tx_hash, amount, "
                "deposit_address) VALUES (?, ?, ?, ?, ?, ?)",
                (10001, "legacy-order-1", "USDT-ARB", "0xother", 1, "0xother"),
            )


def test_current_prod_like_schema_does_not_rebuild(tmp_path, monkeypatch):
    db_path = tmp_path / "current.sqlite3"
    run_init(db_path, monkeypatch)
    before = transaction_snapshot(db_path)

    install_execute_fault(
        monkeypatch,
        lambda sql: sql.startswith("CREATE TABLE SENT_TRANSACTIONS_NEW"),
    )
    asyncio.run(app.init_db())

    assert transaction_snapshot(db_path) == before
