import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from web3 import Web3

import bot as app
import auto_send as sender
from test_fixedfloat_create_persistence_window import (
    create_window, _install_successful_create, _seed_wallet,
    _plan_row, _transaction_rows, USER_ID, NETWORK, DEPOSIT_ADDRESS,
)

RAW = '0x01020304'
HASH = Web3.keccak(Web3.to_bytes(hexstr=RAW)).hex()


def install_send(monkeypatch, harness, status=0):
    snapshots = []
    eth = SimpleNamespace(wait_for_transaction_receipt=Mock(return_value=SimpleNamespace(status=status)))
    for name in ['get_transaction_count', 'send_raw_transaction', 'contract']:
        spy = Mock(side_effect=AssertionError('unexpected ' + name))
        setattr(eth, name, spy)
        harness.financial_spies.append(spy)
    w3 = SimpleNamespace(eth=eth, from_wei=Web3.from_wei)
    monkeypatch.setattr(sender, 'load_keystore', lambda _: {'crypto': {}})
    monkeypatch.setattr(sender, 'decrypt_private_key', lambda *_: '1' * 64)
    monkeypatch.setattr(sender, 'get_web3_instance', lambda _: w3)
    monkeypatch.setattr(sender, 'get_usdt_token_decimals', lambda *_: 6)
    monkeypatch.setattr(sender, 'get_usdt_balance_units', lambda *_: 100_000_000)
    monkeypatch.setattr(sender, 'get_native_balance_wei', lambda *_: 10**18)
    monkeypatch.setattr(sender, 'estimate_gas_for_transfer', lambda *_: 75000)
    monkeypatch.setattr(sender, 'build_gas_params', lambda *_: {'gasPrice': 10**9})

    def transfer(*args):
        args[-1]('transfer', HASH, 7, RAW)
        snapshots.append((_plan_row(harness.db_path, harness.plan_id),
                          _transaction_rows(harness.db_path, harness.plan_id)[0]))
        return HASH

    send = Mock(side_effect=transfer)
    monkeypatch.setattr(sender, 'transfer_usdt', send)
    return snapshots, send


def prepare(harness, monkeypatch, path):
    _seed_wallet(harness)
    if path == 'fresh':
        _install_successful_create(monkeypatch, harness)
    else:
        with sqlite3.connect(harness.db_path) as db:
            db.execute("UPDATE dca_plans SET active_order_id='external-order-1', active_order_token='external-token-1', active_order_address=?, active_order_expires=4000000000", (DEPOSIT_ADDRESS,))
            db.execute("INSERT INTO sent_transactions (plan_id,user_id,order_id,order_token,network_key,amount,deposit_address,state,amount_units,token_decimals,approve_tx_hash) VALUES (?,?, 'external-order-1','external-token-1',?,25,?,'approve_confirmed','25000000',6,?)", (harness.plan_id, USER_ID, NETWORK, DEPOSIT_ADDRESS, '0x'+'ab'*32))


def run(harness, path):
    if path == 'fresh':
        return asyncio.run(app.execute_new_order(app.NewOrderExecutionRequest(
            plan_id=harness.plan_id, user_id=USER_ID, trigger='manual')))
    return asyncio.run(app.reconcile_existing_order(
        plan_id=harness.plan_id, existing_order_id='external-order-1', trigger='manual'))


@pytest.mark.parametrize('path', ['fresh', 'resume'])
def test_immediate_revert_clears_gate(create_window, monkeypatch, path):
    h = create_window
    prepare(h, monkeypatch, path)
    snapshots, send = install_send(monkeypatch, h)
    result = run(h, path)
    assert result.outcome == 'failed'
    send.assert_called_once()
    before_plan, before_tx = snapshots[0]
    expected_tx = dict(before_tx, state='failed', error_message='Transfer tx reverted on-chain')
    assert _transaction_rows(h.db_path, h.plan_id)[0] == expected_tx
    expected_plan = dict(before_plan)
    for field in ['active_order_id', 'active_order_token', 'active_order_address', 'active_order_amount', 'active_order_expires']:
        expected_plan[field] = None
    expected_plan['execution_state'] = 'scheduled'
    assert _plan_row(h.db_path, h.plan_id) == expected_plan
    assert before_plan['active_order_id'] == 'external-order-1'
    assert before_plan['active_order_token'] == 'external-token-1'


@pytest.fixture(autouse=True)
def no_extra_financial_actions(monkeypatch, create_window):
    from eth_account import Account
    spies = []
    create_window.financial_spies = spies
    for module, name in [(app, 'rebroadcast_persisted_erc20_transaction'),
                         (app, 'rebroadcast_raw_transaction'),
                         (app, 'get_web3_instance'),
                         (Account, 'sign_transaction')]:
        spy = Mock(side_effect=AssertionError(name))
        monkeypatch.setattr(module, name, spy)
        spies.append(spy)
    yield
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize('path', ['fresh', 'resume'])
@pytest.mark.parametrize('mutation', ['raw', 'hash', 'nonce', 'token', 'order', 'terminal', 'monitor'])
def test_revert_fail_closed_on_changed_proof_or_owner(create_window, monkeypatch, path, mutation):
    h = create_window
    prepare(h, monkeypatch, path)
    snapshots, send = install_send(monkeypatch, h)
    original = app.apply_new_transfer_revert
    expected = []

    async def race(*args, **kwargs):
        if mutation == 'monitor':
            await app.mark_order_completed(h.plan_id, 'external-order-1', 'fixedfloat_done')
        else:
            sql = {
                'raw': "UPDATE sent_transactions SET transfer_raw_tx='0xaaaa'",
                'hash': "UPDATE sent_transactions SET transfer_tx_hash='other'",
                'nonce': "UPDATE sent_transactions SET transfer_tx_nonce=NULL",
                'token': "UPDATE dca_plans SET active_order_token='new-token'",
                'order': "UPDATE dca_plans SET active_order_id='new-order',active_order_token='new-token'",
                'terminal': "UPDATE sent_transactions SET state='failed'",
            }[mutation]
            with sqlite3.connect(h.db_path) as db:
                db.execute(sql)
        expected.append((_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id)))
        return await original(*args, **kwargs)

    monkeypatch.setattr(app, 'apply_new_transfer_revert', race)
    result = run(h, path)
    assert not result.should_notify
    assert (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id)) == expected[0]
    send.assert_called_once()
    assert len(h.external_creates) == (1 if path == 'fresh' else 0)


@pytest.mark.parametrize('path', ['fresh', 'resume'])
@pytest.mark.parametrize('failure', ['local', 'generic', 'ambiguous'])
def test_nondefinitive_failure_never_uses_new_transition(create_window, monkeypatch, path, failure):
    h = create_window
    prepare(h, monkeypatch, path)
    transition = Mock(side_effect=AssertionError('not a definitive receipt'))
    monkeypatch.setattr(app, 'apply_new_transfer_revert', transition)

    async def send(**kwargs):
        if failure != 'local':
            kwargs['persist_payment_intent'](25000000, 6)
            kwargs['persist_prepared_tx']('transfer', HASH, 7, RAW)
        if failure == 'ambiguous':
            return False, None, HASH, 'TX_PENDING:' + HASH
        return False, None, HASH if failure == 'generic' else None, 'Transfer transaction failed'

    monkeypatch.setattr(app, 'auto_send_usdt', send)
    result = run(h, path)
    assert result.outcome == ('tx_pending' if failure == 'ambiguous' else 'failed')
    assert _plan_row(h.db_path, h.plan_id)['active_order_id'] == 'external-order-1'
    transition.assert_not_called()


@pytest.mark.parametrize('recovery', ['startup_recovery', 'periodic'])
@pytest.mark.parametrize('path', ['fresh', 'resume'])
def test_revert_parity_with_recovery(create_window, monkeypatch, recovery, path):
    h = create_window
    prepare(h, monkeypatch, path)
    snapshots, send = install_send(monkeypatch, h)
    run(h, path)
    immediate = (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id))
    with sqlite3.connect(h.db_path) as db:
        plan, tx = snapshots[0]
        for table, row in [('dca_plans', plan), ('sent_transactions', tx)]:
            columns = list(row)
            db.execute('UPDATE ' + table + ' SET ' + ','.join(c+'=?' for c in columns) + ' WHERE id=?', [row[c] for c in columns] + [row['id']])
        db.execute("UPDATE sent_transactions SET state='tx_pending'")

    async def reverted(*_):
        return 'failed'

    monkeypatch.setattr(app, 'get_transfer_tx_status', reverted)
    if recovery == 'periodic':
        asyncio.run(app.reconcile_pending_transfer_receipts())
    else:
        asyncio.run(app.recovery_scan_pending_transactions())
    assert (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id)) == immediate
    send.assert_called_once()


def test_historical_failed_untouched(create_window, monkeypatch):
    h = create_window
    prepare(h, monkeypatch, 'resume')
    with sqlite3.connect(h.db_path) as db:
        db.execute("UPDATE sent_transactions SET state='failed',transfer_tx_hash=?,transfer_raw_tx=?,transfer_tx_nonce=7", (HASH, RAW))
    before = (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id))
    lookup = Mock(side_effect=AssertionError('historical receipt lookup'))
    monkeypatch.setattr(app, 'get_transfer_tx_status', lookup)
    asyncio.run(app.recovery_scan_pending_transactions())
    asyncio.run(app.reconcile_pending_transfer_receipts())
    assert (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id)) == before
    lookup.assert_not_called()


@pytest.mark.parametrize('path', ['fresh', 'resume'])
def test_next_scheduler_and_manual_pass_use_normal_policy(create_window, monkeypatch, path):
    h = create_window
    prepare(h, monkeypatch, path)
    _, send = install_send(monkeypatch, h)
    run(h, path)
    confirmation_calls = []

    async def confirm(**kwargs):
        confirmation_calls.append(kwargs['plan_id'])
        return False

    original_sleep = asyncio.sleep
    async def stop(delay, *args, **kwargs):
        if delay == 60:
            raise asyncio.CancelledError
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(app, 'create_dca_confirmation_request', confirm)
    monkeypatch.setattr(app.asyncio, 'sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())
    assert confirmation_calls == [h.plan_id]
    assert len(h.external_creates) == (1 if path == 'fresh' else 0)
    send.assert_called_once()

    class ReachedFreshExecution(BaseException):
        pass

    fresh = Mock(side_effect=ReachedFreshExecution)
    monkeypatch.setattr(app, 'execute_new_order', fresh)
    with pytest.raises(ReachedFreshExecution):
        asyncio.run(app.cmd_execute(h.message))
    fresh.assert_called_once()
    send.assert_called_once()


@pytest.mark.parametrize('status', ['expired', 'failed', 'cancelled'])
def test_provider_failure_is_not_receipt_proof(create_window, monkeypatch, status):
    h = create_window
    prepare(h, monkeypatch, 'resume')
    with sqlite3.connect(h.db_path) as db:
        db.execute("UPDATE sent_transactions SET state='tx_pending',transfer_tx_hash=?,transfer_raw_tx=?,transfer_tx_nonce=7", (HASH, RAW))
    before = (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id))
    result = asyncio.run(app.reconcile_existing_order(
        plan_id=h.plan_id, existing_order_id='external-order-1', trigger='manual', fixedfloat_status=status))
    assert result.outcome == 'manual_review'
    assert (_plan_row(h.db_path, h.plan_id), _transaction_rows(h.db_path, h.plan_id)) == before


@pytest.mark.parametrize('other', ['new', 'periodic', 'manual', 'scheduler'])
def test_concurrent_apply_once(create_window, monkeypatch, other):
    h = create_window
    prepare(h, monkeypatch, 'resume')
    with sqlite3.connect(h.db_path) as db:
        db.execute("UPDATE sent_transactions SET state='tx_pending',transfer_tx_hash=?,transfer_raw_tx=?,transfer_tx_nonce=7", (HASH, RAW))

    async def run_race():
        both = asyncio.Event()
        original = app._apply_transfer_receipt
        arrivals = []
        async def barrier(**kwargs):
            arrivals.append(1)
            if len(arrivals) == 2:
                both.set()
            await both.wait()
            return await original(**kwargs)
        async def receipt(*_):
            return 'failed'
        monkeypatch.setattr(app, '_apply_transfer_receipt', barrier)
        monkeypatch.setattr(app, 'get_transfer_tx_status', receipt)
        def new():
            return app.apply_new_transfer_revert(
                h.plan_id, 'external-order-1', 'external-token-1', sender.DefinitiveTransferRevert(HASH),
                commit_fn=app._commit_scheduler_transaction, should_notify=True)
        if other == 'new':
            second = new()
        elif other == 'periodic':
            second = app.observe_persisted_transfer_receipt(h.plan_id, 'external-order-1')
        else:
            second = app.reconcile_existing_order(plan_id=h.plan_id, existing_order_id='external-order-1', trigger=other)
        return await asyncio.gather(new(), second)

    results = asyncio.run(run_race())
    assert sum(r.outcome == 'failed' for r in results) == 1
    assert sum(r.should_notify for r in results) <= 1
    assert _plan_row(h.db_path, h.plan_id)['active_order_id'] is None
    assert _transaction_rows(h.db_path, h.plan_id)[0]['state'] == 'failed'


def test_failure_notification_follows_atomic_cleanup(create_window, monkeypatch):
    h = create_window
    prepare(h, monkeypatch, 'fresh')
    _, send = install_send(monkeypatch, h)
    original = app.build_auto_send_failed_notification
    observed = []

    def notification(**kwargs):
        plan = _plan_row(h.db_path, h.plan_id)
        tx = _transaction_rows(h.db_path, h.plan_id)[0]
        assert (plan['active_order_id'], plan['active_order_token'], tx['state']) == (None, None, 'failed')
        observed.append(kwargs['error_msg'])
        return original(**kwargs)

    monkeypatch.setattr(app, 'build_auto_send_failed_notification', notification)
    asyncio.run(app.cmd_execute(h.message))
    assert observed == ['Transfer tx reverted on-chain']
    assert h.external_creates == [1]
    send.assert_called_once()


def test_stale_revert_does_not_overwrite_monitor_notification(create_window, monkeypatch):
    h = create_window
    prepare(h, monkeypatch, 'fresh')
    _, send = install_send(monkeypatch, h)
    original = app.apply_new_transfer_revert
    after_monitor = []

    async def monitor_wins(*args, **kwargs):
        await app.mark_order_completed(h.plan_id, 'external-order-1', 'fixedfloat_done')
        await app.update_order_progress_message(USER_ID, 'external-order-1', 'Provider completed')
        after_monitor.append((list(h.bot.sent), list(h.bot.edited)))
        return await original(*args, **kwargs)

    monkeypatch.setattr(app, 'apply_new_transfer_revert', monitor_wins)
    asyncio.run(app.cmd_execute(h.message))
    assert (h.bot.sent, h.bot.edited) == after_monitor[0]
    assert _transaction_rows(h.db_path, h.plan_id)[0]['state'] == 'confirmed'
    assert h.external_creates == [1]
    send.assert_called_once()
