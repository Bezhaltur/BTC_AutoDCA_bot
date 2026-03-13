"""
Network configuration for EVM chains.
Centralized configuration for Arbitrum, BSC, and Polygon.
Supports both mainnet and testnet.
"""

import os
from dotenv import load_dotenv

load_dotenv()
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

# Mainnet configurations
NETWORKS_MAINNET = {
    "USDT-ARB": {
        "name": "Arbitrum",
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "chain_id": 42161,
        "native_token": "ETH",
        "usdt_contract": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT on Arbitrum
        "blockchair_base": "https://blockchair.com/bitcoin/transaction",
        "explorer_base": "https://arbiscan.io/tx/",
    },
    "USDT-BSC": {
        "name": "BSC",
        "rpc_url": "https://bsc-dataseed.binance.org/",
        "chain_id": 56,
        "native_token": "BNB",
        "usdt_contract": "0x55d398326f99059fF775485246999027B3197955",  # USDT on BSC
        "blockchair_base": "https://blockchair.com/bitcoin/transaction",
        "explorer_base": "https://bscscan.com/tx/",
    },
    "USDT-MATIC": {
        "name": "Polygon",
        "rpc_url": "https://polygon-rpc.com/",
        "chain_id": 137,
        "native_token": "MATIC",
        "usdt_contract": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",  # USDT on Polygon
        "blockchair_base": "https://blockchair.com/bitcoin/transaction",
        "explorer_base": "https://polygonscan.com/tx/",
    },
}

# Testnet configurations
NETWORKS_TESTNET = {
    "USDT-ARB": {
        "name": "Arbitrum Sepolia",
        "rpc_url": "https://sepolia-rollup.arbitrum.io/rpc",
        "chain_id": 421614,
        "native_token": "ETH",
        "usdt_contract": "0x0000000000000000000000000000000000000000",  # Mock/test contract
        "blockchair_base": "https://blockchair.com/bitcoin/testnet/transaction",
        "explorer_base": "https://sepolia.arbiscan.io/tx/",
    },
    "USDT-BSC": {
        "name": "BSC Testnet",
        "rpc_url": "https://data-seed-prebsc-1-s1.binance.org:8545/",
        "chain_id": 97,
        "native_token": "BNB",
        "usdt_contract": "0x0000000000000000000000000000000000000000",  # Mock/test contract
        "blockchair_base": "https://blockchair.com/bitcoin/testnet/transaction",
        "explorer_base": "https://testnet.bscscan.com/tx/",
    },
    "USDT-MATIC": {
        "name": "Polygon Mumbai",
        "rpc_url": "https://rpc-mumbai.maticvigil.com/",
        "chain_id": 80001,
        "native_token": "MATIC",
        "usdt_contract": "0x0000000000000000000000000000000000000000",  # Mock/test contract
        "blockchair_base": "https://blockchair.com/bitcoin/testnet/transaction",
        "explorer_base": "https://mumbai.polygonscan.com/tx/",
    },
}

# Select networks based on testnet mode
NETWORKS = NETWORKS_TESTNET if USE_TESTNET else NETWORKS_MAINNET


def get_network_config(network_key: str) -> dict:
    """
    Get network configuration by network key.
    
    Args:
        network_key: Network key (e.g., "USDT-ARB")
    
    Returns:
        Network configuration dictionary
    
    Raises:
        ValueError: If network is not supported
    """
    config = NETWORKS.get(network_key)
    if not config:
        raise ValueError(f"Unsupported network: {network_key}. Supported: {list(NETWORKS.keys())}")
    return config


def get_usdt_contract_address(network_key: str) -> str:
    """Get USDT contract address for network."""
    return get_network_config(network_key)["usdt_contract"]


def get_rpc_url(network_key: str) -> str:
    """Get RPC URL for network."""
    return get_network_config(network_key)["rpc_url"]


def get_chain_id(network_key: str) -> int:
    """Get chain ID for network."""
    return get_network_config(network_key)["chain_id"]


def get_native_token(network_key: str) -> str:
    """Get native token symbol for network."""
    return get_network_config(network_key)["native_token"]


def get_blockchair_url(txid: str) -> str:
    """Get Blockchair URL for Bitcoin transaction."""
    return f"https://blockchair.com/bitcoin/transaction/{txid}"
