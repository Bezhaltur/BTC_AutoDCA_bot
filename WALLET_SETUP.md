# Wallet Setup Guide - Single EVM Wallet Model

## Overview

This bot uses a **single EVM wallet** that works across ALL supported networks (Arbitrum, BSC, Polygon). Your private key is encrypted and stored securely using industry-standard Ethereum keystore format.

## Security Model

- **Local-only**: Bot runs on YOUR machine, not in the cloud
- **Private key**: Never stored unencrypted, only in keystore format
- **Password**: Stored in OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service)
- **Restart-safe**: Password persists through bot restarts
- **No cloud sync**: All credentials stay on your machine
- **Equivalent security to**: MetaMask, other always-on software wallets

## One-Time Setup

### Step 1: Create wallet.json

In the bot's root directory, create a file named `wallet.json`:

```json
{
  "private_key": "0xYOUR_PRIVATE_KEY_HERE",
  "password": "YOUR_STRONG_PASSWORD_HERE"
}
```

**Where to get your private key:**
- Export from MetaMask (Account Details → Export Private Key)
- Use an existing EVM wallet private key
- **Must start with 0x**

**Password requirements:**
- Use a strong, unique password
- This will be used to encrypt your private key
- Stored securely in OS keyring

### Step 2: Run /setwallet

In Telegram, send to the bot:
```
/setwallet
```

**What happens:**
1. Bot reads wallet.json
2. Creates encrypted keystore (v3 format)
3. Saves password to OS keyring
4. **Overwrites wallet.json** (removes private key)
5. Confirms setup

### Step 3: Delete any backups

**IMPORTANT**: Delete any backup copies of wallet.json that contain the private key!

The bot has now:
- ✅ Encrypted your private key
- ✅ Stored password in keyring
- ✅ Removed plaintext private key from disk

## Using the Bot

After setup:
1. Create DCA plans: `/setdca USDT-ARB 10 24 bc1xxx...`
2. Bot runs automatically 24/7
3. Auto-sends USDT on schedule
4. BTC arrives at your address

## Restart Behavior

**Bot restart does NOT disable auto-send!**

When bot restarts:
- ✅ Password loaded from keyring
- ✅ Keystore loaded from disk
- ✅ Auto-send continues working
- ✅ Blocked transactions recovered

## Commands

- `/setwallet` - Setup wallet (one time only)
- `/walletstatus` - Check wallet balances
- `/deletewallet` - Remove wallet (use with caution)

## Troubleshooting

### "Wallet already initialized"
- Keystore already exists
- To reset: manually delete keystore file or use `/deletewallet`

### "wallet.json not found"
- Create wallet.json in bot root directory
- Check filename (lowercase, no spaces)

### "Invalid wallet.json format"
- Must contain `private_key` and `password` fields
- Private key must start with `0x`
- Valid JSON format required

### Auto-send not working after restart
- Check logs: "Wallet password loaded from keyring"
- If missing: password not in keyring (re-run /setwallet)
- Check keystore file exists in `keystores/` directory

## Security Best Practices

1. **Never commit wallet.json** (it's in .gitignore)
2. **Use strong password** for keystore encryption
3. **Run bot locally only** (not in cloud)
4. **Keep bot machine secure** (up-to-date, firewall, antivirus)
5. **Regular backups** of keystore + password (separate, secure locations)
6. **Monitor balances** regularly with `/walletstatus`

## Technical Details

### File Locations
- Keystore: `keystores/user_{user_id}_wallet.json`
- Password: OS keyring (service: `AutoDCA_Bot`)
- Database: `dca.db` (contains wallet address, NOT private key)

### Supported Networks
- USDT-ARB (Arbitrum)
- USDT-BSC (Binance Smart Chain)
- USDT-MATIC (Polygon)

Same wallet address on all networks!

### State Management
Transactions are tracked with states:
- `scheduled` - Waiting to execute
- `sending` - Transaction in progress
- `sent` - Successfully sent
- `failed` - Non-retryable error
- `blocked` - RPC error, will retry

### Idempotency
- Duplicate executions prevented
- State checked before each send
- Restart-safe (no duplicates after restart)

### RPC Failure Handling
- **Retryable errors** (timeout, connection): Set to `blocked`, retry on next tick
- **Non-retryable errors** (no balance, revert): Set to `failed`, advance schedule
- **Transaction sent**: Never resend (even if receipt missing)

## FAQ

**Q: Can I use different wallets for different networks?**
A: No, this is a single-wallet model. Same wallet for all networks.

**Q: What if I lose my password?**
A: You cannot decrypt the keystore without the password. Keep backups!

**Q: Is it safe to run 24/7?**
A: Yes, equivalent security to MetaMask. Keep your machine secure.

**Q: Can I change my wallet?**
A: Delete wallet with `/deletewallet`, then run `/setwallet` with new wallet.json

**Q: Where is my private key stored?**
A: Encrypted in `keystores/` directory. Never stored in plaintext.

**Q: What if bot crashes during a transaction?**
A: State is tracked. On restart, blocked transactions are recovered and retried.

## Support

For issues:
1. Check logs: `logs/bot.log`
2. Verify keystore exists: `ls keystores/`
3. Check keyring: OS-specific keyring viewer
4. Review this guide thoroughly
