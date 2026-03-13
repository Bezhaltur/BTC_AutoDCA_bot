"""
Wallet keystore management - Single EVM wallet model.
User provides one private key that works across all networks.
Password is stored in OS keyring for persistence.
"""

import json
import os
from typing import Optional
from eth_account import Account
import keyring
import logging

logger = logging.getLogger(__name__)

# Directory for keystore files
KEYSTORE_DIR = "keystores"
os.makedirs(KEYSTORE_DIR, exist_ok=True)

# Keyring service name
KEYRING_SERVICE = "AutoDCA_Bot"


def generate_keystore_path(user_id: int) -> str:
    """
    Generate keystore file path for user (single wallet, not network-specific).
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        Path to keystore file
    """
    filename = f"user_{user_id}_wallet.json"
    return os.path.join(KEYSTORE_DIR, filename)


def save_keystore(keystore: dict, user_id: int) -> str:
    """
    Save keystore to file.
    
    Args:
        keystore: Keystore dictionary (standard Ethereum JSON format)
        user_id: Telegram user ID
    
    Returns:
        Path to saved keystore file
    """
    filepath = generate_keystore_path(user_id)
    
    with open(filepath, "w") as f:
        json.dump(keystore, f, indent=2)
    
    # Set restrictive permissions (owner read/write only)
    os.chmod(filepath, 0o600)
    
    logger.info(f"Keystore saved to {filepath}")
    return filepath


def load_keystore(user_id: int) -> Optional[dict]:
    """
    Load keystore from file.
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        Keystore dictionary or None if not found
    """
    filepath = generate_keystore_path(user_id)
    
    if not os.path.exists(filepath):
        return None
    
    try:
        with open(filepath, "r") as f:
            keystore = json.load(f)
        return keystore
    except Exception as e:
        logger.error(f"Error loading keystore from {filepath}: {e}")
        return None


def decrypt_private_key(keystore: dict, password: str) -> str:
    """
    Decrypt private key from keystore using eth_account.Account.decrypt.
    
    Args:
        keystore: Keystore dictionary (standard Ethereum JSON format)
        password: Decryption password
    
    Returns:
        Private key (hex string with 0x prefix)
    
    Raises:
        ValueError: If password is incorrect or keystore is invalid
    """
    try:
        # Use eth_account.Account.decrypt (standard method)
        private_key = Account.decrypt(keystore, password)
        return private_key.hex()
    except Exception as e:
        logger.error(f"Error decrypting private key: {e}")
        raise ValueError(f"Incorrect password or invalid keystore: {e}")


def get_wallet_address(keystore: dict) -> str:
    """
    Get wallet address from keystore (no password needed).
    
    Args:
        keystore: Keystore dictionary
    
    Returns:
        Wallet address (checksummed)
    """
    # Address is stored in keystore
    address = keystore.get("address")
    if not address:
        raise ValueError("Invalid keystore: no address field")
    
    # Ensure it has 0x prefix and is checksummed
    if not address.startswith("0x"):
        address = "0x" + address
    
    from web3 import Web3
    return Web3.to_checksum_address(address)


def delete_keystore(user_id: int) -> bool:
    """
    Delete keystore file.
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        True if deleted, False if not found
    """
    filepath = generate_keystore_path(user_id)
    
    if os.path.exists(filepath):
        os.remove(filepath)
        logger.info(f"Keystore deleted: {filepath}")
        return True
    
    return False


def keystore_exists(user_id: int) -> bool:
    """Check if keystore exists for user."""
    filepath = generate_keystore_path(user_id)
    return os.path.exists(filepath)


def save_password_to_keyring(user_id: int, password: str) -> None:
    """
    Save password to OS keyring.
    
    Args:
        user_id: Telegram user ID
        password: Wallet password
    """
    username = f"user_{user_id}"
    keyring.set_password(KEYRING_SERVICE, username, password)
    logger.info(f"Wallet password saved to keyring for user {user_id}")


def load_password_from_keyring(user_id: int) -> Optional[str]:
    """
    Load password from OS keyring.
    
    Args:
        user_id: Telegram user ID
    
    Returns:
        Password or None if not found
    """
    username = f"user_{user_id}"
    try:
        password = keyring.get_password(KEYRING_SERVICE, username)
    except keyring.errors.KeyringError as e:
        logger.warning(f"Keyring unavailable for user {user_id}: {e}")
        return None

    if password:
        logger.info(f"Wallet password loaded from keyring for user {user_id}")
    return password


def delete_password_from_keyring(user_id: int) -> None:
    """
    Delete password from OS keyring.
    
    Args:
        user_id: Telegram user ID
    """
    username = f"user_{user_id}"
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
        logger.info(f"Wallet password deleted from keyring for user {user_id}")
    except keyring.errors.PasswordDeleteError:
        pass  # Password was not set
