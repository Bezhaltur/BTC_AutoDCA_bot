import asyncio
import sqlite3
import threading

import pytest

import bot as app


INDEX_NAME = "idx_sent_transactions_order_id"


def init_current_db(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def remove_unique_index(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute(f"DROP INDEX {INDEX_NAME}")
        db.execute("PRAGMA user_version = 0")


def insert_transaction(
    db_path,
    *,
    order_id,
    state,
    order_token=None,
    approve_hash=None,
    approve_nonce=None,
    approve_raw=None,
    transfer_hash=None,
    transfer_nonce=None,
    transfer_raw=None,
    amount_units=None,
    token_decimals=None,
    timeout=5.0,
):
    with sqlite3.connect(db_path, timeout=timeout) as db:
        cur = db.execute(
            "INSERT INTO sent_transactions ("
            "user_id, plan_id, order_id, order_token, network_key, "
            "approve_tx_hash, approve_tx_nonce, approve_raw_tx, "
            "transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx, "
            "amount_units, token_decimals, amount, deposit_address, state"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                10001,
                7,
                order_id,
                order_token,
                "USDT-ARB",
                approve_hash,
                approve_nonce,
                approve_raw,
                transfer_hash,
                transfer_nonce,
                transfer_raw,
                amount_units,
                token_decimals,
                25.0,
                "0xdeposit",
                state,
            ),
        )
        return cur.lastrowid


def snapshot_transactions_and_gate(db_path):
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id, order_id, order_token, "
            "approve_tx_hash, approve_tx_nonce, approve_raw_tx, "
            "transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx, "
            "amount_units, token_decimals, state "
            "FROM sent_transactions ORDER BY id"
        ).fetchall()
        gate = db.execute(
            "SELECT active_order_id, active_order_token, active_order_address, "
            "active_order_amount, active_order_expires, execution_state "
            "FROM dca_plans WHERE id = 7"
        ).fetchone()
    return rows, gate


def assert_unique_index_absent(db_path):
    with sqlite3.connect(db_path) as db:
        index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone()
    assert index is None


def seed_active_gate(db_path, order_id="duplicate-order"):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans ("
            "id, user_id, active_order_id, active_order_token, active_order_address, "
            "active_order_amount, active_order_expires, execution_state"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                7,
                10001,
                order_id,
                "active-order-secret",
                "0xactive-deposit",
                "25 USDTARB",
                4_000_000_000,
                "scheduled",
            ),
        )


def install_init_trace(monkeypatch, trace_callback):
    real_connect = app.aiosqlite.connect

    class TracedConnection:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            db = await self.connection.__aenter__()
            await db.set_trace_callback(trace_callback)
            return db

        async def __aexit__(self, exc_type, exc_value, traceback):
            return await self.connection.__aexit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(
        app.aiosqlite,
        "connect",
        lambda *args, **kwargs: TracedConnection(real_connect(*args, **kwargs)),
    )


def start_init_thread():
    errors = []

    def target():
        try:
            asyncio.run(app.init_db())
        except BaseException as exc:  # noqa: BLE001 - captured for thread assertion
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def test_init_db_preserves_older_signed_row_when_newer_duplicate_is_empty(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "signed-then-empty.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    seed_active_gate(db_path)

    old_id = insert_transaction(
        db_path,
        order_id="duplicate-order",
        state="tx_pending",
        order_token="row-token-old",
        approve_hash="0xold-approve-hash",
        approve_nonce=40,
        approve_raw="0xold-approve-raw",
        transfer_hash="0xold-signed-hash",
        transfer_nonce=41,
        transfer_raw="0xold-signed-raw",
        amount_units="25000000",
        token_decimals=6,
    )
    new_id = insert_transaction(
        db_path,
        order_id="duplicate-order",
        state="sending",
        order_token="row-token-new",
    )
    before = snapshot_transactions_and_gate(db_path)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(app.init_db())

    message = str(exc_info.value)
    assert "Duplicate sent_transactions rows prevent safe startup" in message
    assert "order_id='duplicate-order'" in message
    assert "count=2" in message
    assert f"row_ids=[{old_id}, {new_id}]" in message
    for secret in (
        "0xold-approve-hash",
        "0xold-approve-raw",
        "0xold-signed-hash",
        "0xold-signed-raw",
        "row-token-old",
        "row-token-new",
        "active-order-secret",
    ):
        assert secret not in message

    assert snapshot_transactions_and_gate(db_path) == before
    assert_unique_index_absent(db_path)


def test_init_db_preserves_duplicate_rows_with_different_signed_intents(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "different-intents.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    seed_active_gate(db_path)
    insert_transaction(
        db_path,
        order_id="duplicate-order",
        state="tx_pending",
        transfer_hash="0xhash-a",
        transfer_nonce=10,
        transfer_raw="0xraw-a",
    )
    insert_transaction(
        db_path,
        order_id="duplicate-order",
        state="tx_pending",
        transfer_hash="0xhash-b",
        transfer_nonce=11,
        transfer_raw="0xraw-b",
    )
    before = snapshot_transactions_and_gate(db_path)

    with pytest.raises(RuntimeError, match="count=2"):
        asyncio.run(app.init_db())

    assert snapshot_transactions_and_gate(db_path) == before
    assert_unique_index_absent(db_path)


def test_init_db_preserves_duplicate_rows_without_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "no-artifacts.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    seed_active_gate(db_path)
    insert_transaction(db_path, order_id="duplicate-order", state="sending")
    insert_transaction(db_path, order_id="duplicate-order", state="blocked")
    before = snapshot_transactions_and_gate(db_path)

    with pytest.raises(RuntimeError, match="count=2"):
        asyncio.run(app.init_db())

    assert snapshot_transactions_and_gate(db_path) == before
    assert_unique_index_absent(db_path)


def test_init_db_without_duplicates_creates_unique_index_and_is_idempotent(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "no-duplicates.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    insert_transaction(db_path, order_id="order-a", state="scheduled")
    insert_transaction(db_path, order_id="order-b", state="sent")
    before = snapshot_transactions_and_gate(db_path)

    asyncio.run(app.init_db())
    asyncio.run(app.init_db())

    assert snapshot_transactions_and_gate(db_path) == before
    with sqlite3.connect(db_path) as db:
        index = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone()
        assert index is not None
        assert "UNIQUE INDEX" in index[0].upper()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sent_transactions ("
                "user_id, order_id, network_key, amount, deposit_address"
                ") VALUES (?, ?, ?, ?, ?)",
                (10001, "order-a", "USDT-ARB", 30.0, "0xanother"),
            )


def test_index_creation_error_does_not_modify_transaction_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "index-error.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    seed_active_gate(db_path, order_id="order-a")
    insert_transaction(
        db_path,
        order_id="order-a",
        state="tx_pending",
        order_token="row-token",
        transfer_hash="0xhash",
        transfer_nonce=15,
        transfer_raw="0xraw",
    )
    with sqlite3.connect(db_path) as db:
        db.execute(f"CREATE TABLE {INDEX_NAME} (marker INTEGER)")
    before = snapshot_transactions_and_gate(db_path)

    with pytest.raises(RuntimeError, match="Unknown or missing SQLite schema objects"):
        asyncio.run(app.init_db())

    assert snapshot_transactions_and_gate(db_path) == before
    with sqlite3.connect(db_path, timeout=0.2) as db:
        db.execute(
            "INSERT INTO sent_transactions ("
            "user_id, order_id, network_key, amount, deposit_address, state"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (10001, "order-after-rollback", "USDT-ARB", 10.0, "0xdeposit", "scheduled"),
        )


def test_begin_immediate_blocks_duplicate_writer_until_index_exists(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "two-connection-race.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    insert_transaction(db_path, order_id="race-order", state="scheduled")

    index_reached = threading.Event()
    allow_index = threading.Event()
    trace_timeouts = []

    def trace_callback(statement):
        if statement.lstrip().upper().startswith("CREATE UNIQUE INDEX"):
            index_reached.set()
            if not allow_index.wait(timeout=3):
                trace_timeouts.append("index release timed out")

    install_init_trace(monkeypatch, trace_callback)
    init_thread, init_errors = start_init_thread()
    assert index_reached.wait(timeout=3), "init_db did not reach index creation"

    writer_errors = []
    writer_finished = threading.Event()

    def competing_writer():
        try:
            with sqlite3.connect(db_path, timeout=0.2) as db:
                db.execute(
                    "INSERT INTO sent_transactions ("
                    "user_id, order_id, network_key, amount, deposit_address, state"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (10001, "race-order", "USDT-ARB", 25.0, "0xdeposit", "sending"),
                )
        except BaseException as exc:  # noqa: BLE001 - captured for thread assertion
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    writer_thread = threading.Thread(target=competing_writer, daemon=True)
    writer_thread.start()
    assert writer_finished.wait(timeout=3), "competing writer did not finish"
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], sqlite3.OperationalError)
    assert "locked" in str(writer_errors[0]).lower()

    allow_index.set()
    init_thread.join(timeout=3)
    writer_thread.join(timeout=3)
    assert not init_thread.is_alive()
    assert not writer_thread.is_alive()
    assert trace_timeouts == []
    assert init_errors == []

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id, order_id, state FROM sent_transactions WHERE order_id = ?",
            ("race-order",),
        ).fetchall()
        index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone()
    assert len(rows) == 1
    assert index == (1,)


def test_duplicate_diagnostics_use_one_locked_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "diagnostic-snapshot.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    first_id = insert_transaction(
        db_path, order_id="snapshot-order", state="tx_pending"
    )
    second_id = insert_transaction(
        db_path, order_id="snapshot-order", state="sending"
    )

    row_ids_query_reached = threading.Event()
    allow_row_ids_query = threading.Event()
    trace_timeouts = []

    def trace_callback(statement):
        normalized = statement.lstrip().upper()
        if normalized.startswith("SELECT ID FROM SENT_TRANSACTIONS WHERE ORDER_ID"):
            row_ids_query_reached.set()
            if not allow_row_ids_query.wait(timeout=3):
                trace_timeouts.append("row_ids query release timed out")

    install_init_trace(monkeypatch, trace_callback)
    init_thread, init_errors = start_init_thread()
    assert row_ids_query_reached.wait(timeout=3), "init_db did not collect row ids"

    writer_errors = []
    writer_finished = threading.Event()

    def competing_writer():
        try:
            insert_transaction(
                db_path, order_id="snapshot-order", state="blocked", timeout=0.2
            )
        except BaseException as exc:  # noqa: BLE001 - captured for thread assertion
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    writer_thread = threading.Thread(target=competing_writer, daemon=True)
    writer_thread.start()
    assert writer_finished.wait(timeout=7), "competing writer did not finish"
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], sqlite3.OperationalError)
    assert "locked" in str(writer_errors[0]).lower()

    allow_row_ids_query.set()
    init_thread.join(timeout=3)
    writer_thread.join(timeout=3)
    assert not init_thread.is_alive()
    assert not writer_thread.is_alive()
    assert trace_timeouts == []
    assert len(init_errors) == 1
    assert isinstance(init_errors[0], RuntimeError)
    diagnostic = str(init_errors[0])
    assert "order_id='snapshot-order' count=2" in diagnostic
    assert f"row_ids=[{first_id}, {second_id}]" in diagnostic


def test_multiple_duplicate_order_ids_have_deterministic_diagnostics(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "multiple-duplicates.sqlite3"
    init_current_db(db_path, monkeypatch)
    remove_unique_index(db_path)
    z_ids = [
        insert_transaction(db_path, order_id="order-z", state="sending"),
        insert_transaction(db_path, order_id="order-z", state="blocked"),
    ]
    a_ids = [
        insert_transaction(db_path, order_id="order-a", state="tx_pending"),
        insert_transaction(db_path, order_id="order-a", state="sending"),
        insert_transaction(db_path, order_id="order-a", state="blocked"),
    ]

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(app.init_db())

    diagnostic = str(exc_info.value)
    a_conflict = f"order_id='order-a' count=3 row_ids={a_ids}"
    z_conflict = f"order_id='order-z' count=2 row_ids={z_ids}"
    assert a_conflict in diagnostic
    assert z_conflict in diagnostic
    assert diagnostic.index(a_conflict) < diagnostic.index(z_conflict)
