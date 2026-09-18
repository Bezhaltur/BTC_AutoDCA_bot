import ast
import asyncio
import inspect
import sqlite3
import threading
import textwrap
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import bot as app


USER_ID = 10001
NETWORK = "USDT-ARB"
BTC_ADDRESS = "bc1qcreatewindow0000000000000000000000000"
DEPOSIT_ADDRESS = "0x1111111111111111111111111111111111111111"
NOW = 1_700_000_000


class SimulatedProcessCrash(BaseException):
    """Bypass cmd_execute's Exception handler like abrupt process loss."""


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._message_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self._message_id += 1
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=self._message_id)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])


class ExecuteMessage:
    from_user = SimpleNamespace(id=USER_ID)
    text = "/execute"

    def __init__(self, fake_bot):
        self.fake_bot = fake_bot

    async def answer(self, text, **kwargs):
        return await self.fake_bot.send_message(USER_ID, text, **kwargs)


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=777)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def _order_response(sequence):
    return {
        "id": f"external-order-{sequence}",
        "token": f"external-token-{sequence}",
        "from": {
            "code": "USDTARB",
            "amount": "25.000000",
            "address": DEPOSIT_ADDRESS,
        },
        "expires_at": NOW + 3600,
    }


def _plan_row(db_path, plan_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM dca_plans WHERE id = ?", (plan_id,)).fetchone()
        )


def _transaction_rows(db_path, plan_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM sent_transactions WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            ).fetchall()
        ]


def _typed_snapshot(db_path, table, where_sql="", parameters=()):
    with sqlite3.connect(db_path) as db:
        columns = [
            row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        selected = ", ".join(
            [f'"{column}"' for column in columns]
            + [f'typeof("{column}")' for column in columns]
        )
        return {
            "columns": tuple(columns),
            "rows": tuple(
                db.execute(
                    f"SELECT {selected} FROM {table} {where_sql}", parameters
                ).fetchall()
            ),
        }


def _typed_row(snapshot):
    assert len(snapshot["rows"]) == 1
    columns = snapshot["columns"]
    row = snapshot["rows"][0]
    values = row[: len(columns)]
    types = row[len(columns) :]
    return dict(zip(columns, zip(values, types)))


def _assert_complete_typed_row(snapshot, expected):
    actual = _typed_row(snapshot)
    assert tuple(actual) == snapshot["columns"]
    assert set(expected) == set(snapshot["columns"])
    assert actual == expected


@pytest.fixture
def create_window(tmp_path, monkeypatch):
    db_path = str(tmp_path / "create-window.sqlite3")
    fake_bot = FakeBot()
    external_creates = []

    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "bot", fake_bot)
    monkeypatch.setattr(app.time, "time", lambda: NOW)
    app._wallet_passwords.clear()
    app._order_progress_messages.clear()

    def forbidden_network(*args, **kwargs):
        raise AssertionError("characterization test attempted real network access")

    monkeypatch.setattr(app.requests, "get", forbidden_network)
    monkeypatch.setattr(app.requests, "post", forbidden_network)
    monkeypatch.setattr(app, "ff_request", forbidden_network)

    async def fake_limits(network_key):
        assert network_key == NETWORK
        return {"min": 1.0, "max": 500.0}

    monkeypatch.setattr(app, "get_fixedfloat_limits", fake_limits)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        cur = db.execute(
            "INSERT INTO dca_plans ("
            "user_id, from_asset, amount, interval_hours, btc_address, next_run, created_at, "
            "active, deleted, execution_state"
            ") VALUES (?, ?, 25.0, 24, ?, ?, ?, 1, 0, 'scheduled')",
            (USER_ID, NETWORK, BTC_ADDRESS, NOW, NOW),
        )
        plan_id = int(cur.lastrowid)
        db.commit()

    return SimpleNamespace(
        db_path=db_path,
        bot=fake_bot,
        message=ExecuteMessage(fake_bot),
        plan_id=plan_id,
        external_creates=external_creates,
    )


def _install_successful_create(monkeypatch, harness):
    def create_order(network_key, amount, btc_address):
        assert (network_key, amount, btc_address) == (NETWORK, 25.0, BTC_ADDRESS)
        harness.external_creates.append(len(harness.external_creates) + 1)
        return _order_response(len(harness.external_creates))

    monkeypatch.setattr(app, "create_fixedfloat_order", create_order)


def _seed_wallet(harness):
    with sqlite3.connect(harness.db_path) as db:
        db.execute(
            "INSERT INTO wallets (user_id, wallet_address) VALUES (?, ?)",
            (USER_ID, "0x2222222222222222222222222222222222222222"),
        )
        db.commit()
    app._wallet_passwords[USER_ID] = "test-password"


def test_crash_before_creating_order_commit_leaves_only_recoverable_claim(
    create_window, monkeypatch
):
    production_mark_started = app.mark_plan_order_creation_started

    async def crash_before_durable_gate(*args, **kwargs):
        raise SimulatedProcessCrash("before creating_order commit")

    monkeypatch.setattr(app, "mark_plan_order_creation_started", crash_before_durable_gate)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "claiming"
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "mark_plan_order_creation_started", production_mark_started)
    _install_successful_create(monkeypatch, create_window)
    asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == [1]
    assert _plan_row(create_window.db_path, create_window.plan_id)["active_order_id"] == "external-order-1"


def test_successful_external_create_lost_before_persistence_cannot_be_duplicated(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)

    def crash_after_response(*args, **kwargs):
        raise SimulatedProcessCrash("after create response, before local persistence")

    monkeypatch.setattr(app, "extract_order_expires_at", crash_after_response)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "extract_order_expires_at", lambda data: data["expires_at"])
    asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == [1]
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None


def test_creating_order_committed_before_external_call_blocks_restart_retry(
    create_window, monkeypatch
):
    create_attempts = []

    def crash_before_request(*args, **kwargs):
        create_attempts.append("entered")
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        raise SimulatedProcessCrash("before actual request")

    monkeypatch.setattr(app, "create_fixedfloat_order", crash_before_request)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_attempts == ["entered"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_ambiguous_create_error_retains_gate_and_blocks_second_create(
    create_window, monkeypatch
):
    attempts = []

    def ambiguous_create(*args, **kwargs):
        attempts.append("request")
        raise TimeoutError("FixedFloat response timeout")

    monkeypatch.setattr(app, "create_fixedfloat_order", ambiguous_create)
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


def test_transport_error_retains_gate_and_blocks_second_create(
    create_window, monkeypatch
):
    attempts = []

    def transport_error(*args, **kwargs):
        attempts.append("request")
        raise ConnectionError("connection reset after request write")

    monkeypatch.setattr(app, "create_fixedfloat_order", transport_error)
    asyncio.run(app.cmd_execute(create_window.message))

    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


def test_cancellation_during_create_retains_gate_and_blocks_restart_retry(
    create_window, monkeypatch
):
    request_started = threading.Event()
    release_request = threading.Event()
    attempts = []

    def delayed_create(*args, **kwargs):
        attempts.append("request")
        request_started.set()
        assert release_request.wait(timeout=5)
        return _order_response(1)

    monkeypatch.setattr(app, "create_fixedfloat_order", delayed_create)

    async def scenario():
        task = asyncio.create_task(app.cmd_execute(create_window.message))
        await asyncio.to_thread(request_started.wait, 5)
        task.cancel()
        release_request.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


@pytest.mark.parametrize("response", [None, {}, {"token": "token-without-id"}])
def test_invalid_attempted_create_response_retains_gate(
    create_window, monkeypatch, response
):
    attempts = []

    def invalid_create(*args, **kwargs):
        attempts.append("request")
        return response

    monkeypatch.setattr(app, "create_fixedfloat_order", invalid_create)
    asyncio.run(app.cmd_execute(create_window.message))
    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))

    assert attempts == ["request"]
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None


def test_crash_during_active_order_commit_leaves_no_partial_gate(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    production_open_db = app.open_db
    injected = False

    class CommitCrashProxy:
        def __init__(self, db):
            self._db = db
            self.active_gate_update = False

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, parameters=None):
            if "UPDATE dca_plans SET active_order_id" in sql:
                self.active_gate_update = True
            if parameters is None:
                return self._db.execute(sql)
            return self._db.execute(sql, parameters)

        async def commit(self):
            nonlocal injected
            if self.active_gate_update and not injected:
                injected = True
                raise SimulatedProcessCrash("during active-order commit")
            return await self._db.commit()

    @asynccontextmanager
    async def crash_open_db():
        async with production_open_db() as db:
            yield CommitCrashProxy(db)

    monkeypatch.setattr(app, "open_db", crash_open_db)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert injected is True
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None
    assert plan["active_order_address"] is None
    assert plan["active_order_amount"] is None
    assert plan["active_order_expires"] is None


def test_active_order_persistence_operational_error_retains_gate(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    production_open_db = app.open_db
    injected = False

    class PersistenceErrorProxy:
        def __init__(self, db):
            self._db = db

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, parameters=None):
            nonlocal injected
            if "UPDATE dca_plans SET active_order_id" in sql:
                injected = True
                raise sqlite3.OperationalError("injected active-order persistence failure")
            if parameters is None:
                return self._db.execute(sql)
            return self._db.execute(sql, parameters)

    @asynccontextmanager
    async def failing_open_db():
        async with production_open_db() as db:
            yield PersistenceErrorProxy(db)

    monkeypatch.setattr(app, "open_db", failing_open_db)
    asyncio.run(app.cmd_execute(create_window.message))
    monkeypatch.setattr(app, "open_db", production_open_db)

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert injected is True
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None

    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


@pytest.mark.parametrize(
    ("source_state", "expected"),
    [
        ("scheduled", True),
        ("skipped", True),
        ("expired", True),
        ("claiming", False),
        ("creating_order", False),
        ("confirming", False),
        ("awaiting_confirmation", False),
    ],
)
def test_skip_missed_cycle_uses_scheduler_owned_state_cas(
    create_window, source_state, expected
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = ? WHERE id = ?",
            (source_state, create_window.plan_id),
        )
        db.commit()

    applied = asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    )

    assert applied is expected
    row = _plan_row(create_window.db_path, create_window.plan_id)
    assert row["execution_state"] == ("skipped" if expected else source_state)


def test_skip_missed_cycle_rejects_active_order_and_inflight_evidence(create_window):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_id = 'active-order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    assert asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    ) is False
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"

    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_id = NULL WHERE id = ?",
            (create_window.plan_id,),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, 'inflight-order', ?, 25.0, ?, 'pending')",
            (USER_ID, create_window.plan_id, NETWORK, DEPOSIT_ADDRESS),
        )
        db.commit()
    assert asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    ) is False
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"


@pytest.mark.parametrize(
    ("source_state", "expected"),
    [
        ("scheduled", True),
        ("confirming", True),
        ("skipped", True),
        ("expired", True),
        ("claiming", False),
        ("creating_order", False),
        ("awaiting_confirmation", False),
    ],
)
def test_claim_plan_execution_has_explicit_source_state_allowlist(
    create_window, source_state, expected
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = ? WHERE id = ?",
            (source_state, create_window.plan_id),
        )
        db.commit()

    assert asyncio.run(
        app.claim_plan_execution(create_window.plan_id, USER_ID)
    ) is expected
    row = _plan_row(create_window.db_path, create_window.plan_id)
    assert row["execution_state"] == ("claiming" if expected else source_state)


def test_stale_scheduler_skip_cannot_erase_concurrent_creating_order(
    create_window, monkeypatch
):
    create_started = threading.Event()
    release_create = threading.Event()

    def delayed_create(*args, **kwargs):
        create_window.external_creates.append(1)
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        create_started.set()
        assert release_create.wait(timeout=5)
        return _order_response(1)

    monkeypatch.setattr(app, "create_fixedfloat_order", delayed_create)

    async def scenario():
        scheduler_snapshot_read = asyncio.Event()
        allow_scheduler_skip = asyncio.Event()

        async def stale_scheduler():
            async with app.open_db() as db:
                async with db.execute(
                    "SELECT execution_state FROM dca_plans WHERE id = ?",
                    (create_window.plan_id,),
                ) as cur:
                    snapshot = await cur.fetchone()
            assert snapshot == ("scheduled",)
            scheduler_snapshot_read.set()
            await allow_scheduler_skip.wait()
            return await app.skip_missed_dca_cycle(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                scheduled_time=NOW,
                interval_hours=24,
            )

        scheduler_task = asyncio.create_task(stale_scheduler())
        await scheduler_snapshot_read.wait()
        execute_task = asyncio.create_task(app.cmd_execute(create_window.message))
        assert await asyncio.to_thread(create_started.wait, 5)
        allow_scheduler_skip.set()
        assert await scheduler_task is False
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        release_create.set()
        await execute_task

    asyncio.run(scenario())

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "scheduled"
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


def test_valid_response_atomically_exits_gate_and_preserves_payment_pipeline(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    observed = []

    async def successful_payment(**kwargs):
        row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
        plan = _plan_row(create_window.db_path, create_window.plan_id)
        observed.append((plan["execution_state"], plan["active_order_id"], row["state"]))
        kwargs["persist_payment_intent"](25_000_000, 6)
        kwargs["persist_prepared_tx"](
            "transfer", "0x" + "cd" * 32, 21, "0x02f86c" + "22" * 32
        )
        return True, None, "0x" + "cd" * 32, ""

    monkeypatch.setattr(app, "auto_send_usdt", successful_payment)
    asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert observed == [("scheduled", "external-order-1", "transfering")]
    assert plan["execution_state"] == "scheduled"
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert (row["state"], row["amount_units"], row["token_decimals"]) == (
        "sent",
        "25000000",
        6,
    )
    assert row["transfer_raw_tx"] is not None


def test_stale_recovery_releases_claiming_but_never_creating_order(create_window):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'claiming', active_order_id = NULL WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    asyncio.run(app.recover_stale_plan_claims())
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"

    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    asyncio.run(app.recover_stale_plan_claims())
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_manual_and_scheduler_do_not_cross_creating_order_gate(
    create_window, monkeypatch
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    create_calls = []

    def forbidden_create(*args, **kwargs):
        create_calls.append("create")
        raise AssertionError("creating_order reached FixedFloat create")

    async def forbidden_confirmation(*args, **kwargs):
        raise AssertionError("creating_order reached confirmation creation")

    async def stop_after_iteration(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden_create)
    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden_confirmation)
    asyncio.run(app.cmd_execute(create_window.message))
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert create_calls == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_confirmation_finally_cannot_clear_concurrent_creating_order(
    create_window, monkeypatch
):
    scheduled_at = NOW
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'awaiting_confirmation', "
            "confirmation_message_id = 777, confirmation_expires_at = ?, "
            "confirmation_scheduled_at = ? WHERE id = ?",
            (NOW + 600, scheduled_at, create_window.plan_id),
        )
        db.commit()

    async def transition_after_confirmation_cas(*args, **kwargs):
        with sqlite3.connect(create_window.db_path) as db:
            db.execute(
                "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
                (create_window.plan_id,),
            )
            db.commit()

    def forbidden_create(*args, **kwargs):
        raise AssertionError("confirmation crossed creating_order gate")

    monkeypatch.setattr(app, "edit_confirmation_message", transition_after_confirmation_cas)
    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden_create)
    callback = FakeCallback(f"dca_confirm:{create_window.plan_id}:{scheduled_at}")
    asyncio.run(app.cb_dca_confirm(callback))

    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_committed_active_gate_without_payment_row_blocks_all_new_create_paths(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)

    def crash_before_payment_row(amount):
        raise SimulatedProcessCrash("after active gate, before sent_transactions")

    monkeypatch.setattr(app, "require_exact_fixedfloat_amount", crash_before_payment_row)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert plan["execution_state"] == "scheduled"
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    # Startup recovery has no payment row to reconstruct, and stale-claim cleanup
    # cannot clear a persisted active gate.
    asyncio.run(app.recovery_scan_pending_transactions())
    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "require_exact_fixedfloat_amount", app.validate_decimal_amount_text)
    asyncio.run(app.cmd_execute(create_window.message))

    async def pending_status(*args, **kwargs):
        return "pending"

    async def forbidden_confirmation(*args, **kwargs):
        raise AssertionError("scheduler attempted a new execution")

    async def stop_scheduler(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", pending_status)
    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden_confirmation)
    monkeypatch.setattr(app.asyncio, "sleep", stop_scheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    # The monitor also observes the existing gate and never owns order creation.
    monitor_sleeps = 0

    async def run_one_monitor_iteration(*args, **kwargs):
        nonlocal monitor_sleeps
        monitor_sleeps += 1
        if monitor_sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app.asyncio, "sleep", run_one_monitor_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.order_monitor())

    assert create_window.external_creates == [1]
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["active_order_id"] == "external-order-1"


def test_crash_after_payment_row_before_transfer_claim_is_fail_closed_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)

    async def crash_before_transfer_claim(*args, **kwargs):
        raise SimulatedProcessCrash("after sent row, before transfer claim")

    monkeypatch.setattr(app, "claim_auto_send_execution", crash_before_transfer_claim)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "sending"
    assert row["amount_units"] is None
    assert row["transfer_raw_tx"] is None

    asyncio.run(app.recovery_scan_pending_transactions())
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "sending"
    assert "missing persisted exact payment intent" in row["error_message"]
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


def test_crash_after_transfer_claim_before_raw_uses_persisted_exact_intent_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)

    async def crash_before_raw_persistence(**kwargs):
        kwargs["persist_payment_intent"](25_000_000, 6)
        raise SimulatedProcessCrash("after transfer claim, before signed raw persistence")

    monkeypatch.setattr(app, "auto_send_usdt", crash_before_raw_persistence)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert (row["state"], row["amount_units"], row["token_decimals"]) == (
        "transfering",
        "25000000",
        6,
    )
    assert row["transfer_raw_tx"] is None

    resume_calls = []

    async def characterize_resume(**kwargs):
        resume_calls.append(kwargs)
        return "blocked", None, None, "characterized before-raw restart"

    monkeypatch.setattr(app, "resume_transfer_after_approve", characterize_resume)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert len(resume_calls) == 1
    assert resume_calls[0]["amount_units"] == 25_000_000
    assert resume_calls[0]["token_decimals"] == 6
    assert _transaction_rows(create_window.db_path, create_window.plan_id)[0]["state"] == "blocked"


def test_crash_after_raw_persistence_rebroadcasts_identical_bytes_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    tx_hash = "0x" + "ab" * 32
    raw_tx = "0x02f86c0180843b9aca0082520894" + "11" * 20 + "8080c0"

    async def crash_after_raw_persistence(**kwargs):
        kwargs["persist_payment_intent"](25_000_000, 6)
        kwargs["persist_prepared_tx"]("transfer", tx_hash, 17, raw_tx)
        raise SimulatedProcessCrash("after signed raw persistence")

    monkeypatch.setattr(app, "auto_send_usdt", crash_after_raw_persistence)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert (row["transfer_tx_hash"], row["transfer_tx_nonce"], row["transfer_raw_tx"]) == (
        tx_hash,
        17,
        raw_tx,
    )

    async def pending_status(network_key, persisted_hash):
        assert (network_key, persisted_hash) == (NETWORK, tx_hash)
        return "pending"

    rebroadcasts = []

    async def capture_rebroadcast(network_key, persisted_raw, persisted_hash, action):
        rebroadcasts.append((network_key, persisted_raw, persisted_hash, action))
        return persisted_hash

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert rebroadcasts == [(NETWORK, raw_tx, tx_hash, "transfer")]
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "tx_pending"
    assert row["transfer_raw_tx"] == raw_tx
    assert row["transfer_tx_nonce"] == 17


def test_fixedfloat_create_request_has_no_established_idempotency_identifier(monkeypatch):
    captured = []

    def capture_request(method, params):
        captured.append((method, dict(params)))
        return _order_response(1)

    monkeypatch.setattr(app, "ff_request", capture_request)
    result = app.create_fixedfloat_order(NETWORK, 25.0, BTC_ADDRESS)

    assert result["id"] == "external-order-1"
    assert captured == [
        (
            "create",
            {
                "type": "fixed",
                "fromCcy": app.get_fixedfloat_symbol(NETWORK),
                "toCcy": "BTC",
                "direction": "from",
                "amount": 25.0,
                "toAddress": BTC_ADDRESS,
            },
        )
    ]


def test_shared_new_order_core_returns_typed_result_and_events_after_commits(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    send_calls = []
    events = []

    async def successful_payment(**kwargs):
        send_calls.append(kwargs["order_id"])
        kwargs["persist_payment_intent"](25_000_000, 6)
        kwargs["persist_prepared_tx"](
            "transfer", "0x" + "ef" * 32, 31, "0x02f86c" + "33" * 32
        )
        return True, None, "0x" + "ef" * 32, ""

    async def capture_event(event):
        # A real writer probe proves progress never runs while the core owns a
        # write transaction or writer lock.
        async with app.open_db() as probe:
            await probe.execute("BEGIN IMMEDIATE")
            await probe.rollback()
        events.append(event.kind)

    monkeypatch.setattr(app, "auto_send_usdt", successful_payment)
    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            ),
            on_progress=capture_event,
        )
    )

    assert result == app.NewOrderExecutionResult(
        outcome="sent",
        reason_code="auto_send_succeeded",
        plan_id=create_window.plan_id,
        order_id="external-order-1",
        tx_state="sent",
        required_amount="25.000000",
        deposit_address=DEPOSIT_ADDRESS,
        order_expires=NOW + 3600,
        approve_tx_hash=None,
        transfer_tx_hash="0x" + "ef" * 32,
        external_create_attempted=True,
        active_gate_retained=True,
        should_notify=True,
        notification_kind="sent",
        schedule_effect="missed_count_reset",
        error_message="",
    )
    assert events == ["creating_order", "order_identified", "awaiting_payment"]
    assert create_window.external_creates == [1]
    assert send_calls == ["external-order-1"]
    # SQLite's strftime() clock is independent from the frozen Python clock.
    # Pin the sole timestamp so the complete raw-value/type snapshot is exact.
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET sent_at = ? WHERE plan_id = ?",
            (NOW, create_window.plan_id),
        )
        db.commit()
    plan_snapshot = _typed_snapshot(
        create_window.db_path,
        "dca_plans",
        "WHERE id = ? ORDER BY id",
        (create_window.plan_id,),
    )
    tx_snapshot = _typed_snapshot(
        create_window.db_path,
        "sent_transactions",
        "WHERE plan_id = ? ORDER BY id",
        (create_window.plan_id,),
    )
    _assert_complete_typed_row(
        plan_snapshot,
        {
            "id": (create_window.plan_id, "integer"),
            "user_id": (USER_ID, "integer"),
            "from_asset": (NETWORK, "text"),
            "amount": (25.0, "real"),
            "interval_hours": (24, "integer"),
            "btc_address": (BTC_ADDRESS, "text"),
            "next_run": (NOW, "integer"),
            "active": (1, "integer"),
            "created_at": (NOW, "integer"),
            "active_order_id": ("external-order-1", "text"),
            "active_order_token": ("external-token-1", "text"),
            "active_order_address": (DEPOSIT_ADDRESS, "text"),
            "active_order_amount": ("25.000000 USDTARB", "text"),
            "active_order_expires": (NOW + 3600, "integer"),
            "deleted": (0, "integer"),
            "execution_state": ("scheduled", "text"),
            "last_tx_hash": (None, "null"),
            "skip_notified": (0, "integer"),
            "skip_reason": (None, "null"),
            "missed_count": (0, "integer"),
            "last_missed_at": (None, "null"),
            "last_execution_attempt_at": (NOW, "integer"),
            "confirmation_message_id": (None, "null"),
            "confirmation_expires_at": (None, "null"),
            "confirmation_scheduled_at": (None, "null"),
            "order_expired_notified": (0, "integer"),
        },
    )
    _assert_complete_typed_row(
        tx_snapshot,
        {
            "id": (1, "integer"),
            "user_id": (USER_ID, "integer"),
            "plan_id": (create_window.plan_id, "integer"),
            "order_id": ("external-order-1", "text"),
            "order_token": ("external-token-1", "text"),
            "network_key": (NETWORK, "text"),
            "approve_tx_hash": (None, "null"),
            "approve_tx_nonce": (None, "null"),
            "approve_raw_tx": (None, "null"),
            "transfer_tx_hash": ("0x" + "ef" * 32, "text"),
            "transfer_tx_nonce": (31, "integer"),
            "transfer_raw_tx": ("0x02f86c" + "33" * 32, "text"),
            "amount": (25.0, "real"),
            "amount_units": ("25000000", "text"),
            "token_decimals": (6, "integer"),
            "deposit_address": (DEPOSIT_ADDRESS, "text"),
            "state": ("sent", "text"),
            "error_message": (None, "null"),
            "sent_at": (NOW, "integer"),
        },
    )
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert (row["state"], row["amount_units"], row["token_decimals"]) == (
        "sent",
        "25000000",
        6,
    )


def test_progress_callback_failure_cannot_change_durable_money_state(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    callback_calls = []

    async def failing_callback(event):
        callback_calls.append(event.kind)
        raise RuntimeError("telegram unavailable")

    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            ),
            on_progress=failing_callback,
        )
    )

    assert result.outcome == "manual_payment_required"
    assert callback_calls == ["creating_order", "order_identified"]
    assert create_window.external_creates == [1]
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert plan["execution_state"] == "scheduled"


def test_manual_payment_notification_failure_preserves_baseline_schedule_ordering(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET missed_count = 7, next_run = ? WHERE id = ?",
            (NOW + 123, create_window.plan_id),
        )
        db.commit()
    before = _typed_row(
        _typed_snapshot(
            create_window.db_path,
            "dca_plans",
            "WHERE id = ?",
            (create_window.plan_id,),
        )
    )
    notifications = []

    async def fail_manual_notification_once(user_id, order_id, text):
        notifications.append((user_id, order_id, text))
        if len(notifications) == 1:
            raise RuntimeError("telegram notification unavailable")

    monkeypatch.setattr(app, "update_order_progress_message", fail_manual_notification_once)
    asyncio.run(app.cmd_execute(create_window.message))

    expected = dict(before)
    expected.update(
        {
            "active_order_id": ("external-order-1", "text"),
            "active_order_token": ("external-token-1", "text"),
            "active_order_address": (DEPOSIT_ADDRESS, "text"),
            "active_order_amount": ("25.000000 USDTARB", "text"),
            "active_order_expires": (NOW + 3600, "integer"),
            "execution_state": ("scheduled", "text"),
            "last_execution_attempt_at": (NOW, "integer"),
        }
    )
    after = _typed_snapshot(
        create_window.db_path,
        "dca_plans",
        "WHERE id = ?",
        (create_window.plan_id,),
    )
    _assert_complete_typed_row(after, expected)
    assert _typed_row(after)["missed_count"] == (7, "integer")
    assert _typed_row(after)["next_run"] == (NOW + 123, "integer")
    assert _typed_snapshot(
        create_window.db_path,
        "sent_transactions",
        "WHERE plan_id = ?",
        (create_window.plan_id,),
    )["rows"] == ()
    assert len(notifications) == 2


@pytest.mark.parametrize(
    ("send_result", "expected_outcome", "expected_reason"),
    [
        ((False, None, "0x" + "aa" * 32, "TX_PENDING:0xabc"), "tx_pending", "broadcast_pending"),
        ((False, None, None, "connection timeout"), "blocked", "retryable_without_transaction_evidence"),
        ((False, None, None, "Invalid private key format"), "failed", "terminal_auto_send_failure"),
        ((False, None, None, "PERSISTENCE_CONFLICT:claimed"), "tx_pending", "persistence_conflict"),
    ],
)
def test_shared_core_preserves_send_result_mapping(
    create_window, monkeypatch, send_result, expected_outcome, expected_reason
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    send_calls = []

    async def characterized_send(**kwargs):
        send_calls.append(kwargs["order_id"])
        return send_result

    monkeypatch.setattr(app, "auto_send_usdt", characterized_send)
    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            )
        )
    )

    assert (result.outcome, result.reason_code) == (expected_outcome, expected_reason)
    assert create_window.external_creates == [1]
    assert send_calls == ["external-order-1"]
    tx_row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert tx_row["state"] == expected_outcome
    assert tx_row["error_message"] == send_result[3]


def test_shared_core_create_error_retains_gate_and_reports_attempt(create_window, monkeypatch):
    attempts = []

    def timed_out_create(*args, **kwargs):
        attempts.append("create")
        raise TimeoutError("FixedFloat response timeout")

    monkeypatch.setattr(app, "create_fixedfloat_order", timed_out_create)
    request = app.NewOrderExecutionRequest(
        plan_id=create_window.plan_id,
        user_id=USER_ID,
        trigger="manual",
    )
    first = asyncio.run(app.execute_new_order(request))
    second = asyncio.run(app.execute_new_order(request))

    assert first.outcome == "create_error"
    assert first.external_create_attempted is True
    assert first.active_gate_retained is True
    assert first.error_message == "FixedFloat response timeout"
    assert second.outcome == "claim_contended"
    assert attempts == ["create"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_shared_core_reports_active_order_persistence_failure_with_gate_retained(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    production_open_db = app.open_db

    class PersistenceErrorProxy:
        def __init__(self, db):
            self._db = db

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, parameters=None):
            if "UPDATE dca_plans SET active_order_id" in sql:
                raise sqlite3.OperationalError("injected active-order persistence failure")
            if parameters is None:
                return self._db.execute(sql)
            return self._db.execute(sql, parameters)

    @asynccontextmanager
    async def failing_open_db():
        async with production_open_db() as db:
            yield PersistenceErrorProxy(db)

    monkeypatch.setattr(app, "open_db", failing_open_db)
    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            )
        )
    )
    monkeypatch.setattr(app, "open_db", production_open_db)

    assert result.outcome == "active_order_persistence_failed"
    assert result.external_create_attempted is True
    assert result.active_gate_retained is True
    assert "injected active-order persistence failure" in result.error_message
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert create_window.external_creates == [1]

    second = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            )
        )
    )
    assert second.outcome == "claim_contended"
    assert create_window.external_creates == [1]


def test_shared_core_insert_conflict_retains_active_order_and_never_sends(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, order_token, network_key, amount, deposit_address, state) "
            "VALUES (?, NULL, 'external-order-1', 'historical-token', ?, 1.0, ?, 'confirmed')",
            (USER_ID, NETWORK, DEPOSIT_ADDRESS),
        )
        db.commit()

    async def forbidden_send(**kwargs):
        raise AssertionError("insert conflict reached blockchain send")

    monkeypatch.setattr(app, "auto_send_usdt", forbidden_send)
    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            )
        )
    )

    assert result.outcome == "sent_transaction_insert_failed"
    assert result.active_gate_retained is True
    assert "UNIQUE constraint failed" in result.error_message
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert create_window.external_creates == [1]


@pytest.mark.parametrize(
    (
        "case",
        "expected_outcome",
        "expected_reason",
        "create_attempted",
        "gate_retained",
        "should_notify",
        "schedule_effect",
    ),
    [
        ("claim_contended", "claim_contended", "plan_claim_not_acquired", False, False, True, "unchanged"),
        ("create_gate_failed", "create_gate_failed", "durable_create_gate_not_acquired", False, False, True, "unchanged"),
        ("create_ambiguous", "create_error", "fixedfloat_create_error", True, True, True, "unchanged"),
        ("invalid_response", "invalid_response", "invalid_response_type", True, True, True, "unchanged"),
        ("token_missing", "missing_token", "fixedfloat_security_token_unavailable", True, True, True, "unchanged"),
        ("invalid_exact_amount", "invalid_exact_amount", "invalid_authoritative_payment_amount", True, True, True, "unchanged"),
        ("manual_payment", "manual_payment_required", "wallet_or_unlock_unavailable", True, True, True, "missed_count_reset"),
        ("tx_insert_conflict", "sent_transaction_insert_failed", "payment_row_not_persisted", True, True, True, "unchanged"),
        ("transfer_contended", "transfer_claim_contended", "auto_send_claim_not_acquired", True, True, False, "unchanged"),
        ("sent", "sent", "auto_send_succeeded", True, True, True, "missed_count_reset"),
        ("tx_pending", "tx_pending", "broadcast_pending", True, True, True, "unchanged"),
        ("blocked", "blocked", "retryable_without_transaction_evidence", True, True, True, "unchanged"),
        ("failed", "failed", "terminal_auto_send_failure", True, True, True, "unchanged"),
    ],
)
def test_new_order_result_contract_matrix(
    create_window,
    monkeypatch,
    case,
    expected_outcome,
    expected_reason,
    create_attempted,
    gate_retained,
    should_notify,
    schedule_effect,
):
    _install_successful_create(monkeypatch, create_window)

    if case == "claim_contended":
        async def reject_claim(*args, **kwargs):
            return False
        monkeypatch.setattr(app, "claim_plan_execution", reject_claim)
    elif case == "create_gate_failed":
        async def reject_gate(*args, **kwargs):
            return False
        monkeypatch.setattr(app, "mark_plan_order_creation_started", reject_gate)
    elif case == "create_ambiguous":
        def ambiguous_create(*args, **kwargs):
            create_window.external_creates.append(1)
            raise TimeoutError("ambiguous create")
        monkeypatch.setattr(app, "create_fixedfloat_order", ambiguous_create)
    elif case == "invalid_response":
        def invalid_create(*args, **kwargs):
            create_window.external_creates.append(1)
            return None
        monkeypatch.setattr(app, "create_fixedfloat_order", invalid_create)
    elif case in {"token_missing", "invalid_exact_amount"}:
        def incomplete_create(*args, **kwargs):
            create_window.external_creates.append(1)
            response = _order_response(1)
            if case == "token_missing":
                response.pop("token")
            else:
                response["from"]["amount"] = "not-a-decimal"
            return response
        monkeypatch.setattr(app, "create_fixedfloat_order", incomplete_create)
    elif case != "manual_payment":
        _seed_wallet(create_window)

    if case == "tx_insert_conflict":
        with sqlite3.connect(create_window.db_path) as db:
            db.execute(
                "INSERT INTO sent_transactions "
                "(user_id, plan_id, order_id, order_token, network_key, amount, deposit_address, state) "
                "VALUES (?, NULL, 'external-order-1', 'old-token', ?, 1.0, ?, 'confirmed')",
                (USER_ID, NETWORK, DEPOSIT_ADDRESS),
            )
            db.commit()
    elif case == "transfer_contended":
        async def reject_transfer(*args, **kwargs):
            return False
        monkeypatch.setattr(app, "claim_auto_send_execution", reject_transfer)
    elif case in {"sent", "tx_pending", "blocked", "failed"}:
        send_results = {
            "sent": (True, "0xapprove", "0xtransfer", ""),
            "tx_pending": (False, None, "0xtransfer", "TX_PENDING:0xtransfer"),
            "blocked": (False, None, None, "connection timeout"),
            "failed": (False, None, None, "invalid private key"),
        }

        async def characterized_send(**kwargs):
            return send_results[case]

        monkeypatch.setattr(app, "auto_send_usdt", characterized_send)

    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                trigger="manual",
            )
        )
    )

    assert result.outcome == expected_outcome
    assert result.reason_code == expected_reason
    assert result.external_create_attempted is create_attempted
    assert result.active_gate_retained is gate_retained
    assert result.should_notify is should_notify
    assert result.notification_kind == expected_outcome
    assert result.schedule_effect == schedule_effect
    assert len(create_window.external_creates) == int(create_attempted)


def test_scheduler_has_confirmation_only_fresh_order_routing():
    source = inspect.getsource(app.dca_scheduler)
    tree = ast.parse(textwrap.dedent(source))

    confirmation_then_continue = False
    for node in ast.walk(tree):
        for _field, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            for current, following in zip(value, value[1:]):
                if not isinstance(current, ast.Expr) or not isinstance(current.value, ast.Await):
                    continue
                call = current.value.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "create_dca_confirmation_request"
                    and isinstance(following, ast.Continue)
                ):
                    confirmation_then_continue = True

    assert confirmation_then_continue is True
    assert "create_fixedfloat_order" not in source
    assert "execute_new_order" not in source
    assert "claim_plan_execution" not in source
    assert "claim_auto_send_execution" not in source
    assert "await cmd_execute(" in inspect.getsource(app.cb_dca_confirm)
    assert "await execute_new_order(" in inspect.getsource(app.cmd_execute)
