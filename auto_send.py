"""
Automatic USDT sending to FixedFloat deposit addresses.
Handles all checks and direct transfers.
"""

import asyncio
import logging
import re
from decimal import Decimal
from typing import Optional, Tuple
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from networks import get_network_config, get_blockchair_url
from erc20 import (
    PreparedTransactionConflict,
    TransactionBroadcastUncertain,
    get_web3_instance,
    decimal_amount_to_units,
    get_usdt_balance_units,
    get_usdt_token_decimals,
    get_native_balance,
    has_sufficient_token_balance,
    validate_decimal_amount_text,
    transfer_usdt,
    estimate_gas_for_transfer,
    build_gas_params,
)
from wallet import load_keystore, decrypt_private_key

logger = logging.getLogger(__name__)

# Gas price multiplier for safety margin
GAS_PRICE_MULTIPLIER = 1.2
# Minimum native token balance multiplier (for safety)
MIN_NATIVE_MULTIPLIER = 1.5

HEX_PRIVATE_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SEND_LOCKS = {}
_SEND_LOCKS_GUARD = asyncio.Lock()


async def _get_wallet_send_lock(network_key: str, wallet_address: str) -> asyncio.Lock:
    """Per wallet+network lock to prevent nonce races on parallel sends."""
    key = (network_key, wallet_address.lower())
    async with _SEND_LOCKS_GUARD:
        lock = _SEND_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _SEND_LOCKS[key] = lock
        return lock


async def auto_send_usdt(
    network_key: str,
    user_id: int,
    wallet_password: str,
    deposit_address: str,
    required_amount,
    btc_address: str,
    order_id: str,
    dry_run: bool = False,
    persist_prepared_tx=None,
    amount_units: Optional[int] = None,
    token_decimals: Optional[int] = None,
    persist_payment_intent=None,
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """
    Automatically send USDT to FixedFloat deposit address.
    
    Performs all checks:
    - Deposit address validation
    - BTC address validation
    - USDT balance check
    - Native token balance check
    - Direct transfer
    
    Args:
        network_key: Network key (e.g., "USDT-ARB")
        user_id: Telegram user ID
        wallet_password: Keystore password
        deposit_address: FixedFloat deposit address
        required_amount: Required USDT amount
        btc_address: Expected BTC address (for validation)
        order_id: FixedFloat order ID
        dry_run: If True, don't broadcast transactions
    
    Returns:
        Tuple of (success, approve_tx_hash, transfer_tx_hash, error_message)
        - success: True if transfer succeeded
        - approve_tx_hash: Always None for new direct transfers
        - transfer_tx_hash: Transaction hash for transfer
        - error_message: Error message if failed
    """
    try:
        try:
            has_persisted_units = amount_units is not None or token_decimals is not None
            if has_persisted_units:
                if amount_units is None or token_decimals is None:
                    raise ValueError("incomplete persisted exact payment intent")
                if (
                    isinstance(amount_units, bool)
                    or not isinstance(amount_units, int)
                    or amount_units <= 0
                ):
                    raise ValueError("persisted amount units must be a positive integer")
                if (
                    isinstance(token_decimals, bool)
                    or not isinstance(token_decimals, int)
                    or token_decimals < 0
                    or token_decimals > 255
                ):
                    raise ValueError("persisted token decimals must be an integer from 0 to 255")
                # Recovery must never reconstruct an exact intent from legacy REAL.
                amount_text = format(
                    Decimal(str(amount_units)).scaleb(-token_decimals), "f"
                )
            elif isinstance(required_amount, str):
                # Validate syntax/finite/positive before any wallet or RPC work;
                # units are resolved exactly once after reading token decimals.
                amount_text = validate_decimal_amount_text(required_amount)
            else:
                raise ValueError("amount must be an exact positive decimal string")
        except (TypeError, ValueError) as e:
            return (False, None, None, f"INVALID_PAYMENT_AMOUNT:{e}")

        # Load keystore (single wallet for all networks)
        keystore = load_keystore(user_id)
        if not keystore:
            return (False, None, None, f"Wallet not configured. Use /setwallet to configure.")
        
        # Decrypt private key (in memory only)
        try:
            private_key_hex = decrypt_private_key(keystore, wallet_password)
        except ValueError as e:
            return (False, None, None, f"Incorrect wallet password: {e}")

        if private_key_hex.startswith("0x"):
            private_key_hex = private_key_hex[2:]
        if not HEX_PRIVATE_KEY_RE.fullmatch(private_key_hex):
            return (
                False,
                None,
                None,
                "Invalid private key format in keystore. Please reconfigure wallet with /setwallet."
            )

        private_key = "0x" + private_key_hex
        
        from eth_account import Account
        account = Account.from_key(private_key)
        wallet_address = account.address
        masked_wallet = f"{wallet_address[:6]}...{wallet_address[-4:]}" if len(wallet_address) > 10 else wallet_address
        masked_deposit = f"{deposit_address[:6]}...{deposit_address[-4:]}" if len(deposit_address) > 10 else deposit_address
        
        logger.info(f"=== Auto-send USDT started ===")
        logger.info(f"Order ID: {order_id}")
        logger.info(f"Network: {network_key}")
        logger.info(f"Wallet: {masked_wallet}")
        logger.info(f"Deposit: {masked_deposit}")
        logger.info(f"Amount: {amount_text} USDT")
        logger.info(f"Dry-run: {dry_run}")
        
        # Initialize Web3
        w3 = await asyncio.to_thread(get_web3_instance, network_key)
        config = get_network_config(network_key)
        
        # Check 1: Validate deposit address format
        logger.info(f"Check 1: Validating deposit address format...")
        try:
            deposit_address_checksum = Web3.to_checksum_address(deposit_address)
            logger.info(f"✓ Deposit address valid: {masked_deposit}")
        except Exception as e:
            logger.error(f"✗ Invalid deposit address format: {e}")
            return (False, None, None, f"Invalid deposit address format: {e}")
        
        # Check 2: Get balances
        logger.info(f"Check 2: Checking balances...")
        try:
            observed_decimals = await asyncio.to_thread(
                get_usdt_token_decimals, w3, network_key
            )
            if token_decimals is not None and int(token_decimals) != observed_decimals:
                raise ValueError(
                    f"persisted token decimals {token_decimals} do not match contract decimals {observed_decimals}"
                )
            if amount_units is None:
                resolved_amount_units = decimal_amount_to_units(
                    amount_text, observed_decimals
                )
            else:
                resolved_amount_units = amount_units
            balance_units = await asyncio.to_thread(
                get_usdt_balance_units, w3, network_key, wallet_address
            )
            token_decimals = observed_decimals
            if persist_payment_intent is not None:
                persist_payment_intent(resolved_amount_units, token_decimals)
            amount_decimal = Decimal(str(resolved_amount_units)) / (
                Decimal("10") ** token_decimals
            )
            usdt_balance = Decimal(str(balance_units)) / (
                Decimal("10") ** token_decimals
            )
            native_balance = await asyncio.to_thread(get_native_balance, w3, wallet_address)
            logger.info(f"✓ USDT balance: {usdt_balance:.6f} USDT")
            logger.info(f"✓ Native balance: {native_balance:.6f} {config['native_token']}")
        except PreparedTransactionConflict as e:
            logger.warning("Payment intent persistence conflict: %s", e)
            return (False, None, None, f"PERSISTENCE_CONFLICT:{e}")
        except (TypeError, ValueError) as e:
            logger.error("Invalid exact payment amount: %s", e)
            return (False, None, None, f"INVALID_PAYMENT_AMOUNT:{e}")
        except Exception as e:
            logger.error(f"✗ Failed to check balances: {e}")
            return (False, None, None, f"Failed to check balances: {e}")
        
        # Check 3: USDT balance sufficient
        logger.info(f"Check 3: Verifying USDT balance sufficient...")
        if not has_sufficient_token_balance(balance_units, resolved_amount_units):
            logger.error(f"✗ Insufficient USDT: required={amount_decimal:.6f}, available={usdt_balance:.6f}")
            return (
                False, None, None,
                f"Insufficient USDT balance.\n"
                f"Required: {amount_decimal:.6f} USDT\n"
                f"Available: {usdt_balance:.6f} USDT\n"
                f"Shortage: {amount_decimal - usdt_balance:.6f} USDT"
            )
        logger.info(f"✓ USDT balance sufficient")
        
        # Check 4: Estimate gas for the direct transfer
        logger.info(f"Check 4: Estimating gas for transfer...")
        try:
            transfer_gas = await asyncio.to_thread(
                estimate_gas_for_transfer,
                w3,
                network_key,
                wallet_address,
                deposit_address_checksum,
                resolved_amount_units,
            )
            total_gas = transfer_gas
            
            gas_params = await asyncio.to_thread(build_gas_params, w3, network_key)
            if "gasPrice" in gas_params:
                gas_price_wei = int(gas_params["gasPrice"])
                gas_label = f"{w3.from_wei(gas_price_wei, 'gwei'):.2f} Gwei"
            else:
                gas_price_wei = int(gas_params["maxFeePerGas"])
                priority_fee_wei = int(gas_params.get("maxPriorityFeePerGas", 0))
                gas_label = (
                    f"maxFee={w3.from_wei(gas_price_wei, 'gwei'):.2f} Gwei, "
                    f"priority={w3.from_wei(priority_fee_wei, 'gwei'):.2f} Gwei"
                )
            total_gas_cost_wei = total_gas * gas_price_wei * GAS_PRICE_MULTIPLIER
            total_gas_cost = w3.from_wei(total_gas_cost_wei, "ether")
            min_native_required = float(total_gas_cost) * MIN_NATIVE_MULTIPLIER
            
            logger.info(f"✓ Gas estimation complete:")
            logger.info(f"  Transfer gas: {transfer_gas}")
            logger.info(f"  Total gas: {total_gas}")
            logger.info(f"  Gas params: {gas_label}")
            logger.info(f"  Estimated cost: {total_gas_cost:.6f} {config['native_token']}")
            logger.info(f"  Required (with margin): {min_native_required:.6f} {config['native_token']}")
        except Exception as e:
            logger.error(f"✗ Failed to estimate gas: {e}")
            return (False, None, None, f"Failed to estimate gas: {e}")
        
        # Check 5: Native token balance sufficient
        logger.info(f"Check 5: Verifying native token balance sufficient...")
        if native_balance < min_native_required:
            logger.error(f"✗ Insufficient native token: required={min_native_required:.6f}, available={native_balance:.6f}")
            return (
                False, None, None,
                f"Insufficient {config['native_token']} balance for gas.\n"
                f"Required: {min_native_required:.6f} {config['native_token']}\n"
                f"Available: {native_balance:.6f} {config['native_token']}\n"
                f"Shortage: {min_native_required - native_balance:.6f} {config['native_token']}"
            )
        logger.info(f"✓ Native token balance sufficient")
        logger.info(f"=== All checks passed, proceeding with transactions ===")
        
        # All checks passed - proceed with transactions under wallet/network lock
        approve_tx_hash = None
        transfer_tx_hash = None
        send_lock = await _get_wallet_send_lock(network_key, wallet_address)
        async with send_lock:
            logger.info(f"Transferring {amount_decimal:.6f} USDT to {masked_deposit}")
            try:
                if transfer_tx_hash:
                    logger.info(f"Transfer tx already exists, skip new transfer: {transfer_tx_hash}")
                else:
                    transfer_tx_hash = await asyncio.to_thread(
                        transfer_usdt,
                        w3, network_key, private_key,
                        deposit_address_checksum, resolved_amount_units, dry_run,
                        persist_prepared_tx,
                    )
                
                if dry_run:
                    logger.info(f"[DRY RUN] Transfer step completed (no transaction sent)")
                    logger.info(f"=== Auto-send completed (DRY RUN) ===")
                    return (True, approve_tx_hash, None, "DRY RUN: Would transfer USDT")
                
                if not transfer_tx_hash:
                    logger.error(f"✗ Transfer transaction returned None")
                    return (False, approve_tx_hash, None, "Transfer transaction failed")
                
                logger.info(f"Waiting for transfer transaction confirmation...")
                try:
                    receipt = await asyncio.to_thread(
                        w3.eth.wait_for_transaction_receipt, transfer_tx_hash, timeout=120
                    )
                except TimeExhausted:
                    try:
                        receipt = await asyncio.to_thread(
                            w3.eth.get_transaction_receipt, transfer_tx_hash
                        )
                    except TransactionNotFound:
                        logger.warning(f"Transfer tx pending confirmation: {transfer_tx_hash}")
                        return (False, approve_tx_hash, transfer_tx_hash, f"TX_PENDING:{transfer_tx_hash}")
                    except Exception as receipt_err:
                        logger.warning(f"Transfer tx status unknown, keeping pending: {transfer_tx_hash}, err={receipt_err}")
                        return (False, approve_tx_hash, transfer_tx_hash, f"TX_PENDING:{transfer_tx_hash}")
                except Exception as receipt_err:
                    logger.warning(f"Transfer tx status unknown, keeping pending: {transfer_tx_hash}, err={receipt_err}")
                    return (False, approve_tx_hash, transfer_tx_hash, f"TX_PENDING:{transfer_tx_hash}")
                if receipt.status != 1:
                    logger.error(f"✗ Transfer transaction failed: {transfer_tx_hash}")
                    return (False, approve_tx_hash, transfer_tx_hash, "Transfer transaction failed")
                
                logger.info(f"✓ Transfer transaction confirmed: {transfer_tx_hash}, block={receipt.blockNumber}")
                logger.info(f"=== Auto-send completed successfully ===")
                
                # Clear private key from memory (best effort)
                private_key = None
                del private_key
                
                return (True, approve_tx_hash, transfer_tx_hash, "")
                
            except TransactionBroadcastUncertain as e:
                transfer_tx_hash = e.tx_hash
                logger.warning("Transfer broadcast status unknown: %s", transfer_tx_hash)
                return (False, approve_tx_hash, transfer_tx_hash, f"TX_PENDING:{transfer_tx_hash}")
            except PreparedTransactionConflict as e:
                logger.warning("Transfer persistence conflict; keeping order unresolved: %s", e)
                return (False, approve_tx_hash, None, f"PERSISTENCE_CONFLICT:{e}")
            except Exception as e:
                logger.error(f"✗ Transfer failed: {e}")
                return (False, approve_tx_hash, transfer_tx_hash, f"Transfer failed: {e}")
    
    except Exception as e:
        logger.error(f"Error in auto_send_usdt: {e}", exc_info=True)
        return (False, None, None, f"Unexpected error: {e}")
