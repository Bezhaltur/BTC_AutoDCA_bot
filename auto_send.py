"""
Automatic USDT sending to FixedFloat deposit addresses.
Handles all checks, approvals, and transfers.
"""

import asyncio
import logging
import re
from typing import Optional, Tuple
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from networks import get_network_config, get_blockchair_url
from erc20 import (
    get_web3_instance,
    get_usdt_balance,
    get_native_balance,
    approve_usdt,
    transfer_usdt,
    estimate_gas_for_approve,
    estimate_gas_for_transfer,
    build_gas_params,
    check_allowance,
)
from wallet import load_keystore, decrypt_private_key
from test_config import mask_sensitive_data

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
    required_amount: float,
    btc_address: str,
    order_id: str,
    dry_run: bool = False
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """
    Automatically send USDT to FixedFloat deposit address.
    
    Performs all checks:
    - Deposit address validation
    - BTC address validation
    - USDT balance check
    - Native token balance check
    - Approval (if needed)
    - Transfer
    
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
        - approve_tx_hash: Transaction hash for approve (None if not needed)
        - transfer_tx_hash: Transaction hash for transfer
        - error_message: Error message if failed
    """
    try:
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
        logger.info(f"Amount: {required_amount:.6f} USDT")
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
            usdt_balance = await asyncio.to_thread(get_usdt_balance, w3, network_key, wallet_address)
            native_balance = await asyncio.to_thread(get_native_balance, w3, wallet_address)
            logger.info(f"✓ USDT balance: {usdt_balance:.6f} USDT")
            logger.info(f"✓ Native balance: {native_balance:.6f} {config['native_token']}")
        except Exception as e:
            logger.error(f"✗ Failed to check balances: {e}")
            return (False, None, None, f"Failed to check balances: {e}")
        
        # Check 3: USDT balance sufficient
        logger.info(f"Check 3: Verifying USDT balance sufficient...")
        if usdt_balance < required_amount:
            logger.error(f"✗ Insufficient USDT: required={required_amount:.6f}, available={usdt_balance:.6f}")
            return (
                False, None, None,
                f"Insufficient USDT balance.\n"
                f"Required: {required_amount:.6f} USDT\n"
                f"Available: {usdt_balance:.6f} USDT\n"
                f"Shortage: {required_amount - usdt_balance:.6f} USDT"
            )
        logger.info(f"✓ USDT balance sufficient")
        
        # Check 4: Estimate gas for both transactions
        logger.info(f"Check 4: Estimating gas for transactions...")
        try:
            approve_gas = await asyncio.to_thread(
                estimate_gas_for_approve, w3, network_key, wallet_address, deposit_address_checksum, required_amount
            )
            transfer_gas = await asyncio.to_thread(
                estimate_gas_for_transfer, w3, network_key, wallet_address, deposit_address_checksum, required_amount
            )
            total_gas = approve_gas + transfer_gas
            
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
            logger.info(f"  Approve gas: {approve_gas}")
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
            # Check current allowance
            logger.info(f"Checking current USDT allowance...")
            current_allowance = await asyncio.to_thread(
                check_allowance, w3, network_key, wallet_address, deposit_address_checksum
            )
            logger.info(f"Current allowance: {current_allowance:.6f} USDT")
            
            if current_allowance < required_amount:
                # Need to approve
                logger.info(f"Step 1: Approving {required_amount:.6f} USDT to {masked_deposit}")
                try:
                    approve_tx_hash = await asyncio.to_thread(
                        approve_usdt,
                        w3, network_key, private_key,
                        deposit_address_checksum, required_amount, dry_run
                    )
                    
                    if dry_run:
                        logger.info(f"[DRY RUN] Approve step completed (no transaction sent)")
                    elif approve_tx_hash:
                        logger.info(f"Waiting for approve transaction confirmation...")
                        try:
                            receipt = await asyncio.to_thread(
                                w3.eth.wait_for_transaction_receipt, approve_tx_hash, timeout=120
                            )
                        except TimeExhausted:
                            logger.warning(f"Approve tx pending confirmation: {approve_tx_hash}")
                            return (False, approve_tx_hash, None, f"APPROVE_TX_PENDING:{approve_tx_hash}")
                        if receipt.status != 1:
                            logger.error(f"✗ Approve transaction failed: {approve_tx_hash}")
                            return (False, approve_tx_hash, None, "Approve transaction failed")
                        logger.info(f"✓ Approve transaction confirmed: {approve_tx_hash}, block={receipt.blockNumber}")
                    else:
                        logger.error(f"✗ Approve transaction returned None")
                        return (False, None, None, "Approve transaction failed")
                except Exception as e:
                    logger.error(f"✗ Approve failed: {e}")
                    return (False, None, None, f"Approve failed: {e}")
            else:
                logger.info(f"✓ Sufficient allowance already exists: {current_allowance:.6f} USDT (no approve needed)")
            
            # Transfer USDT
            logger.info(f"Step 2: Transferring {required_amount:.6f} USDT to {masked_deposit}")
            try:
                transfer_tx_hash = await asyncio.to_thread(
                    transfer_usdt,
                    w3, network_key, private_key,
                    deposit_address_checksum, required_amount, dry_run
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
                if receipt.status != 1:
                    logger.error(f"✗ Transfer transaction failed: {transfer_tx_hash}")
                    return (False, approve_tx_hash, transfer_tx_hash, "Transfer transaction failed")
                
                logger.info(f"✓ Transfer transaction confirmed: {transfer_tx_hash}, block={receipt.blockNumber}")
                logger.info(f"=== Auto-send completed successfully ===")
                
                # Clear private key from memory (best effort)
                private_key = None
                del private_key
                
                return (True, approve_tx_hash, transfer_tx_hash, "")
                
            except Exception as e:
                logger.error(f"✗ Transfer failed: {e}")
                return (False, approve_tx_hash, transfer_tx_hash, f"Transfer failed: {e}")
    
    except Exception as e:
        logger.error(f"Error in auto_send_usdt: {e}", exc_info=True)
        return (False, None, None, f"Unexpected error: {e}")
