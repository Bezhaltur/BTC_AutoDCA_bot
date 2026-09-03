import asyncio
import sqlite3

import pytest

import bot as app


def init_temp_db(tmp_path, monkeypatch, name="tokens.sqlite3"):
    db_path = tmp_path / name
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())
    return db_path


def insert_plan(db_path, *, order_id=None, order_token=None, expires=None):
    with sqlite3.connect(db_path) as db:
        cur = db.execute(
            "INSERT INTO dca_plans ("
            "user_id, from_asset, amount, interval_hours, btc_address, next_run, "
            "active_order_id, active_order_token, active_order_expires, execution_state"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')",
            (
                10001,
                "USDT-ARB",
                25.0,
                24,
                "bc1qtestdestination000000000000000000000",
                1_700_000_000,
                order_id,
                order_token,
                expires,
            ),
        )
        return int(cur.lastrowid)


def test_order_status_and_details_send_id_and_token(monkeypatch):
    calls = []

    async def fake_request(method, params=None):
        calls.append((method, params))
        return {"status": "NEW"}

    monkeypatch.setattr(app, "ff_request_async", fake_request)

    async def scenario():
        status = await app.get_fixedfloat_order_status("order-1", "secret-token")
        details = await app.fetch_fixedfloat_order_details("order-1", "secret-token")
        return status, details

    status, details = asyncio.run(scenario())
    assert status == "new"
    assert details == {"status": "NEW"}
    assert calls == [
        ("order", {"id": "order-1", "token": "secret-token"}),
        ("order", {"id": "order-1", "token": "secret-token"}),
    ]
    assert "secret-token" not in str(
        app.mask_sensitive_data({"id": "order-1", "token": "secret-token"})
    )


def test_fixedfloat_request_logging_masks_order_token(monkeypatch, caplog):
    monkeypatch.setattr(app, "MOCK_FIXEDFLOAT", True)
    with caplog.at_level("INFO", logger=app.logger.name):
        assert app.ff_request(
            "order", {"id": "order-1", "token": "never-log-this-token"}
        ) == {}
    assert "never-log-this-token" not in caplog.text


def test_dca_token_survives_init_db_migration_and_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "dca-migration.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE dca_plans ("
            "id INTEGER PRIMARY KEY, user_id INTEGER, active_order_id TEXT"
            ")"
        )
        db.execute(
            "INSERT INTO dca_plans (id, user_id, active_order_id) VALUES (1, 10001, 'order-1')"
        )

    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_token = 'persisted-token' WHERE id = 1"
        )

    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT active_order_id, active_order_token FROM dca_plans WHERE id = 1"
        ).fetchone()
    assert row == ("order-1", "persisted-token")


def test_sent_transactions_rebuild_preserves_order_token(tmp_path, monkeypatch):
    db_path = tmp_path / "sent-migration.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE sent_transactions ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plan_id INTEGER, "
            "order_id TEXT NOT NULL, order_token TEXT, network_key TEXT NOT NULL, "
            "approve_tx_hash TEXT, transfer_tx_hash TEXT NOT NULL, amount REAL NOT NULL, "
            "deposit_address TEXT NOT NULL, state TEXT, error_message TEXT, sent_at INTEGER"
            ")"
        )
        db.execute(
            "INSERT INTO sent_transactions VALUES ("
            "1, 10001, NULL, 'order-1', 'historical-token', 'USDT-ARB', "
            "NULL, '0xtx', 25.0, '0xdeposit', 'sent', NULL, 1700000000"
            ")"
        )

    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT order_id, order_token, transfer_tx_hash FROM sent_transactions"
        ).fetchone()
        transfer_column = next(
            column for column in db.execute("PRAGMA table_info(sent_transactions)")
            if column[1] == "transfer_tx_hash"
        )
    assert row == ("order-1", "historical-token", "0xtx")
    assert transfer_column[3] == 0


def test_legacy_order_without_token_skips_api_and_remains_active(tmp_path, monkeypatch):
    db_path = init_temp_db(tmp_path, monkeypatch, "legacy.sqlite3")
    plan_id = insert_plan(
        db_path,
        order_id="legacy-order",
        order_token=None,
        expires=1,
    )
    api_calls = []

    async def forbidden_request(*args, **kwargs):
        api_calls.append((args, kwargs))
        raise AssertionError("FixedFloat must not be called without an order token")

    monkeypatch.setattr(app, "ff_request_async", forbidden_request)

    async def scenario():
        status = await app.get_fixedfloat_order_status_with_retry(
            "legacy-order", None, attempts=7, delay_seconds=0
        )
        details = await app.fetch_fixedfloat_order_details("legacy-order", None)
        cleared = await app.notify_and_clear_expired_order(plan_id, "legacy-order")
        return status, details, cleared

    status, details, cleared = asyncio.run(scenario())
    assert status == app.FIXEDFLOAT_TOKEN_UNAVAILABLE_STATUS
    assert details == {"error": app.FIXEDFLOAT_TOKEN_UNAVAILABLE_STATUS}
    assert cleared is False
    assert api_calls == []
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT active_order_id, active_order_token FROM dca_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    assert row == ("legacy-order", None)


@pytest.mark.parametrize("clearer", [app.mark_order_completed, app.mark_order_failed])
def test_clearing_active_order_also_clears_token(tmp_path, monkeypatch, clearer):
    db_path = init_temp_db(tmp_path, monkeypatch, "clear.sqlite3")
    plan_id = insert_plan(
        db_path,
        order_id="order-1",
        order_token="historical-token",
        expires=1_800_000_000,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions ("
            "user_id, plan_id, order_id, order_token, network_key, amount, deposit_address, state"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 'sent')",
            (10001, plan_id, "order-1", "historical-token", "USDT-ARB", 25.0, "0xdeposit"),
        )

    asyncio.run(clearer(plan_id, "order-1", "test"))
    with sqlite3.connect(db_path) as db:
        plan_row = db.execute(
            "SELECT active_order_id, active_order_token FROM dca_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        tx_token = db.execute(
            "SELECT order_token FROM sent_transactions WHERE order_id = 'order-1'"
        ).fetchone()[0]
    assert plan_row == (None, None)
    assert tx_token == "historical-token"


def test_secondary_monitor_selects_only_unnotified_orders_with_token(tmp_path, monkeypatch):
    db_path = init_temp_db(tmp_path, monkeypatch, "secondary-monitor.sqlite3")
    valid_plan = insert_plan(db_path)
    null_plan = insert_plan(db_path)
    empty_plan = insert_plan(db_path)
    notified_plan = insert_plan(db_path)

    with sqlite3.connect(db_path) as db:
        rows = [
            (valid_plan, "valid-order", "valid-token"),
            (null_plan, "null-token-order", None),
            (empty_plan, "empty-token-order", ""),
            (notified_plan, "already-notified-order", "notified-token"),
        ]
        for plan_id, order_id, order_token in rows:
            db.execute(
                "INSERT INTO sent_transactions ("
                "user_id, plan_id, order_id, order_token, network_key, transfer_tx_hash, "
                "amount, deposit_address, state"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sent')",
                (
                    10001,
                    plan_id,
                    order_id,
                    order_token,
                    "USDT-ARB",
                    f"tx-{order_id}",
                    25.0,
                    "0xdeposit",
                ),
            )
        db.execute(
            "INSERT INTO completed_orders (user_id, order_id, notified) VALUES (?, ?, 2)",
            (10001, "already-notified-order"),
        )

    status_calls = []

    async def fake_status(order_id, order_token, *args, **kwargs):
        status_calls.append((order_id, order_token))
        return ""

    sleep_calls = 0

    async def one_iteration_sleep(*args, **kwargs):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", fake_status)
    monkeypatch.setattr(app.asyncio, "sleep", one_iteration_sleep)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await app.order_monitor()

    asyncio.run(scenario())
    assert status_calls == [("valid-order", "valid-token")]
