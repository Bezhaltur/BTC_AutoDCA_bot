"""
ERC20 USDT operations for EVM chains.
Handles balance checks, approvals, and transfers.
"""

import logging
import time
from typing import Optional, Tuple
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound
from eth_account import Account
from networks import get_network_config, get_usdt_contract_address
from requests.exceptions import Timeout as RequestsTimeout, ConnectionError as RequestsConnectionError

logger = logging.getLogger(__name__)


class TransactionBroadcastUncertain(RuntimeError):
    """Broadcast may have reached the network; never replace it with a new nonce."""

    def __init__(self, tx_hash: str, message: str):
        super().__init__(message)
        self.tx_hash = tx_hash


class PreparedTransactionConflict(RuntimeError):
    """A different signed intent is already persisted for this logical action."""

ARBITRUM_RPC_URLS = [
    "https://arb1.arbitrum.io/rpc",
    "https://rpc.ankr.com/arbitrum",
    "https://arbitrum.blockpi.network/v1/rpc/public",
]

BSC_RPC_URLS = [
    "https://bsc-dataseed.binance.org",
    "https://rpc.ankr.com/bsc",
    "https://bsc.publicnode.com",
]

POLYGON_RPC_URLS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
    "https://polygon.blockpi.network/v1/rpc/public",
    "https://1rpc.io/matic",
]
LEGACY_GAS_NETWORKS = {"USDT-BSC"}
GAS_MULTIPLIER = 1.2
DEFAULT_PRIORITY_FEE_GWEI = 0.1
POA_MIDDLEWARE_FLAG_ATTR = "_dca_poa_middleware_enabled"
RPC_TIMEOUT_SECONDS = 10
RPC_CONNECT_MAX_ATTEMPTS = 3
RPC_CONNECT_RETRY_DELAY_SECONDS = 1.5
TX_SEND_MAX_ATTEMPTS = 3
TX_RETRY_DELAY_SECONDS = 2.5
GAS_FALLBACK_TRANSFER = 200000
GAS_FALLBACK_APPROVE = 200000

# ERC20 ABI (minimal - only functions we need)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]


def _should_enable_poa_middleware(chain_id: int, rpc_url: str) -> bool:
    """Enable PoA middleware only for BSC-like networks."""
    rpc_lower = (rpc_url or "").lower()
    return int(chain_id) == 56 or "bsc" in rpc_lower


def _resolve_poa_middleware():
    """
    Resolve PoA middleware across Web3.py versions.
    - v7+: ExtraDataToPOAMiddleware
    - v6 and older: geth_poa_middleware
    Returns (middleware_callable, middleware_name) or (None, None).
    """
    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        return ExtraDataToPOAMiddleware, "ExtraDataToPOAMiddleware"
    except Exception:
        pass

    try:
        from web3.middleware import geth_poa_middleware
        return geth_poa_middleware, "geth_poa_middleware"
    except Exception:
        return None, None


def _inject_poa_middleware_if_needed(w3: Web3, network_name: str, expected_chain_id: int, rpc_url: str) -> None:
    """Inject PoA middleware once per Web3 instance when required."""
    if not _should_enable_poa_middleware(expected_chain_id, rpc_url):
        return
    if getattr(w3, POA_MIDDLEWARE_FLAG_ATTR, False):
        return

    middleware, middleware_name = _resolve_poa_middleware()
    if middleware is None:
        raise RuntimeError(
            f"PoA middleware is required for {network_name} ({rpc_url}) but not available in installed web3 version"
        )

    try:
        w3.middleware_onion.inject(middleware, layer=0)
        setattr(w3, POA_MIDDLEWARE_FLAG_ATTR, True)
        logger.info(
            "PoA middleware enabled for %s (%s): %s",
            network_name, rpc_url, middleware_name
        )
    except ValueError as e:
        # Defensive fallback for duplicate-inject scenarios.
        if "duplicate" in str(e).lower() or "already" in str(e).lower():
            setattr(w3, POA_MIDDLEWARE_FLAG_ATTR, True)
            logger.info("PoA middleware already present for %s (%s)", network_name, rpc_url)
            return
        raise RuntimeError(f"Failed to enable PoA middleware for {network_name}: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to enable PoA middleware for {network_name}: {e}") from e


def _build_rpc_candidates(network_key: str, primary_rpc: str, chain_id: int) -> list[str]:
    """Build ordered list of RPC endpoints with per-network fallbacks."""
    rpc_candidates = [primary_rpc]
    if network_key == "USDT-ARB":
        fallback_list = ARBITRUM_RPC_URLS
    elif network_key == "USDT-BSC":
        fallback_list = BSC_RPC_URLS
    elif network_key == "USDT-POLYGON" and int(chain_id) == 137:
        fallback_list = POLYGON_RPC_URLS
    else:
        fallback_list = []

    for rpc_url in fallback_list:
        if rpc_url not in rpc_candidates:
            rpc_candidates.append(rpc_url)
    return rpc_candidates


def _get_provider_url(w3: Web3) -> str:
    """Return provider endpoint URL for diagnostics."""
    return str(getattr(w3.provider, "endpoint_uri", "unknown-rpc"))


def _is_retryable_send_error(error: Exception) -> bool:
    """True for temporary RPC/network failures where retry is reasonable."""
    if isinstance(error, (TimeoutError, RequestsTimeout, RequestsConnectionError, ConnectionError, OSError, ValueError)):
        return True
    msg = str(error).lower()
    keywords = (
        "timeout",
        "timed out",
        "connection",
        "temporarily unavailable",
        "max retries exceeded",
        "read timed out",
        "502",
        "503",
        "504",
        "failed to estimate gas",
    )
    return any(keyword in msg for keyword in keywords)


def _format_gas_label_and_cost(w3: Web3, tx: dict) -> tuple[str, float]:
    """Format gas price details and estimated native-token cost."""
    if "gasPrice" in tx:
        gas_price_wei = int(tx["gasPrice"])
        gas_label = f"{w3.from_wei(gas_price_wei, 'gwei'):.2f} Gwei"
    else:
        max_fee_wei = int(tx["maxFeePerGas"])
        priority_fee_wei = int(tx.get("maxPriorityFeePerGas", 0))
        gas_price_wei = max_fee_wei
        gas_label = (
            f"maxFee={w3.from_wei(max_fee_wei, 'gwei'):.2f} Gwei, "
            f"priority={w3.from_wei(priority_fee_wei, 'gwei'):.2f} Gwei"
        )
    gas_cost = float(w3.from_wei(tx["gas"] * gas_price_wei, "ether"))
    return gas_label, gas_cost


def send_transaction_with_retry(
    w3: Web3,
    account: Account,
    tx_builder,
    *,
    network_key: str,
    action_name: str,
    persist_prepared_tx=None,
) -> tuple[str, dict]:
    """
    Send signed transaction with retries for temporary RPC issues.
    Returns (tx_hash_hex, tx_dict_used_for_successful_send).
    """
    if persist_prepared_tx is None:
        raise RuntimeError("Prepared transaction persistence is required before broadcast")

    tx = tx_builder()
    signed_tx = account.sign_transaction(tx)
    raw_tx = getattr(signed_tx, "rawTransaction", None)
    if raw_tx is None:
        raw_tx = signed_tx.raw_transaction
    tx_hash_hex = Web3.keccak(raw_tx).hex()
    raw_tx_hex = Web3.to_hex(raw_tx)

    try:
        persist_prepared_tx(action_name, tx_hash_hex, int(tx["nonce"]), raw_tx_hex)
    except PreparedTransactionConflict:
        raise
    except Exception as e:
        raise PreparedTransactionConflict(
            f"Prepared {action_name} transaction persistence did not complete"
        ) from e

    _broadcast_raw_transaction_with_retry(w3, raw_tx, tx_hash_hex, action_name=action_name)
    return tx_hash_hex, tx


def _broadcast_raw_transaction_with_retry(
    w3: Web3,
    raw_tx,
    tx_hash_hex: str,
    *,
    action_name: str,
) -> str:
    """Retry broadcast using only one already-signed transaction."""
    provider_url = _get_provider_url(w3)
    last_error = None
    for attempt in range(1, TX_SEND_MAX_ATTEMPTS + 1):
        try:
            if not w3.is_connected():
                raise ConnectionError(f"Web3 provider is not connected: {provider_url}")
            returned_hash = w3.eth.send_raw_transaction(raw_tx).hex()
            if returned_hash.lower() != tx_hash_hex.lower():
                raise TransactionBroadcastUncertain(
                    tx_hash_hex,
                    f"{action_name} RPC returned hash {returned_hash}, "
                    f"local signed hash is {tx_hash_hex}",
                )
            return tx_hash_hex
        except TransactionBroadcastUncertain:
            raise
        except Exception as e:
            last_error = e
            error_text = str(e).lower()
            if "already known" in error_text or "known transaction" in error_text:
                return tx_hash_hex
            if "nonce too low" in error_text:
                raise TransactionBroadcastUncertain(
                    tx_hash_hex,
                    f"{action_name} transaction broadcast status is uncertain: {e}",
                ) from e
            if not _is_retryable_send_error(e) or attempt >= TX_SEND_MAX_ATTEMPTS:
                raise TransactionBroadcastUncertain(
                    tx_hash_hex,
                    f"{action_name} transaction broadcast status is uncertain: {e}",
                ) from e
            logger.info(
                "Retrying %s transaction in %.1fs (attempt %s/%s)",
                action_name, TX_RETRY_DELAY_SECONDS, attempt + 1, TX_SEND_MAX_ATTEMPTS,
            )
            time.sleep(TX_RETRY_DELAY_SECONDS)

    raise TransactionBroadcastUncertain(
        tx_hash_hex,
        f"{action_name} transaction broadcast status is uncertain: {last_error}",
    ) from last_error


def rebroadcast_raw_transaction(
    w3: Web3,
    raw_tx_hex: str,
    expected_tx_hash: str,
    *,
    action_name: str,
) -> str:
    """Rebroadcast a persisted signed transaction without rebuilding it or changing nonce."""
    raw_tx = Web3.to_bytes(hexstr=raw_tx_hex)
    local_hash = Web3.keccak(raw_tx).hex()
    if local_hash.lower() != expected_tx_hash.lower():
        raise ValueError(
            f"Persisted {action_name} raw transaction hash mismatch: "
            f"expected {expected_tx_hash}, got {local_hash}"
        )

    return _broadcast_raw_transaction_with_retry(
        w3, raw_tx, local_hash, action_name=action_name
    )


def create_web3_client(network_key: str) -> Web3:
    """
    Create Web3 instance for network.
    
    Args:
        network_key: Network key (e.g., "USDT-ARB")
    
    Returns:
        Web3 instance
    """
    config = get_network_config(network_key)
    rpc_candidates = _build_rpc_candidates(network_key, config["rpc_url"], config["chain_id"])

    last_error = None
    for attempt in range(1, RPC_CONNECT_MAX_ATTEMPTS + 1):
        for rpc_url in rpc_candidates:
            try:
                logger.info(
                    "Trying RPC for %s (attempt %s/%s): %s",
                    config["name"], attempt, RPC_CONNECT_MAX_ATTEMPTS, rpc_url
                )
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}))
                if not w3.is_connected():
                    raise RuntimeError(f"RPC not reachable for {config['name']}: {rpc_url}")

                _inject_poa_middleware_if_needed(
                    w3=w3,
                    network_name=config["name"],
                    expected_chain_id=config["chain_id"],
                    rpc_url=rpc_url,
                )

                chain_id = w3.eth.chain_id
                if chain_id != config["chain_id"]:
                    raise RuntimeError(f"RPC chainId mismatch: expected {config['chain_id']}, got {chain_id}")
                logger.info("Using RPC for %s (chain_id=%s): %s", config["name"], chain_id, rpc_url)
                return w3
            except (RequestsTimeout, RequestsConnectionError, TimeoutError, ValueError, OSError, ConnectionError) as e:
                last_error = e
                logger.warning(
                    "RPC connection failed for %s: %s, err=%s",
                    config["name"], rpc_url, e
                )
            except Exception as e:
                last_error = e
                logger.warning("Unexpected RPC initialization error for %s: %s, err=%s", config["name"], rpc_url, e)
        if attempt < RPC_CONNECT_MAX_ATTEMPTS:
            logger.info(
                "Retrying RPC discovery for %s in %.1fs (%s/%s)",
                config["name"], RPC_CONNECT_RETRY_DELAY_SECONDS, attempt + 1, RPC_CONNECT_MAX_ATTEMPTS
            )
            time.sleep(RPC_CONNECT_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Failed to connect to {config['name']} RPCs ({', '.join(rpc_candidates)}): {last_error}"
    )


def get_web3_instance(network_key: str) -> Web3:
    """Backward-compatible wrapper for existing call sites."""
    return create_web3_client(network_key)


def build_gas_params(w3: Web3, network_key: str) -> dict:
    """
    Build gas params for EIP-1559 or legacy networks.
    """
    if network_key in LEGACY_GAS_NETWORKS:
        return {"gasPrice": int(w3.eth.gas_price)}

    try:
        latest_block = w3.eth.get_block("latest")
    except Exception as e:
        logger.warning(f"Failed to fetch latest block on {network_key}, fallback to gasPrice: {e}")
        return {"gasPrice": int(w3.eth.gas_price)}

    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        logger.warning(f"baseFeePerGas is None on {network_key}, fallback to gasPrice")
        return {"gasPrice": int(w3.eth.gas_price)}

    base_fee = int(base_fee)

    try:
        priority_fee = int(w3.eth.max_priority_fee)
    except Exception as e:
        priority_fee = int(w3.to_wei(DEFAULT_PRIORITY_FEE_GWEI, "gwei"))
        logger.warning(
            f"Failed to fetch max_priority_fee on {network_key}, "
            f"using default {DEFAULT_PRIORITY_FEE_GWEI} gwei: {e}"
        )

    max_fee = int((base_fee * 2 + priority_fee) * GAS_MULTIPLIER)
    if max_fee < base_fee:
        max_fee = base_fee * 2

    return {
        "maxPriorityFeePerGas": priority_fee,
        "maxFeePerGas": max_fee,
    }


def get_usdt_contract(w3: Web3, network_key: str):
    """Get USDT contract instance."""
    contract_address = get_usdt_contract_address(network_key)
    return w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=ERC20_ABI)


def get_usdt_balance(w3: Web3, network_key: str, address: str) -> float:
    """
    Get USDT balance for address.
    
    Args:
        w3: Web3 instance
        network_key: Network key
        address: Wallet address
    
    Returns:
        USDT balance (float)
    """
    try:
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        balance_wei = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        balance = balance_wei / (10 ** decimals)
        masked_addr = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
        logger.info(f"Balance check: {masked_addr} on {network_key} = {balance:.6f} USDT")
        return balance
    except Exception as e:
        masked_addr = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
        logger.error(f"Error getting USDT balance for {masked_addr} on {network_key}: {e}")
        raise RuntimeError(f"Failed to get USDT balance: {e}")


def get_native_balance(w3: Web3, address: str) -> float:
    """
    Get native token balance (ETH/BNB/POLYGON).
    
    Args:
        w3: Web3 instance
        address: Wallet address
    
    Returns:
        Native token balance in ETH/BNB/POLYGON
    """
    try:
        balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
        balance = Web3.from_wei(balance_wei, "ether")
        masked_addr = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
        logger.info(f"Native balance check: {masked_addr} = {float(balance):.6f}")
        return float(balance)
    except Exception as e:
        masked_addr = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address
        logger.error(f"Error getting native balance for {masked_addr}: {e}")
        raise RuntimeError(f"Failed to get native balance: {e}")


def estimate_gas_for_approve(w3: Web3, network_key: str, from_address: str, spender_address: str, amount: float) -> int:
    """
    Estimate gas for approve transaction.
    
    Args:
        w3: Web3 instance
        network_key: Network key
        from_address: Address approving
        spender_address: Address being approved
        amount: Amount to approve
    
    Returns:
        Estimated gas (int)
    """
    try:
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        amount_wei = int(amount * (10 ** decimals))
        
        tx = contract.functions.approve(
            Web3.to_checksum_address(spender_address),
            amount_wei
        ).build_transaction({
            "from": Web3.to_checksum_address(from_address),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), "pending"),
        })
        
        estimated_gas = w3.eth.estimate_gas(tx)
        masked_from = f"{from_address[:6]}...{from_address[-4:]}" if len(from_address) > 10 else from_address
        masked_spender = f"{spender_address[:6]}...{spender_address[-4:]}" if len(spender_address) > 10 else spender_address
        logger.info(f"Gas estimation (approve): {network_key}, from={masked_from}, spender={masked_spender}, amount={amount:.6f} USDT, gas={estimated_gas}")
        return estimated_gas
    except Exception as e:
        logger.warning(
            "Gas estimation failed for approve on %s, using fallback gas=%s: %s",
            network_key, GAS_FALLBACK_APPROVE, e
        )
        return GAS_FALLBACK_APPROVE


def estimate_gas_for_transfer(w3: Web3, network_key: str, from_address: str, to_address: str, amount: float) -> int:
    """
    Estimate gas for transfer transaction.
    
    Args:
        w3: Web3 instance
        network_key: Network key
        from_address: Sender address
        to_address: Recipient address
        amount: Amount to transfer
    
    Returns:
        Estimated gas (int)
    """
    try:
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        amount_wei = int(amount * (10 ** decimals))
        
        tx = contract.functions.transfer(
            Web3.to_checksum_address(to_address),
            amount_wei
        ).build_transaction({
            "from": Web3.to_checksum_address(from_address),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), "pending"),
        })
        
        estimated_gas = w3.eth.estimate_gas(tx)
        masked_from = f"{from_address[:6]}...{from_address[-4:]}" if len(from_address) > 10 else from_address
        masked_to = f"{to_address[:6]}...{to_address[-4:]}" if len(to_address) > 10 else to_address
        logger.info(f"Gas estimation (transfer): {network_key}, from={masked_from}, to={masked_to}, amount={amount:.6f} USDT, gas={estimated_gas}")
        return estimated_gas
    except Exception as e:
        logger.warning(
            "Gas estimation failed for transfer on %s, using fallback gas=%s: %s",
            network_key, GAS_FALLBACK_TRANSFER, e
        )
        return GAS_FALLBACK_TRANSFER


def approve_usdt(
    w3: Web3,
    network_key: str,
    private_key: str,
    spender_address: str,
    amount: float,
    dry_run: bool = False,
    persist_prepared_tx=None,
) -> Optional[str]:
    """
    Approve USDT spending (exact amount only).
    
    Args:
        w3: Web3 instance
        network_key: Network key
        private_key: Private key (hex with 0x)
        spender_address: Address to approve
        amount: Exact amount to approve
        dry_run: If True, don't broadcast transaction
    
    Returns:
        Transaction hash (None if dry_run)
    
    Raises:
        RuntimeError: If approval fails
    """
    try:
        account = Account.from_key(private_key)
        from_address = account.address
        
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        amount_wei = int(amount * (10 ** decimals))
        
        def build_approve_tx() -> dict:
            return contract.functions.approve(
                Web3.to_checksum_address(spender_address),
                amount_wei
            ).build_transaction({
                "from": from_address,
                "nonce": w3.eth.get_transaction_count(from_address, "pending"),
                "gas": estimate_gas_for_approve(w3, network_key, from_address, spender_address, amount),
                **build_gas_params(w3, network_key),
                "chainId": get_network_config(network_key)["chain_id"],
            })

        masked_from = f"{from_address[:6]}...{from_address[-4:]}" if len(from_address) > 10 else from_address
        masked_spender = f"{spender_address[:6]}...{spender_address[-4:]}" if len(spender_address) > 10 else spender_address
        
        if dry_run:
            tx = build_approve_tx()
            gas_label, gas_cost = _format_gas_label_and_cost(w3, tx)
            logger.info(f"[DRY RUN] Approve transaction prepared:")
            logger.info(f"  Network: {network_key}")
            logger.info(f"  From: {masked_from}")
            logger.info(f"  Spender: {masked_spender}")
            logger.info(f"  Amount: {amount:.6f} USDT")
            logger.info(f"  Gas: {tx['gas']}")
            logger.info(f"  Gas Params: {gas_label}")
            logger.info(f"  Estimated Cost: {gas_cost:.6f} {get_network_config(network_key)['native_token']}")
            logger.info(f"  [DRY RUN] Transaction NOT sent - would approve {amount:.6f} USDT")
            return None
        
        # Sign and send with retries
        logger.info(
            "Signing approve transaction: %s -> %s, amount=%.6f USDT, rpc=%s",
            masked_from, masked_spender, amount, _get_provider_url(w3)
        )
        tx_hash_hex, tx_used = send_transaction_with_retry(
            w3=w3,
            account=account,
            tx_builder=build_approve_tx,
            network_key=network_key,
            action_name="approve",
            persist_prepared_tx=persist_prepared_tx,
        )
        
        tx_gas_label, _ = _format_gas_label_and_cost(w3, tx_used)
        logger.info(f"Approve transaction sent: {tx_hash_hex}, gas={tx_used['gas']}, gas={tx_gas_label}")
        return tx_hash_hex
        
    except (TransactionBroadcastUncertain, PreparedTransactionConflict):
        raise
    except Exception as e:
        logger.error(f"Error approving USDT: {e}")
        raise RuntimeError(f"Failed to approve USDT: {e}")


def transfer_usdt(
    w3: Web3,
    network_key: str,
    private_key: str,
    to_address: str,
    amount: float,
    dry_run: bool = False,
    persist_prepared_tx=None,
) -> Optional[str]:
    """
    Transfer USDT to address.
    
    Args:
        w3: Web3 instance
        network_key: Network key
        private_key: Private key (hex with 0x)
        to_address: Recipient address
        amount: Amount to transfer
        dry_run: If True, don't broadcast transaction
    
    Returns:
        Transaction hash (None if dry_run)
    
    Raises:
        RuntimeError: If transfer fails
    """
    try:
        account = Account.from_key(private_key)
        from_address = account.address
        
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        amount_wei = int(amount * (10 ** decimals))
        
        def build_transfer_tx() -> dict:
            return contract.functions.transfer(
                Web3.to_checksum_address(to_address),
                amount_wei
            ).build_transaction({
                "from": from_address,
                "nonce": w3.eth.get_transaction_count(from_address, "pending"),
                "gas": estimate_gas_for_transfer(w3, network_key, from_address, to_address, amount),
                **build_gas_params(w3, network_key),
                "chainId": get_network_config(network_key)["chain_id"],
            })

        masked_from = f"{from_address[:6]}...{from_address[-4:]}" if len(from_address) > 10 else from_address
        masked_to = f"{to_address[:6]}...{to_address[-4:]}" if len(to_address) > 10 else to_address
        
        if dry_run:
            tx = build_transfer_tx()
            gas_label, gas_cost = _format_gas_label_and_cost(w3, tx)
            logger.info(f"[DRY RUN] Transfer transaction prepared:")
            logger.info(f"  Network: {network_key}")
            logger.info(f"  From: {masked_from}")
            logger.info(f"  To: {masked_to}")
            logger.info(f"  Amount: {amount:.6f} USDT")
            logger.info(f"  Gas: {tx['gas']}")
            logger.info(f"  Gas Params: {gas_label}")
            logger.info(f"  Estimated Cost: {gas_cost:.6f} {get_network_config(network_key)['native_token']}")
            logger.info(f"  [DRY RUN] Transaction NOT sent - would transfer {amount:.6f} USDT")
            return None
        
        # Sign and send with retries
        logger.info(
            "Signing transfer transaction: %s -> %s, amount=%.6f USDT, rpc=%s",
            masked_from, masked_to, amount, _get_provider_url(w3)
        )
        tx_hash_hex, tx_used = send_transaction_with_retry(
            w3=w3,
            account=account,
            tx_builder=build_transfer_tx,
            network_key=network_key,
            action_name="transfer",
            persist_prepared_tx=persist_prepared_tx,
        )
        
        tx_gas_label, _ = _format_gas_label_and_cost(w3, tx_used)
        logger.info(f"Transfer transaction sent: {tx_hash_hex}, gas={tx_used['gas']}, gas={tx_gas_label}")
        return tx_hash_hex
        
    except (TransactionBroadcastUncertain, PreparedTransactionConflict):
        raise
    except Exception as e:
        logger.error(f"Error transferring USDT: {e}")
        raise RuntimeError(f"Failed to transfer USDT: {e}")


def check_allowance(w3: Web3, network_key: str, owner_address: str, spender_address: str) -> float:
    """
    Check current USDT allowance.
    
    Args:
        w3: Web3 instance
        network_key: Network key
        owner_address: Owner address
        spender_address: Spender address
    
    Returns:
        Current allowance (float)
    """
    try:
        contract = get_usdt_contract(w3, network_key)
        decimals = contract.functions.decimals().call()
        allowance_wei = contract.functions.allowance(
            Web3.to_checksum_address(owner_address),
            Web3.to_checksum_address(spender_address)
        ).call()
        allowance = allowance_wei / (10 ** decimals)
        return allowance
    except Exception as e:
        logger.error(f"Error checking allowance: {e}")
        raise RuntimeError(f"Failed to check allowance: {e}")
