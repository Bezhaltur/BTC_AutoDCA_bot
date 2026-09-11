import asyncio
import sqlite3

import pytest

import bot as app


V1_COMPLETED_SQL = (
    "CREATE TABLE completed_orders ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,"
    "order_id TEXT NOT NULL UNIQUE,btc_txid TEXT,"
    "notified INTEGER DEFAULT 0,completed_at INTEGER,"
    "FOREIGN KEY(user_id) REFERENCES dca_plans(user_id))"
)

LEGACY_SENT_SQL = (
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


def run_init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def create_legacy_sent_db(db_path, monkeypatch, *, plan_id, seed_parent):
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE completed_orders")
        db.execute(V1_COMPLETED_SQL)
        db.execute("DROP TABLE sent_transactions")
        db.execute(LEGACY_SENT_SQL)
        db.execute(
            "CREATE UNIQUE INDEX idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
        if seed_parent:
            db.execute(
                "INSERT INTO dca_plans(id,user_id) VALUES(?,10001)",
                (plan_id,),
            )
        db.execute(
            "INSERT INTO sent_transactions("
            "id,user_id,plan_id,order_id,order_token,network_key,"
            "approve_tx_hash,approve_tx_nonce,approve_raw_tx,transfer_tx_hash,"
            "transfer_tx_nonce,transfer_raw_tx,amount,amount_units,token_decimals,"
            "deposit_address,state,error_message,sent_at) "
            "VALUES(1,10001,?,'legacy-order','token','USDT-ARB',"
            "'approve',10,'approve-raw','transfer',11,'transfer-raw',25.0,"
            "'25000000',6,'deposit','tx_pending','preserve',1700000000)",
            (plan_id,),
        )
        db.execute("PRAGMA user_version=0")


def db_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall(),
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT * FROM dca_plans ORDER BY id").fetchall(),
            db.execute("SELECT * FROM sent_transactions ORDER BY id").fetchall(),
            db.execute("SELECT * FROM completed_orders ORDER BY id").fetchall(),
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

        async def __aexit__(self, *args):
            return await self.connection.__aexit__(*args)

    monkeypatch.setattr(
        app.aiosqlite,
        "connect",
        lambda *args, **kwargs: TracedConnection(real_connect(*args, **kwargs)),
    )


def test_async_factory_enforces_foreign_keys_before_dml(tmp_path, monkeypatch):
    db_path = tmp_path / "async-foreign-keys.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    async def inspect():
        async with app.open_db() as db:
            assert db.in_transaction is False
            return (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]

    assert asyncio.run(inspect()) == 1


def test_sync_factory_enforces_foreign_keys_before_dml(tmp_path, monkeypatch):
    db_path = tmp_path / "sync-foreign-keys.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    with app.open_db_sync() as db:
        assert db.in_transaction is False
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_async_foreign_keys_readback_mismatch_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "async-mismatch.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    real_connect = app.aiosqlite.connect

    class FalseCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def fetchone(self):
            return (0,)

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.split()).upper() == "PRAGMA FOREIGN_KEYS":
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
    with pytest.raises(RuntimeError, match="foreign_keys mismatch"):
        asyncio.run(_open_async_factory_once())


async def _open_async_factory_once():
    async with app.open_db():
        pass


def test_sync_foreign_keys_readback_mismatch_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "sync-mismatch.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    real_connect = app.sqlite3.connect

    class FalseResult:
        def fetchone(self):
            return (0,)

    class Proxy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.split()).upper() == "PRAGMA FOREIGN_KEYS":
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
    with pytest.raises(RuntimeError, match="foreign_keys mismatch"):
        with app.open_db_sync():
            pass


def test_foreign_key_enforcement_accepts_valid_and_nullable_children(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "valid-children.sqlite3"
    run_init(db_path, monkeypatch)

    async def insert_rows():
        async with app.open_db() as db:
            parent = await db.execute(
                "INSERT INTO dca_plans(user_id,deleted) VALUES(10001,0)"
            )
            await db.execute(
                "INSERT INTO sent_transactions(user_id,plan_id,order_id,network_key,"
                "amount,deposit_address) VALUES(10001,?,'valid','USDT-ARB',1,'deposit')",
                (parent.lastrowid,),
            )
            await db.execute(
                "INSERT INTO sent_transactions(user_id,plan_id,order_id,network_key,"
                "amount,deposit_address) VALUES(10001,NULL,'nullable','USDT-ARB',1,'deposit')"
            )
            await db.commit()

    asyncio.run(insert_rows())
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_key_enforcement_rejects_orphan_child(tmp_path, monkeypatch):
    db_path = tmp_path / "orphan-child.sqlite3"
    run_init(db_path, monkeypatch)

    async def insert_orphan():
        async with app.open_db() as db:
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute(
                    "INSERT INTO sent_transactions(user_id,plan_id,order_id,network_key,"
                    "amount,deposit_address) VALUES(10001,999,'orphan','USDT-ARB',1,'deposit')"
                )
            await db.rollback()

    asyncio.run(insert_orphan())


@pytest.mark.parametrize(
    "statement",
    (
        "DELETE FROM dca_plans WHERE id=7",
        "UPDATE dca_plans SET id=8 WHERE id=7",
    ),
    ids=("hard-delete", "primary-key-update"),
)
def test_referenced_parent_identity_cannot_be_removed(
    tmp_path, monkeypatch, statement
):
    db_path = tmp_path / "referenced-parent.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO dca_plans(id,user_id) VALUES(7,10001)")
        db.execute(
            "INSERT INTO sent_transactions(user_id,plan_id,order_id,network_key,"
            "amount,deposit_address) VALUES(10001,7,'linked','USDT-ARB',1,'deposit')"
        )

    async def mutate_parent():
        async with app.open_db() as db:
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute(statement)
            await db.rollback()

    asyncio.run(mutate_parent())


def test_soft_delete_referenced_parent_remains_valid(tmp_path, monkeypatch):
    db_path = tmp_path / "soft-delete.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO dca_plans(id,user_id) VALUES(7,10001)")
        db.execute(
            "INSERT INTO sent_transactions(user_id,plan_id,order_id,network_key,"
            "amount,deposit_address) VALUES(10001,7,'linked','USDT-ARB',1,'deposit')"
        )

    async def soft_delete():
        async with app.open_db() as db:
            await db.execute("UPDATE dca_plans SET deleted=1,active=0 WHERE id=7")
            await db.commit()

    asyncio.run(soft_delete())
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fresh_init_has_no_foreign_key_violations(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v1_to_v2_migration_succeeds_with_foreign_keys_enabled(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "v1.sqlite3"
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE completed_orders")
        db.execute(V1_COMPLETED_SQL)
        db.execute("INSERT INTO dca_plans(id,user_id) VALUES(7,10001)")
        db.execute(
            "INSERT INTO completed_orders(id,user_id,order_id) VALUES(1,10001,'done')"
        )
        db.execute("PRAGMA user_version=1")
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT id,user_id,order_id FROM completed_orders").fetchall() == [
            (1, 10001, "done")
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_supported_legacy_rebuild_with_valid_parent_succeeds(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-valid.sqlite3"
    create_legacy_sent_db(db_path, monkeypatch, plan_id=7, seed_parent=True)
    run_init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT plan_id,order_id,transfer_raw_tx,amount_units FROM sent_transactions"
        ).fetchone() == (7, "legacy-order", "transfer-raw", "25000000")


def test_legacy_orphan_rebuild_rolls_back_before_drop(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-orphan.sqlite3"
    create_legacy_sent_db(db_path, monkeypatch, plan_id=999, seed_parent=False)
    before = db_snapshot(db_path)
    statements = []
    install_trace(monkeypatch, statements)
    with pytest.raises(sqlite3.IntegrityError):
        run_init(db_path, monkeypatch)
    assert db_snapshot(db_path) == before
    assert not any(
        "DROP TABLE SENT_TRANSACTIONS" in statement.upper()
        for statement in statements
    )
    with sqlite3.connect(db_path, timeout=0.2) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
