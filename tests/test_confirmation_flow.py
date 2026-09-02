import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import bot as app


USER_ID = 10001
NETWORK = "USDT-ARB"
BTC_ADDRESS = "bc1qtestdestination000000000000000000000"


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._next_message_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self._next_message_id += 1
        message = SimpleNamespace(message_id=self._next_message_id)
        self.sent.append((chat_id, text, kwargs, message.message_id))
        return message

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])


class FakeCallback:
    def __init__(self, data, message_id=101):
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class Harness:
    def __init__(self, db_path, fake_bot, fixedfloat_calls, claimed_states):
        self.db_path = db_path
        self.bot = fake_bot
        self.fixedfloat_calls = fixedfloat_calls
        self.claimed_states = claimed_states

    async def add_plan(
        self,
        *,
        state="scheduled",
        next_run=1_700_000_000,
        interval_hours=24,
        message_id=None,
        expires_at=None,
        scheduled_at=None,
        missed_count=0,
        active_order_id=None,
    ):
        async with app.aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO dca_plans ("
                "user_id, from_asset, amount, interval_hours, btc_address, next_run, "
                "active, deleted, execution_state, confirmation_message_id, "
                "confirmation_expires_at, confirmation_scheduled_at, missed_count, active_order_id"
                ") VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
                (
                    USER_ID,
                    NETWORK,
                    25.0,
                    interval_hours,
                    BTC_ADDRESS,
                    next_run,
                    state,
                    message_id,
                    expires_at,
                    scheduled_at,
                    missed_count,
                    active_order_id,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    def plan(self, plan_id):
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM dca_plans WHERE id = ?", (plan_id,)).fetchone())


@pytest.fixture
def harness(tmp_path, monkeypatch):
    db_path = str(tmp_path / "confirmation-flow.sqlite3")
    fake_bot = FakeBot()
    fixedfloat_calls = []
    claimed_states = []
    call_lock = threading.Lock()

    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "bot", fake_bot)
    app._wallet_passwords.clear()
    app._order_progress_messages.clear()

    def forbidden_http(*args, **kwargs):
        raise AssertionError("A test attempted a real HTTP request")

    monkeypatch.setattr(app.requests, "get", forbidden_http)
    monkeypatch.setattr(app.requests, "post", forbidden_http)
    monkeypatch.setattr(app, "load_password_from_keyring", forbidden_http)
    monkeypatch.setattr(app, "save_password_to_keyring", forbidden_http)
    monkeypatch.setattr(app, "delete_password_from_keyring", forbidden_http)

    async def forbidden_transaction(*args, **kwargs):
        raise AssertionError("A test attempted to send an EVM/USDT transaction")

    monkeypatch.setattr(app, "auto_send_usdt", forbidden_transaction)

    async def fake_limits(network_key):
        assert network_key == NETWORK
        return {"min": 1.0, "max": 500.0}

    def fake_create_order(network_key, amount, btc_address):
        with call_lock:
            fixedfloat_calls.append((network_key, amount, btc_address))
            with sqlite3.connect(db_path) as db:
                state = db.execute(
                    "SELECT execution_state FROM dca_plans WHERE user_id = ?",
                    (USER_ID,),
                ).fetchone()[0]
            claimed_states.append(state)
            sequence = len(fixedfloat_calls)
        return {
            "id": f"fake-order-{sequence}",
            "from": {
                "code": "USDTARB",
                "amount": str(amount),
                "address": "0x1111111111111111111111111111111111111111",
            },
            "time": {"left": 900},
        }

    monkeypatch.setattr(app, "get_fixedfloat_limits", fake_limits)
    monkeypatch.setattr(app, "create_fixedfloat_order", fake_create_order)
    monkeypatch.setattr(app, "ff_request", forbidden_http)

    asyncio.run(app.init_db())
    return Harness(db_path, fake_bot, fixedfloat_calls, claimed_states)


def test_scheduler_creates_one_persisted_confirmation(harness, monkeypatch):
    now = 1_700_000_100
    scheduled_at = 1_700_000_000
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = asyncio.run(harness.add_plan(next_run=scheduled_at))

    async def scenario():
        first = await app.create_dca_confirmation_request(
            plan_id=plan_id,
            user_id=USER_ID,
            network_key=NETWORK,
            amount=25.0,
            interval_hours=24,
            scheduled_at=scheduled_at,
            plan_number=1,
        )
        second = await app.create_dca_confirmation_request(
            plan_id=plan_id,
            user_id=USER_ID,
            network_key=NETWORK,
            amount=25.0,
            interval_hours=24,
            scheduled_at=scheduled_at,
            plan_number=1,
        )
        return first, second

    assert asyncio.run(scenario()) == (True, False)
    row = harness.plan(plan_id)
    assert row["execution_state"] == "awaiting_confirmation"
    assert row["confirmation_expires_at"] == now + app.DCA_CONFIRMATION_TIMEOUT_SECONDS
    assert row["confirmation_scheduled_at"] == scheduled_at
    assert row["confirmation_message_id"] == 101
    assert len(harness.bot.sent) == 1


def test_user_skip_is_idempotent_and_advances_from_original_schedule(harness, monkeypatch):
    now = 1_700_010_000
    scheduled_at = 1_700_000_000
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=77,
            expires_at=now + 300,
            scheduled_at=scheduled_at,
        )
    )
    callback = FakeCallback(f"dca_skip:{plan_id}:{scheduled_at}", message_id=77)

    async def scenario():
        await app.cb_dca_skip(callback)
        await app.cb_dca_skip(callback)

    asyncio.run(scenario())
    row = harness.plan(plan_id)
    expected = app.calculate_next_run_preserving_schedule(scheduled_at, 24, now)
    assert row["execution_state"] == "scheduled"
    assert row["next_run"] == expected
    assert row["confirmation_message_id"] is None
    assert row["confirmation_expires_at"] is None
    assert row["confirmation_scheduled_at"] is None
    assert row["missed_count"] == 1
    assert [answer[0] for answer in callback.answers] == [
        "Покупка пропущена.",
        "Этот запрос уже обработан.",
    ]


def test_timeout_is_idempotent_and_never_creates_order(harness):
    now = 1_700_010_000
    scheduled_at = 1_700_000_000
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=88,
            expires_at=now,
            scheduled_at=scheduled_at,
            missed_count=2,
        )
    )

    async def scenario():
        return (
            await app.expire_dca_confirmation(plan_id, now),
            await app.expire_dca_confirmation(plan_id, now),
        )

    assert asyncio.run(scenario()) == (True, False)
    row = harness.plan(plan_id)
    assert row["execution_state"] == "scheduled"
    assert row["next_run"] == app.calculate_next_run_preserving_schedule(scheduled_at, 24, now)
    assert row["confirmation_message_id"] is None
    assert row["confirmation_expires_at"] is None
    assert row["confirmation_scheduled_at"] is None
    assert row["missed_count"] == 3
    assert harness.fixedfloat_calls == []


def test_double_confirm_claims_once_and_creates_one_fake_order(harness, monkeypatch):
    now = 1_700_000_100
    scheduled_at = 1_700_000_000
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=99,
            expires_at=now + 300,
            scheduled_at=scheduled_at,
        )
    )
    first = FakeCallback(f"dca_confirm:{plan_id}:{scheduled_at}", message_id=99)
    second = FakeCallback(f"dca_confirm:{plan_id}:{scheduled_at}", message_id=99)

    async def scenario():
        await asyncio.gather(app.cb_dca_confirm(first), app.cb_dca_confirm(second))

    asyncio.run(scenario())

    row = harness.plan(plan_id)
    assert len(harness.fixedfloat_calls) == 1
    assert harness.claimed_states == ["claiming"]
    assert row["active_order_id"] == "fake-order-1"
    assert row["execution_state"] == "scheduled"
    all_answers = [answer[0] for callback in (first, second) for answer in callback.answers]
    assert sorted(all_answers) == sorted(["Запускаю покупку.", "Этот запрос уже обработан."])


def test_restart_recovery_expires_only_due_and_resets_stale_claims(harness, monkeypatch):
    now = 1_700_010_000
    monkeypatch.setattr(app.time, "time", lambda: now)

    async def scenario():
        expired_id = await harness.add_plan(
            state="awaiting_confirmation",
            next_run=now - 100,
            expires_at=now - 1,
            scheduled_at=now - 100,
        )
        pending_id = await harness.add_plan(
            state="awaiting_confirmation",
            next_run=now - 100,
            message_id=90,
            expires_at=now + 300,
            scheduled_at=now - 100,
        )
        confirming_id = await harness.add_plan(state="confirming", next_run=now - 100)
        claiming_id = await harness.add_plan(state="claiming", next_run=now - 100)
        await app.recover_stale_plan_claims()
        await app.recover_dca_confirmations()
        return expired_id, pending_id, confirming_id, claiming_id

    expired_id, pending_id, confirming_id, claiming_id = asyncio.run(scenario())
    assert harness.plan(expired_id)["execution_state"] == "scheduled"
    assert harness.plan(expired_id)["missed_count"] == 1
    assert harness.plan(pending_id)["execution_state"] == "awaiting_confirmation"
    assert harness.plan(pending_id)["active_order_id"] is None
    assert harness.plan(confirming_id)["execution_state"] == "scheduled"
    assert harness.plan(claiming_id)["execution_state"] == "scheduled"
    assert harness.fixedfloat_calls == []


def test_confirm_timeout_race_has_one_winner(harness, monkeypatch):
    confirm_now = 1_700_000_100
    scheduled_at = 1_700_000_000
    expires_at = confirm_now + 10
    monkeypatch.setattr(app.time, "time", lambda: confirm_now)
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=111,
            expires_at=expires_at,
            scheduled_at=scheduled_at,
        )
    )
    callback = FakeCallback(f"dca_confirm:{plan_id}:{scheduled_at}", message_id=111)

    async def scenario():
        _, timeout_won = await asyncio.gather(
            app.cb_dca_confirm(callback),
            app.expire_dca_confirmation(plan_id, expires_at),
        )
        return timeout_won

    timeout_won = asyncio.run(scenario())
    row = harness.plan(plan_id)
    assert row["execution_state"] == "scheduled"
    assert len(harness.fixedfloat_calls) <= 1
    if timeout_won:
        assert row["active_order_id"] is None
        assert row["missed_count"] == 1
        assert harness.fixedfloat_calls == []
    else:
        assert row["active_order_id"] == "fake-order-1"
        assert row["missed_count"] == 0
        assert len(harness.fixedfloat_calls) == 1


def test_scheduler_gates_transient_confirmation_states(harness, monkeypatch):
    now = 1_700_010_000
    monkeypatch.setattr(app.time, "time", lambda: now)

    async def forbidden_pipeline(*args, **kwargs):
        raise AssertionError("Scheduler started the execution pipeline")

    async def stop_after_one_iteration(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden_pipeline)
    monkeypatch.setattr(app, "claim_plan_execution", forbidden_pipeline)
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_one_iteration)

    async def scenario():
        awaiting_id = await harness.add_plan(
            state="awaiting_confirmation",
            next_run=now - 10,
            message_id=501,
            expires_at=now + 300,
            scheduled_at=now - 10,
        )
        confirming_id = await harness.add_plan(state="confirming", next_run=now - 10)
        claiming_id = await harness.add_plan(state="claiming", next_run=now - 10)
        with pytest.raises(asyncio.CancelledError):
            await app.dca_scheduler()
        return awaiting_id, confirming_id, claiming_id

    awaiting_id, confirming_id, claiming_id = asyncio.run(scenario())
    awaiting = harness.plan(awaiting_id)
    assert awaiting["execution_state"] == "awaiting_confirmation"
    assert awaiting["confirmation_message_id"] == 501
    assert awaiting["active_order_id"] is None
    assert harness.plan(confirming_id)["execution_state"] == "confirming"
    assert harness.plan(confirming_id)["active_order_id"] is None
    assert harness.plan(claiming_id)["execution_state"] == "claiming"
    assert harness.plan(claiming_id)["active_order_id"] is None
    assert harness.bot.sent == []
    assert harness.fixedfloat_calls == []


def test_recovery_preserves_transient_states_with_active_orders(harness):
    async def scenario():
        confirming_active_id = await harness.add_plan(
            state="confirming", active_order_id="existing-order-1"
        )
        claiming_active_id = await harness.add_plan(
            state="claiming", active_order_id="existing-order-2"
        )
        confirming_empty_id = await harness.add_plan(state="confirming")
        claiming_empty_id = await harness.add_plan(state="claiming")
        await app.recover_stale_plan_claims()
        return confirming_active_id, claiming_active_id, confirming_empty_id, claiming_empty_id

    active_a, active_b, empty_a, empty_b = asyncio.run(scenario())
    assert harness.plan(active_a)["execution_state"] == "confirming"
    assert harness.plan(active_a)["active_order_id"] == "existing-order-1"
    assert harness.plan(active_b)["execution_state"] == "claiming"
    assert harness.plan(active_b)["active_order_id"] == "existing-order-2"
    assert harness.plan(empty_a)["execution_state"] == "scheduled"
    assert harness.plan(empty_a)["active_order_id"] is None
    assert harness.plan(empty_b)["execution_state"] == "scheduled"
    assert harness.plan(empty_b)["active_order_id"] is None


@pytest.mark.parametrize("telegram_was_sent", [False, True])
def test_recovery_safely_expires_orphaned_confirmation(
    harness, monkeypatch, telegram_was_sent
):
    now = 1_700_010_000
    scheduled_at = now - 100
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=None,
            expires_at=now + 300,
            scheduled_at=scheduled_at,
        )
    )
    if telegram_was_sent:
        harness.bot.sent.append((USER_ID, "unpersisted confirmation", {}, 777))
    sent_before_recovery = len(harness.bot.sent)

    asyncio.run(app.recover_dca_confirmations())

    row = harness.plan(plan_id)
    assert row["execution_state"] == "scheduled"
    assert row["next_run"] == app.calculate_next_run_preserving_schedule(
        scheduled_at, 24, now
    )
    assert row["confirmation_message_id"] is None
    assert row["confirmation_expires_at"] is None
    assert row["confirmation_scheduled_at"] is None
    assert row["skip_reason"] == "confirmation_timeout"
    assert row["missed_count"] == 1
    assert row["active_order_id"] is None
    assert len(harness.bot.sent) == sent_before_recovery
    assert harness.fixedfloat_calls == []


def test_user_skip_replaces_previous_skip_reason(harness, monkeypatch):
    now = 1_700_010_000
    scheduled_at = now - 100
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=601,
            expires_at=now + 300,
            scheduled_at=scheduled_at,
        )
    )
    with sqlite3.connect(harness.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET skip_reason = 'confirmation_timeout' WHERE id = ?",
            (plan_id,),
        )
    callback = FakeCallback(f"dca_skip:{plan_id}:{scheduled_at}", message_id=601)

    asyncio.run(app.cb_dca_skip(callback))

    assert harness.plan(plan_id)["skip_reason"] == "user_skipped"


def test_timeout_replaces_previous_skip_reason(harness):
    now = 1_700_010_000
    scheduled_at = now - 100
    plan_id = asyncio.run(
        harness.add_plan(
            state="awaiting_confirmation",
            next_run=scheduled_at,
            message_id=602,
            expires_at=now,
            scheduled_at=scheduled_at,
        )
    )
    with sqlite3.connect(harness.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET skip_reason = 'user_skipped' WHERE id = ?",
            (plan_id,),
        )

    assert asyncio.run(app.expire_dca_confirmation(plan_id, now)) is True
    assert harness.plan(plan_id)["skip_reason"] == "confirmation_timeout"
