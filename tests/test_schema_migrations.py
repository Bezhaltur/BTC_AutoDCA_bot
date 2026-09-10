import asyncio
import sqlite3

import pytest

import bot as app


DCA_BASE = [
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "user_id INTEGER",
    "from_asset TEXT",
    "amount REAL",
    "interval_hours INTEGER",
    "btc_address TEXT",
    "next_run INTEGER",
    "active BOOLEAN DEFAULT 1",
    "created_at INTEGER DEFAULT (strftime('%s','now'))",
    "active_order_id TEXT",
    "active_order_address TEXT",
    "active_order_amount TEXT",
    "active_order_expires INTEGER",
    "deleted BOOLEAN DEFAULT 0",
    "execution_state TEXT DEFAULT 'scheduled'",
    "last_tx_hash TEXT",
]
DCA_CONFIRMATION = [
    "skip_notified INTEGER DEFAULT 0",
    "skip_reason TEXT",
    "missed_count INTEGER DEFAULT 0",
    "last_missed_at INTEGER",
    "last_execution_attempt_at INTEGER",
    "confirmation_message_id INTEGER",
    "confirmation_expires_at INTEGER",
    "confirmation_scheduled_at INTEGER",
    "order_expired_notified INTEGER DEFAULT 0",
]
DCA_TOKEN = ["active_order_token TEXT"]

SENT_CORE = [
    "id INTEGER PRIMARY KEY",
    "user_id INTEGER NOT NULL",
    "plan_id INTEGER",
    "order_id TEXT NOT NULL",
    "network_key TEXT NOT NULL",
    "approve_tx_hash TEXT",
    "transfer_tx_hash TEXT NOT NULL",
    "amount REAL NOT NULL",
    "deposit_address TEXT NOT NULL",
    "sent_at INTEGER",
]
SENT_STATE = ["state TEXT", "error_message TEXT"]
NULLABLE_SENT_STATE = ["state TEXT DEFAULT 'scheduled'", "error_message TEXT"]
SENT_TOKEN = ["order_token TEXT"]
SENT_INTENT = [
    "approve_tx_nonce INTEGER",
    "approve_raw_tx TEXT",
    "transfer_tx_nonce INTEGER",
    "transfer_raw_tx TEXT",
]
SENT_EXACT = ["amount_units TEXT", "token_decimals INTEGER"]

NULLABLE_GENERATIONS = (
    ("state", NULLABLE_SENT_STATE),
    ("token", NULLABLE_SENT_STATE + SENT_TOKEN),
    ("intent", NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT),
    ("exact", NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT + SENT_EXACT),
)


def run_init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def user_version(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def schema_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall(),
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT * FROM dca_plans ORDER BY id").fetchall()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dca_plans'"
            ).fetchone()
            else None,
            db.execute("SELECT * FROM sent_transactions ORDER BY id").fetchall()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sent_transactions'"
            ).fetchone()
            else None,
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


def create_current_then_unstamp(db_path, monkeypatch):
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 0")


def recreate_dca_generation(db_path, definitions):
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE dca_plans")
        db.execute("CREATE TABLE dca_plans (" + ",".join(definitions) + ")")
        names = {row[1] for row in db.execute("PRAGMA table_info(dca_plans)")}
        values = {
            "id": 7,
            "user_id": 10001,
            "active_order_id": "active-order",
            "active_order_address": "0xdeposit",
            "active_order_amount": "25.000001",
            "active_order_expires": 4_000_000_000,
            "execution_state": "scheduled",
            "active_order_token": "secret-token",
            "confirmation_message_id": 44,
            "confirmation_expires_at": 4_000_000_001,
            "confirmation_scheduled_at": 3_999_999_999,
        }
        selected = [name for name in values if name in names]
        db.execute(
            "INSERT INTO dca_plans (" + ",".join(selected) + ") VALUES (" +
            ",".join("?" for _ in selected) + ")",
            [values[name] for name in selected],
        )
        db.execute("PRAGMA user_version = 0")
        return selected, tuple(values[name] for name in selected)


def recreate_nullable_sent_generation(db_path, extra_definitions):
    definitions = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "user_id INTEGER NOT NULL",
        "plan_id INTEGER",
        "order_id TEXT NOT NULL",
        "network_key TEXT NOT NULL",
        "approve_tx_hash TEXT",
        "transfer_tx_hash TEXT",
        "amount REAL NOT NULL",
        "deposit_address TEXT NOT NULL",
        "sent_at INTEGER DEFAULT (strftime('%s','now'))",
    ] + extra_definitions
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE sent_transactions")
        db.execute(
            "CREATE TABLE sent_transactions (" + ",".join(definitions) +
            ",FOREIGN KEY(plan_id) REFERENCES dca_plans(id))"
        )
        names = {row[1] for row in db.execute("PRAGMA table_info(sent_transactions)")}
        values = {
            "id": 9,
            "user_id": 10001,
            "plan_id": 7,
            "order_id": "nullable-order",
            "network_key": "USDT-ARB",
            "approve_tx_hash": "0xapprove",
            "transfer_tx_hash": "0xtransfer",
            "amount": 25.000001,
            "deposit_address": "0xdeposit",
            "sent_at": 1_700_000_000,
            "state": "tx_pending",
            "error_message": "preserve-error",
            "order_token": "order-token",
            "approve_tx_nonce": 10,
            "approve_raw_tx": "0xapprove-raw",
            "transfer_tx_nonce": 11,
            "transfer_raw_tx": "0xtransfer-raw",
            "amount_units": "25000001",
            "token_decimals": 6,
        }
        selected = [name for name in values if name in names]
        db.execute(
            "INSERT INTO sent_transactions (" + ",".join(selected) + ") VALUES (" +
            ",".join("?" for _ in selected) + ")",
            [values[name] for name in selected],
        )
        db.execute("PRAGMA user_version = 0")
        return selected, tuple(values[name] for name in selected)


def test_fresh_database_becomes_exact_v1(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.sqlite3"
    run_init(db_path, monkeypatch)
    assert user_version(db_path) == app.CURRENT_SCHEMA_VERSION == 1
    before = schema_snapshot(db_path)
    run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before


def test_current_unstamped_schema_is_stamp_only(tmp_path, monkeypatch):
    db_path = tmp_path / "current-unstamped.sqlite3"
    create_current_then_unstamp(db_path, monkeypatch)
    before = schema_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    assert user_version(db_path) == 1
    after = schema_snapshot(db_path)
    assert (after[0], after[2], after[3]) == (before[0], before[2], before[3])
    ddl = [sql.upper() for sql in statements if sql.lstrip().upper().startswith(
        ("CREATE", "ALTER", "DROP")
    )]
    assert ddl == []
    assert any("PRAGMA USER_VERSION = 1" in sql.upper() for sql in statements)


def test_exact_stamped_v1_is_validation_only(tmp_path, monkeypatch):
    db_path = tmp_path / "stamped-v1.sqlite3"
    run_init(db_path, monkeypatch)
    before = schema_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before
    mutations = [sql for sql in statements if sql.lstrip().upper().startswith(
        ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE")
    ) or "PRAGMA USER_VERSION =" in sql.upper()]
    assert mutations == []


@pytest.mark.parametrize(
    "drift",
    [
        "column", "index", "trigger", "constraint", "foreign-key",
        "default", "primary-key", "table-option", "generated",
    ],
)
def test_stamped_v1_schema_drift_fails_without_mutation(
    tmp_path, monkeypatch, drift
):
    db_path = tmp_path / f"v1-drift-{drift}.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        if drift == "column":
            db.execute("ALTER TABLE dca_plans ADD COLUMN unexpected TEXT")
        elif drift == "index":
            db.execute("CREATE INDEX unexpected_idx ON dca_plans(user_id)")
        elif drift == "trigger":
            db.execute(
                "CREATE TRIGGER unexpected_trigger AFTER INSERT ON dca_plans "
                "BEGIN SELECT 1; END"
            )
        elif drift == "constraint":
            db.execute("ALTER TABLE wallets RENAME TO wallets_old")
            db.execute(
                "CREATE TABLE wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL UNIQUE CHECK(user_id > 0), "
                "wallet_address TEXT NOT NULL, "
                "created_at INTEGER DEFAULT (strftime('%s','now')))"
            )
            db.execute(
                "INSERT INTO wallets SELECT * FROM wallets_old"
            )
            db.execute("DROP TABLE wallets_old")
        elif drift == "foreign-key":
            db.execute("DROP TABLE completed_orders")
            db.execute(
                "CREATE TABLE completed_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL, order_id TEXT NOT NULL UNIQUE, "
                "btc_txid TEXT, notified INTEGER DEFAULT 0, completed_at INTEGER, "
                "FOREIGN KEY(user_id) REFERENCES dca_plans(id))"
            )
        elif drift in {"default", "primary-key", "table-option"}:
            db.execute("ALTER TABLE wallets RENAME TO wallets_old")
            id_sql = (
                "id INTEGER" if drift == "primary-key"
                else "id INTEGER PRIMARY KEY AUTOINCREMENT"
            )
            created_default = "0" if drift == "default" else "(strftime('%s','now'))"
            suffix = " STRICT" if drift == "table-option" else ""
            db.execute(
                "CREATE TABLE wallets (" + id_sql + ", user_id INTEGER NOT NULL UNIQUE, "
                "wallet_address TEXT NOT NULL, created_at INTEGER DEFAULT " +
                created_default + ")" + suffix
            )
            db.execute("INSERT INTO wallets SELECT * FROM wallets_old")
            db.execute("DROP TABLE wallets_old")
        else:
            db.execute(
                "ALTER TABLE dca_plans ADD COLUMN generated_drift TEXT "
                "GENERATED ALWAYS AS (user_id) VIRTUAL"
            )
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before


def test_too_new_version_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "too-new.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 2")
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before


SUPPORTED_NULLABLE_DATABASES = (
    ("base-state", DCA_BASE, NULLABLE_SENT_STATE, False),
    ("confirmation-state", DCA_BASE + DCA_CONFIRMATION, NULLABLE_SENT_STATE, False),
    ("confirmation-state-indexed", DCA_BASE + DCA_CONFIRMATION, NULLABLE_SENT_STATE, True),
    ("current-token", DCA_BASE + DCA_CONFIRMATION + DCA_TOKEN, NULLABLE_SENT_STATE + SENT_TOKEN, True),
    ("current-intent", DCA_BASE + DCA_CONFIRMATION + DCA_TOKEN, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT, True),
    ("current-exact-unindexed", DCA_BASE + DCA_CONFIRMATION + DCA_TOKEN, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT + SENT_EXACT, False),
    ("current-exact", DCA_BASE + DCA_CONFIRMATION + DCA_TOKEN, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT + SENT_EXACT, True),
)


@pytest.mark.parametrize(
    "name,dca_generation,sent_generation,has_index",
    SUPPORTED_NULLABLE_DATABASES,
    ids=[case[0] for case in SUPPORTED_NULLABLE_DATABASES],
)
def test_each_supported_nullable_whole_database_fingerprint_migrates(
    tmp_path, monkeypatch, name, dca_generation, sent_generation, has_index
):
    db_path = tmp_path / f"nullable-{name}.sqlite3"
    run_init(db_path, monkeypatch)
    dca_selected, dca_expected = recreate_dca_generation(db_path, dca_generation)
    sent_selected, sent_expected = recreate_nullable_sent_generation(
        db_path, sent_generation
    )
    if has_index:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
                "ON sent_transactions(order_id)"
            )
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        actual_dca = db.execute(
            "SELECT " + ",".join(dca_selected) + " FROM dca_plans WHERE id=7"
        ).fetchone()
        actual_sent = db.execute(
            "SELECT " + ",".join(sent_selected) +
            " FROM sent_transactions WHERE id=9"
        ).fetchone()
        dca_columns = {row[1] for row in db.execute("PRAGMA table_info(dca_plans)")}
        sent_columns = {row[1] for row in db.execute(
            "PRAGMA table_info(sent_transactions)"
        )}
    assert actual_dca == dca_expected
    assert actual_sent == sent_expected
    assert dca_columns == app._CURRENT_DCA_PLAN_COLUMNS
    assert sent_columns == app._CURRENT_SENT_TRANSACTION_COLUMNS
    assert not any("SENT_TRANSACTIONS_NEW" in sql.upper() for sql in statements)
    assert not any("DROP TABLE SENT_TRANSACTIONS" in sql.upper() for sql in statements)
    assert user_version(db_path) == 1


@pytest.mark.parametrize("include_state", [False, True])
@pytest.mark.parametrize("include_token", [False, True])
@pytest.mark.parametrize("safety_generation", [0, 1, 2])
@pytest.mark.parametrize("has_index", [False, True])
def test_each_notnull_sent_column_allowlist_variant_migrates(
    tmp_path, monkeypatch, include_state, include_token, safety_generation, has_index
):
    db_path = tmp_path / "notnull-generation.sqlite3"
    run_init(db_path, monkeypatch)
    definitions = list(SENT_CORE)
    if include_state:
        definitions += SENT_STATE
    if include_token:
        definitions += SENT_TOKEN
    if safety_generation >= 1:
        definitions += SENT_INTENT
    if safety_generation == 2:
        definitions += SENT_EXACT
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE sent_transactions")
        db.execute("CREATE TABLE sent_transactions (" + ",".join(definitions) + ")")
        db.execute(
            "INSERT INTO sent_transactions (id,user_id,order_id,network_key,"
            "approve_tx_hash,transfer_tx_hash,amount,deposit_address,sent_at) "
            "VALUES (1,10001,'legacy-order','USDT-ARB','0xapprove','0xtransfer',"
            "25.000001,'0xdeposit',1700000000)"
        )
        if has_index:
            db.execute(
                "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
                "ON sent_transactions(order_id)"
            )
        db.execute("PRAGMA user_version = 0")
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT approve_tx_hash,transfer_tx_hash,amount_units,token_decimals "
            "FROM sent_transactions WHERE id=1"
        ).fetchone()
        nullable = next(row[3] for row in db.execute(
            "PRAGMA table_info(sent_transactions)"
        ) if row[1] == "transfer_tx_hash")
    assert row == ("0xapprove", "0xtransfer", None, None)
    assert nullable == 0
    assert user_version(db_path) == 1


def test_canonical_notnull_schema_with_fk_defaults_and_index_migrates(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "canonical-notnull.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE sent_transactions")
        db.execute(
            "CREATE TABLE sent_transactions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,"
            "plan_id INTEGER,order_id TEXT NOT NULL,order_token TEXT,"
            "network_key TEXT NOT NULL,approve_tx_hash TEXT,approve_tx_nonce INTEGER,"
            "approve_raw_tx TEXT,transfer_tx_hash TEXT NOT NULL,"
            "transfer_tx_nonce INTEGER,transfer_raw_tx TEXT,amount REAL NOT NULL,"
            "amount_units TEXT,token_decimals INTEGER,deposit_address TEXT NOT NULL,"
            "state TEXT DEFAULT 'scheduled',error_message TEXT,"
            "sent_at INTEGER DEFAULT (strftime('%s','now')),"
            "FOREIGN KEY(plan_id) REFERENCES dca_plans(id))"
        )
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
        db.execute(
            "INSERT INTO sent_transactions VALUES (1,10001,NULL,'canonical-order',"
            "'secret-token','USDT-ARB','0xapprove',10,'0xapprove-raw',"
            "'0xtransfer',11,'0xtransfer-raw',25.000001,'25000001',6,"
            "'0xdeposit','tx_pending','preserve-error',1700000000)"
        )
        db.execute("PRAGMA user_version = 0")
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT order_token,approve_tx_hash,approve_tx_nonce,approve_raw_tx,"
            "transfer_tx_hash,transfer_tx_nonce,transfer_raw_tx,amount_units,"
            "token_decimals,state,error_message FROM sent_transactions"
        ).fetchone()
    assert row == (
        "secret-token", "0xapprove", 10, "0xapprove-raw", "0xtransfer", 11,
        "0xtransfer-raw", "25000001", 6, "tx_pending", "preserve-error",
    )
    assert user_version(db_path) == 1


@pytest.mark.parametrize(
    "semantic_variant",
    ("autoincrement-only", "state-default-only", "sent-default-only", "fk-only", "canonical-without-index"),
)
def test_unproven_notnull_semantic_axis_combinations_fail_closed(
    tmp_path, monkeypatch, semantic_variant
):
    db_path = tmp_path / f"notnull-{semantic_variant}.sqlite3"
    run_init(db_path, monkeypatch)
    id_sql = (
        "id INTEGER PRIMARY KEY AUTOINCREMENT"
        if semantic_variant in {"autoincrement-only", "canonical-without-index"}
        else "id INTEGER PRIMARY KEY"
    )
    state_sql = (
        "state TEXT DEFAULT 'scheduled'"
        if semantic_variant in {"state-default-only", "canonical-without-index"}
        else "state TEXT"
    )
    sent_at_sql = (
        "sent_at INTEGER DEFAULT (strftime('%s','now'))"
        if semantic_variant in {"sent-default-only", "canonical-without-index"}
        else "sent_at INTEGER"
    )
    foreign_key_sql = (
        ",FOREIGN KEY(plan_id) REFERENCES dca_plans(id)"
        if semantic_variant in {"fk-only", "canonical-without-index"}
        else ""
    )
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE sent_transactions")
        db.execute(
            "CREATE TABLE sent_transactions (" + id_sql + ","
            "user_id INTEGER NOT NULL,plan_id INTEGER,order_id TEXT NOT NULL,"
            "order_token TEXT,network_key TEXT NOT NULL,approve_tx_hash TEXT,"
            "approve_tx_nonce INTEGER,approve_raw_tx TEXT,"
            "transfer_tx_hash TEXT NOT NULL,transfer_tx_nonce INTEGER,"
            "transfer_raw_tx TEXT,amount REAL NOT NULL,amount_units TEXT,"
            "token_decimals INTEGER,deposit_address TEXT NOT NULL," + state_sql +
            ",error_message TEXT," + sent_at_sql + foreign_key_sql + ")"
        )
        db.execute("PRAGMA user_version = 0")
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError, match="semantic fingerprint"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before


@pytest.mark.parametrize(
    "extra",
    (
        ["state TEXT DEFAULT 'scheduled'", "error_message TEXT", "order_token TEXT", "approve_tx_nonce INTEGER"],
        ["state TEXT DEFAULT 'scheduled'", "error_message TEXT", "order_token TEXT", "amount_units TEXT", "token_decimals INTEGER"],
        ["state TEXT DEFAULT 'scheduled'", "error_message TEXT", "token_decimals INTEGER"],
    ),
    ids=("partial-intent", "exact-without-intent", "mixed-generation"),
)
def test_partial_nullable_generations_fail_closed(tmp_path, monkeypatch, extra):
    db_path = tmp_path / "partial-nullable.sqlite3"
    run_init(db_path, monkeypatch)
    recreate_nullable_sent_generation(db_path, extra)
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError, match="Unsupported nullable"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before


@pytest.mark.parametrize(
    "name,dca_generation,sent_generation,has_index",
    (
        ("base-exact", DCA_BASE, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT + SENT_EXACT, True),
        ("current-state", DCA_BASE + DCA_CONFIRMATION + DCA_TOKEN, NULLABLE_SENT_STATE, False),
        ("confirmation-intent", DCA_BASE + DCA_CONFIRMATION, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT, True),
        ("base-token", DCA_BASE, NULLABLE_SENT_STATE + SENT_TOKEN, True),
        ("confirmation-exact", DCA_BASE + DCA_CONFIRMATION, NULLABLE_SENT_STATE + SENT_TOKEN + SENT_INTENT + SENT_EXACT, True),
    ),
)
def test_mixed_whole_database_generations_fail_closed(
    tmp_path, monkeypatch, name, dca_generation, sent_generation, has_index
):
    db_path = tmp_path / f"mixed-{name}.sqlite3"
    run_init(db_path, monkeypatch)
    recreate_dca_generation(db_path, dca_generation)
    recreate_nullable_sent_generation(db_path, sent_generation)
    if has_index:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
                "ON sent_transactions(order_id)"
            )
    before = schema_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    with pytest.raises(RuntimeError, match="whole-database"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before
    assert not any(
        sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))
        for sql in statements
    )


def test_unknown_unversioned_table_fails_before_stamp(tmp_path, monkeypatch):
    db_path = tmp_path / "unknown-table.sqlite3"
    create_current_then_unstamp(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE unexpected(marker TEXT)")
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError, match="Unknown or missing"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before
    assert user_version(db_path) == 0


def test_unknown_unversioned_view_fails_before_stamp(tmp_path, monkeypatch):
    db_path = tmp_path / "unknown-view.sqlite3"
    create_current_then_unstamp(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE VIEW unexpected_view AS SELECT id FROM dca_plans")
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError, match="Unknown or missing"):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before
    assert user_version(db_path) == 0


@pytest.mark.parametrize("drift", ["index", "trigger", "constraint"])
def test_unversioned_unknown_schema_semantics_fail_before_stamp(
    tmp_path, monkeypatch, drift
):
    db_path = tmp_path / f"unversioned-{drift}.sqlite3"
    create_current_then_unstamp(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        if drift == "index":
            db.execute("CREATE INDEX unexpected_idx ON dca_plans(user_id)")
        elif drift == "trigger":
            db.execute(
                "CREATE TRIGGER unexpected_trigger AFTER INSERT ON dca_plans "
                "BEGIN SELECT 1; END"
            )
        else:
            db.execute("ALTER TABLE wallets RENAME TO wallets_old")
            db.execute(
                "CREATE TABLE wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL UNIQUE, wallet_address TEXT NOT NULL "
                "CHECK(length(wallet_address) > 0), "
                "created_at INTEGER DEFAULT (strftime('%s','now')))"
            )
            db.execute("INSERT INTO wallets SELECT * FROM wallets_old")
            db.execute("DROP TABLE wallets_old")
    before = schema_snapshot(db_path)
    with pytest.raises(RuntimeError):
        run_init(db_path, monkeypatch)
    assert schema_snapshot(db_path) == before
    assert user_version(db_path) == 0


def test_exact_known_schema_accepts_safe_keyword_case_and_whitespace(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "safe-formatting.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE wallets")
        db.execute(
            "CrEaTe   TaBlE wallets (\n"
            " id InTeGeR   PrImArY KeY AuToInCrEmEnT,\n"
            " user_id INTEGER NOT NULL UNIQUE, wallet_address TEXT NOT NULL,\n"
            " created_at INTEGER DEFAULT ( strftime( '%s' , 'now' ) )\n)"
        )
        db.execute("DROP TABLE sent_transactions")
        db.execute(
            "cReAtE TABLE sent_transactions (\n"
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,\n"
            "plan_id INTEGER, order_id TEXT NOT NULL, order_token TEXT,\n"
            "network_key TEXT NOT NULL, approve_tx_hash TEXT,\n"
            "approve_tx_nonce INTEGER, approve_raw_tx TEXT, transfer_tx_hash TEXT,\n"
            "transfer_tx_nonce INTEGER, transfer_raw_tx TEXT, amount REAL NOT NULL,\n"
            "amount_units TEXT, token_decimals INTEGER, deposit_address TEXT NOT NULL,\n"
            "state TEXT DEFAULT 'scheduled', error_message TEXT,\n"
            "sent_at INTEGER DEFAULT ( strftime( '%s', 'now' ) ),\n"
            "FoReIgN KeY ( plan_id ) ReFeReNcEs dca_plans ( id )\n)"
        )
        db.execute(
            "CrEaTe UnIqUe InDeX idx_sent_transactions_order_id "
            "On sent_transactions ( order_id )"
        )
        db.execute("PRAGMA user_version = 0")
    before = schema_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    after = schema_snapshot(db_path)
    assert user_version(db_path) == 1
    assert (after[0], after[2], after[3]) == (before[0], before[2], before[3])
    assert not any(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for sql in statements)


def test_prod_like_physical_order_is_stamped_without_rebuild(tmp_path, monkeypatch):
    db_path = tmp_path / "prod-like.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            "CREATE TABLE dca_plans ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,from_asset TEXT,"
            "amount REAL,interval_hours INTEGER,btc_address TEXT,next_run INTEGER,"
            "active BOOLEAN DEFAULT 1,created_at INTEGER DEFAULT (strftime('%s','now')),"
            "active_order_id TEXT,active_order_address TEXT,active_order_amount TEXT,"
            "active_order_expires INTEGER,deleted BOOLEAN DEFAULT 0,"
            "execution_state TEXT DEFAULT 'scheduled',last_tx_hash TEXT,"
            "skip_notified INTEGER DEFAULT 0,skip_reason TEXT,"
            "missed_count INTEGER DEFAULT 0,last_missed_at INTEGER,"
            "last_execution_attempt_at INTEGER,order_expired_notified INTEGER DEFAULT 0,"
            "confirmation_message_id INTEGER,confirmation_expires_at INTEGER,"
            "confirmation_scheduled_at INTEGER,active_order_token TEXT);"
            "CREATE TABLE wallets (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER NOT NULL UNIQUE,wallet_address TEXT NOT NULL,"
            "created_at INTEGER DEFAULT (strftime('%s','now')));"
            "CREATE TABLE sent_transactions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,plan_id INTEGER,"
            "order_id TEXT NOT NULL,network_key TEXT NOT NULL,approve_tx_hash TEXT,"
            "transfer_tx_hash TEXT,amount REAL NOT NULL,deposit_address TEXT NOT NULL,"
            "state TEXT DEFAULT 'scheduled',error_message TEXT,"
            "sent_at INTEGER DEFAULT (strftime('%s','now')),order_token TEXT,"
            "approve_tx_nonce INTEGER,approve_raw_tx TEXT,transfer_tx_nonce INTEGER,"
            "transfer_raw_tx TEXT,amount_units TEXT,token_decimals INTEGER,"
            "FOREIGN KEY(plan_id) REFERENCES dca_plans(id));"
            "CREATE TABLE completed_orders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER NOT NULL,order_id TEXT NOT NULL UNIQUE,btc_txid TEXT,"
            "notified INTEGER DEFAULT 0,completed_at INTEGER,"
            "FOREIGN KEY(user_id) REFERENCES dca_plans(user_id));"
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id);"
        )
        db.execute(
            "INSERT INTO sent_transactions (user_id,order_id,network_key,amount,"
            "deposit_address,state) VALUES (10001,'prod-order','USDT-ARB',25,"
            "'0xdeposit','scheduled')"
        )
    before = schema_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    run_init(db_path, monkeypatch)
    after = schema_snapshot(db_path)
    assert user_version(db_path) == 1
    assert (after[0], after[2], after[3]) == (before[0], before[2], before[3])
    assert not any("SENT_TRANSACTIONS_NEW" in sql.upper() for sql in statements)
    assert not any("DROP TABLE SENT_TRANSACTIONS" in sql.upper() for sql in statements)
