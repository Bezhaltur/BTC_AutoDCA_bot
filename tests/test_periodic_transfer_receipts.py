import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from web3 import Web3
from web3.exceptions import TransactionNotFound
from eth_account import Account

import bot as app
import auto_send
import erc20
from test_existing_order_reconciliation import (
    PLAN_ID, ORDER_ID, _seed, _snapshot, reconciliation_db,
)

HASH = "0x" + "67" * 32
RAW = "0x01020304"


@pytest.fixture(autouse=True)
def forbid_financial_side_effects(monkeypatch, reconciliation_db):
    spies = []
    for module, name in [
        (app, "create_fixedfloat_order"), (app, "ff_request_async"),
        (app, "auto_send_usdt"), (app, "rebroadcast_persisted_erc20_transaction"),
        (app, "rebroadcast_raw_transaction"), (erc20, "transfer_usdt"),
        (auto_send, "transfer_usdt"), (erc20, "approve_usdt"),
        (Account, "sign_transaction"), (app.bot, "send_message"),
    ]:
        spy = Mock(side_effect=AssertionError(f"Forbidden: {name}"))
        monkeypatch.setattr(module, name, spy)
        spies.append(spy)
    yield
    for spy in spies:
        spy.assert_not_called()


def install_rpc(monkeypatch, receipt=None, error=None):
    lookup = Mock(return_value=receipt, side_effect=error)
    nonce = Mock(side_effect=AssertionError("nonce allocation"))
    send = Mock(side_effect=AssertionError("broadcast"))
    build = Mock(side_effect=AssertionError("contract/transfer build"))
    w3 = SimpleNamespace(eth=SimpleNamespace(
        get_transaction_receipt=lookup, get_transaction_count=nonce,
        send_raw_transaction=send, contract=build,
    ))
    monkeypatch.setattr(app, "get_web3_instance", lambda *_: w3)
    return lookup, nonce, send, build


@pytest.mark.parametrize("trigger", ["manual", "scheduler"])
@pytest.mark.parametrize("state", ["tx_pending", "blocked"])
def test_live_receipt_gap_closed(reconciliation_db, monkeypatch, trigger, state):
    _seed(reconciliation_db, state=state, exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)
    calls = []

    async def receipt(*args):
        calls.append(args)
        return "confirmed"

    monkeypatch.setattr(app, "get_transfer_tx_status", receipt)
    result = asyncio.run(app.reconcile_existing_order(
        plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger,
    ))
    assert result.outcome == "confirmed"
    assert calls == [("USDT-ARB", HASH)]
    assert _snapshot(reconciliation_db)[0] == before[0]


@pytest.mark.parametrize("state", ["tx_pending", "blocked"])
@pytest.mark.parametrize("raw_only", [False, True])
def test_periodic_success(reconciliation_db, monkeypatch, state, raw_only):
    _seed(reconciliation_db, state=state, exact=True,
          transfer_hash=None if raw_only else HASH,
          transfer_raw=RAW if raw_only else None)
    before = _snapshot(reconciliation_db)
    lookup, *forbidden = install_rpc(monkeypatch, SimpleNamespace(status=1))
    asyncio.run(app.reconcile_pending_transfer_receipts())
    after = _snapshot(reconciliation_db)
    assert after[0] == before[0]
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state,transfer_tx_hash,transfer_raw_tx,amount_units,token_decimals FROM sent_transactions").fetchone() == (
            "confirmed", None if raw_only else HASH, RAW if raw_only else None, "25000000", 6,
        )
    lookup.assert_called_once_with(Web3.keccak(Web3.to_bytes(hexstr=RAW)).hex() if raw_only else HASH)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == after
    lookup.assert_called_once()
    for spy in forbidden:
        spy.assert_not_called()


@pytest.mark.parametrize("error", [None, TransactionNotFound(), TimeoutError(), ConnectionError()])
@pytest.mark.parametrize("state", ["tx_pending", "blocked"])
def test_unavailable_receipt_is_exact_noop(reconciliation_db, monkeypatch, state, error):
    _seed(reconciliation_db, state=state, exact=True, transfer_raw=RAW)
    before = _snapshot(reconciliation_db)
    lookup, *forbidden = install_rpc(monkeypatch, error=error)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before
    lookup.assert_called_once()
    for spy in forbidden:
        spy.assert_not_called()


@pytest.mark.parametrize("complete_evidence", [False, True])
def test_revert_preserves_existing_gate_proof_rules(reconciliation_db, monkeypatch, complete_evidence):
    tx_hash = Web3.keccak(Web3.to_bytes(hexstr=RAW)).hex() if complete_evidence else HASH
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=tx_hash,
          transfer_raw=RAW if complete_evidence else None)
    if complete_evidence:
        with sqlite3.connect(reconciliation_db) as db:
            db.execute("UPDATE sent_transactions SET transfer_tx_nonce=7")
    before = _snapshot(reconciliation_db)
    install_rpc(monkeypatch, SimpleNamespace(status=0))
    result = asyncio.run(app.observe_persisted_transfer_receipt(PLAN_ID, ORDER_ID))
    if complete_evidence:
        assert result.outcome == "failed"
        with sqlite3.connect(reconciliation_db) as db:
            assert db.execute("SELECT state,transfer_tx_hash,transfer_raw_tx,transfer_tx_nonce FROM sent_transactions").fetchone() == ("failed", tx_hash, RAW, 7)
            assert db.execute("SELECT active_order_id,next_run FROM dca_plans").fetchone() == (None, 1_700_000_000)
    else:
        assert result.outcome == "manual_review"
        assert _snapshot(reconciliation_db) == before


@pytest.mark.parametrize("state", ["failed", "sent", "confirmed", "expired", "sending", "transfering", "approve_confirmed", "pending"])
def test_other_states_untouched(reconciliation_db, monkeypatch, state):
    _seed(reconciliation_db, state=state, exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)
    lookup, *_ = install_rpc(monkeypatch, SimpleNamespace(status=1))
    asyncio.run(app.reconcile_pending_transfer_receipts())
    asyncio.run(app.observe_persisted_transfer_receipt(PLAN_ID, ORDER_ID))
    assert _snapshot(reconciliation_db) == before
    lookup.assert_not_called()


@pytest.mark.parametrize("approve_hash", [None, HASH])
def test_blocked_without_transfer_is_untouched(reconciliation_db, monkeypatch, approve_hash):
    _seed(reconciliation_db, state="blocked", exact=True, approve_hash=approve_hash)
    before = _snapshot(reconciliation_db)
    lookup, *_ = install_rpc(monkeypatch, SimpleNamespace(status=1))
    asyncio.run(app.reconcile_pending_transfer_receipts())
    asyncio.run(app.observe_persisted_transfer_receipt(PLAN_ID, ORDER_ID))
    assert _snapshot(reconciliation_db) == before
    lookup.assert_not_called()


@pytest.mark.parametrize("active,deleted", [(0, 0), (0, 1), (1, 0)])
def test_scheduler_hook_before_plan_eligibility(reconciliation_db, monkeypatch, active, deleted):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    with sqlite3.connect(reconciliation_db) as db:
        db.execute("UPDATE dca_plans SET active=?,deleted=?,next_run=4000000000", (active, deleted))
    before = _snapshot(reconciliation_db)[0]
    lookup, *_ = install_rpc(monkeypatch, SimpleNamespace(status=1))

    original_sleep = asyncio.sleep

    async def stop_after_one_pass(delay, *args, **kwargs):
        if delay == 60:
            raise asyncio.CancelledError
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(app.asyncio, "sleep", stop_after_one_pass)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())
    lookup.assert_called_once_with(HASH)
    assert _snapshot(reconciliation_db)[0] == before
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("confirmed",)


def test_concurrent_manual_scheduler_observations(reconciliation_db, monkeypatch):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)[0]

    async def run():
        both = asyncio.Event()
        calls = []

        async def receipt(*_):
            calls.append(1)
            if len(calls) == 2:
                both.set()
            await both.wait()
            return "confirmed"

        monkeypatch.setattr(app, "get_transfer_tx_status", receipt)
        return await asyncio.gather(*(app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger,
        ) for trigger in ["manual", "scheduler"]))

    results = asyncio.run(run())
    assert sorted(r.outcome for r in results) == ["confirmed", "stale_result"]
    assert all(not r.should_notify for r in results)
    assert _snapshot(reconciliation_db)[0] == before


@pytest.mark.parametrize("race", ["monitor", "failed", "evidence", "network"])
@pytest.mark.parametrize("status", ["confirmed", "failed"])
def test_receipt_race_does_not_overwrite_new_state(reconciliation_db, monkeypatch, race, status):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    after_race = []

    async def receipt(*_):
        if race == "monitor":
            await app.mark_order_completed(PLAN_ID, ORDER_ID, "fixedfloat_finished")
        else:
            with sqlite3.connect(reconciliation_db) as db:
                if race == "failed":
                    db.execute("UPDATE sent_transactions SET state='failed'")
                elif race == "network":
                    db.execute("UPDATE sent_transactions SET network_key='USDT-BSC'")
                else:
                    db.execute("UPDATE sent_transactions SET transfer_tx_hash='changed'")
        after_race.append(_snapshot(reconciliation_db))
        return status

    monkeypatch.setattr(app, "get_transfer_tx_status", receipt)
    result = asyncio.run(app.observe_persisted_transfer_receipt(PLAN_ID, ORDER_ID))
    assert result.outcome == ("stale_result" if status == "confirmed" else "manual_review")
    assert not result.should_notify
    assert _snapshot(reconciliation_db) == after_race[0]


def test_concurrent_revert_applies_once(reconciliation_db, monkeypatch):
    tx_hash = Web3.keccak(Web3.to_bytes(hexstr=RAW)).hex()
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=tx_hash, transfer_raw=RAW)
    with sqlite3.connect(reconciliation_db) as db:
        db.execute("UPDATE sent_transactions SET transfer_tx_nonce=7")

    async def run():
        both = asyncio.Event()
        calls = []

        async def receipt(*_):
            calls.append(1)
            if len(calls) == 2:
                both.set()
            await both.wait()
            return "failed"

        monkeypatch.setattr(app, "get_transfer_tx_status", receipt)
        return await asyncio.gather(*(app.observe_persisted_transfer_receipt(
            PLAN_ID, ORDER_ID,
        ) for _ in range(2)))

    results = asyncio.run(run())
    assert [r.outcome for r in results].count("failed") == 1
    assert all(not r.should_notify for r in results)
    before = _snapshot(reconciliation_db)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before


@pytest.mark.parametrize("receipt", [SimpleNamespace(), SimpleNamespace(status=2)])
def test_nondefinitive_receipt_untouched(reconciliation_db, monkeypatch, receipt):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)
    install_rpc(monkeypatch, receipt)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before


def test_provider_unavailable_untouched(reconciliation_db, monkeypatch):
    _seed(reconciliation_db, state="blocked", exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)
    monkeypatch.setattr(app, "get_web3_instance", Mock(side_effect=RuntimeError("unavailable")))
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before


def test_missing_tx_network_uses_persisted_plan_network(reconciliation_db, monkeypatch):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    with sqlite3.connect(reconciliation_db) as db:
        db.execute("UPDATE sent_transactions SET network_key=''")
    before = _snapshot(reconciliation_db)[0]
    lookup, *_ = install_rpc(monkeypatch, SimpleNamespace(status=1))
    provider = Mock(wraps=app.get_web3_instance)
    monkeypatch.setattr(app, "get_web3_instance", provider)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    provider.assert_called_once_with("USDT-ARB")
    lookup.assert_called_once_with(HASH)
    assert _snapshot(reconciliation_db)[0] == before
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state,network_key FROM sent_transactions").fetchone() == ("confirmed", "")


def test_invalid_raw_untouched(reconciliation_db, monkeypatch):
    _seed(reconciliation_db, state="blocked", exact=True, transfer_raw="not hex")
    before = _snapshot(reconciliation_db)
    lookup, *_ = install_rpc(monkeypatch, SimpleNamespace(status=1))
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before
    lookup.assert_not_called()


def test_commit_failure_rolls_back(reconciliation_db, monkeypatch):
    _seed(reconciliation_db, state="tx_pending", exact=True, transfer_hash=HASH)
    before = _snapshot(reconciliation_db)
    install_rpc(monkeypatch, SimpleNamespace(status=1))

    async def fail_commit(_):
        raise sqlite3.OperationalError("injected commit error")

    monkeypatch.setattr(app, "_commit_scheduler_transaction", fail_commit)
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert _snapshot(reconciliation_db) == before
    with sqlite3.connect(reconciliation_db, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
