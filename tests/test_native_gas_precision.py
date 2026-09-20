import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from web3 import Web3

import auto_send as sender
import erc20


def test_required_native_balance_uses_exact_conservative_integer_formula():
    calculate = sender.calculate_required_native_balance_wei

    assert calculate(0, 0) == 0
    assert calculate(1, 1) == 2
    assert calculate(5, 1) == 9
    assert calculate(75_000, 1_000_000_000) == 135_000_000_000_000

    base_cost_above_float_exactness = (2**53) + 1
    assert calculate(1, base_cost_above_float_exactness) == (
        base_cost_above_float_exactness * 9 + 4
    ) // 5


@pytest.mark.parametrize(
    ("gas", "fee_per_gas", "balance_delta", "allowed"),
    [
        (75_000, 1_000_000_000, -1, False),
        (75_000, 1_000_000_000, 0, True),
        (75_000, 1_000_000_000, 1, True),
        # The old float path falsely rejected the exact boundary here.
        (200_000, 50_000_000_000, -1, False),
        (200_000, 50_000_000_000, 0, True),
        (200_000, 50_000_000_000, 1, True),
        # The old float path could accept one wei below this boundary.
        (75_000, 50_000_000_000, -1, False),
        (75_000, 50_000_000_000, 0, True),
        (75_000, 50_000_000_000, 1, True),
    ],
)
def test_production_native_balance_gate_is_exact_integer_wei(
    monkeypatch, gas, fee_per_gas, balance_delta, allowed
):
    required_wei = (gas * fee_per_gas * 9 + 4) // 5
    transfer_calls = []

    class FakeWeb3:
        eth = SimpleNamespace()

        @staticmethod
        def from_wei(value, unit):
            divisor = 10**18 if unit == "ether" else 10**9
            return Decimal(value) / Decimal(divisor)

    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: FakeWeb3())
    monkeypatch.setattr(
        sender, "get_network_config", lambda _network: {"native_token": "ETH"}
    )
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 6)
    monkeypatch.setattr(sender, "get_usdt_balance_units", lambda *_args: 25_000_000)
    monkeypatch.setattr(
        sender,
        "get_native_balance_wei",
        lambda *_args: required_wei + balance_delta,
    )
    monkeypatch.setattr(sender, "estimate_gas_for_transfer", lambda *_args: gas)
    monkeypatch.setattr(
        sender, "build_gas_params", lambda *_args: {"gasPrice": fee_per_gas}
    )

    def transfer(*args):
        transfer_calls.append(args)
        return None

    monkeypatch.setattr(sender, "transfer_usdt", transfer)
    sender._SEND_LOCKS.clear()

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-BSC",
            user_id=10001,
            wallet_password="password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="25",
            btc_address="bc1unused",
            order_id="integer-native-gate",
            dry_run=True,
        )
    )

    if allowed:
        assert result == (True, None, None, "DRY RUN: Would transfer USDT")
        assert len(transfer_calls) == 1
    else:
        assert result[:3] == (False, None, None)
        assert result[3].startswith("Insufficient ETH balance for gas")
        assert transfer_calls == []


def test_get_native_balance_wei_returns_raw_rpc_integer():
    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_balance=lambda _address: (2**53) + 1)
    )
    assert (
        erc20.get_native_balance_wei(
            w3, "0x1111111111111111111111111111111111111111"
        )
        == (2**53) + 1
    )


@pytest.mark.parametrize("rpc_value", [True, Decimal("1"), -1])
def test_get_native_balance_wei_rejects_non_integer_or_negative_rpc_values(
    rpc_value,
):
    w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda _address: rpc_value))
    with pytest.raises(RuntimeError, match="native balance RPC result"):
        erc20.get_native_balance_wei(
            w3, "0x1111111111111111111111111111111111111111"
        )


def test_eip1559_fee_margin_uses_exact_ceiling_and_integer_fields():
    fee_base = (2**53) + 1
    base_fee = (fee_base - 101) // 2
    priority_fee = fee_base - base_fee * 2
    w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=lambda _block: {"baseFeePerGas": base_fee},
            max_priority_fee=priority_fee,
        )
    )

    params = erc20.build_gas_params(w3, "USDT-ARB")

    assert params == {
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": (fee_base * 6 + 4) // 5,
    }
    assert all(type(value) is int for value in params.values())


def test_priority_fee_fallback_is_exact_integer_wei():
    class FailingPriorityEth:
        @staticmethod
        def get_block(_block):
            return {"baseFeePerGas": 1_000_000_000}

        @property
        def max_priority_fee(self):
            raise RuntimeError("priority RPC unavailable")

    w3 = SimpleNamespace(eth=FailingPriorityEth())
    params = erc20.build_gas_params(w3, "USDT-ARB")

    assert erc20.DEFAULT_PRIORITY_FEE_WEI == 100_000_000
    assert params["maxPriorityFeePerGas"] == 100_000_000
    assert params["maxFeePerGas"] == ((2_000_000_000 + 100_000_000) * 6 + 4) // 5
    assert all(type(value) is int for value in params.values())


def test_legacy_gas_price_remains_exact_integer():
    w3 = SimpleNamespace(eth=SimpleNamespace(gas_price=(2**53) + 1))
    params = erc20.build_gas_params(w3, "USDT-BSC")

    assert params == {"gasPrice": (2**53) + 1}
    assert type(params["gasPrice"]) is int
    assert sender.calculate_required_native_balance_wei(75_000, params["gasPrice"]) == (
        75_000 * ((2**53) + 1) * 9 + 4
    ) // 5


@pytest.mark.parametrize(
    ("network_key", "gas_params"),
    [
        ("USDT-BSC", {"gasPrice": 3_000_000_001}),
        (
            "USDT-ARB",
            {"maxPriorityFeePerGas": 100_000_000, "maxFeePerGas": 3_600_000_002},
        ),
    ],
)
def test_transfer_transaction_fee_fields_are_python_ints(
    monkeypatch, network_key, gas_params
):
    built_transactions = []

    class TransferBuilder:
        def build_transaction(self, tx):
            built_transactions.append(dict(tx))
            return dict(tx)

    contract = SimpleNamespace(
        functions=SimpleNamespace(
            transfer=lambda _to, _units: TransferBuilder(),
        )
    )
    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_transaction_count=lambda *_args: 7),
        from_wei=lambda value, unit: Decimal(value)
        / Decimal(10**18 if unit == "ether" else 10**9),
    )

    monkeypatch.setattr(
        erc20.Account,
        "from_key",
        lambda _key: SimpleNamespace(
            address="0x2222222222222222222222222222222222222222"
        ),
    )
    monkeypatch.setattr(erc20, "get_usdt_contract", lambda *_args: contract)
    monkeypatch.setattr(erc20, "estimate_gas_for_transfer", lambda *_args: 75_001)
    monkeypatch.setattr(erc20, "build_gas_params", lambda *_args: dict(gas_params))
    monkeypatch.setattr(
        erc20,
        "get_network_config",
        lambda _network: {"chain_id": 42161, "native_token": "ETH"},
    )

    assert (
        erc20.transfer_usdt(
            w3,
            network_key,
            "0x" + "1" * 64,
            "0x1111111111111111111111111111111111111111",
            25_000_000,
            dry_run=True,
        )
        is None
    )

    tx = built_transactions[0]
    assert tx["gas"] == 75_001
    for field in ("gas", "nonce", "chainId", *gas_params):
        assert type(tx[field]) is int
