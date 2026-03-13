# Migration Guide: Network-Specific to Single Wallet Model

## Overview

The bot has been updated from **network-specific wallets** to a **single EVM wallet** model.

## What Changed

### Before (Old Model)
- Separate wallet for each network (USDT-ARB, USDT-BSC, USDT-MATIC)
- Command: `/setwallet NETWORK /path/to/keystore.json PASSWORD`
- Password stored in memory only (lost on restart)
- Network-specific keystore files

### After (New Model)
- **ONE wallet for ALL networks**
- Command: `/setwallet` (no arguments, reads wallet.json)
- Password stored in OS keyring (persists through restarts)
- Single keystore file per user
- Restart-safe auto-send

## Migration Steps

### For Existing Users

**IMPORTANT**: This is a breaking change. You must reconfigure your wallet.

1. **Backup current keystores** (if you want to keep them):
   ```bash
   cp -r keystores keystores_backup
   ```

2. **Note your private key** (from your wallet software like MetaMask)

3. **Run the bot** - it will automatically migrate the database schema

4. **Delete old wallet configuration**:
   ```
   /deletewallet
   ```

5. **Create wallet.json** in bot root:
   ```json
   {
     "private_key": "0xYOUR_PRIVATE_KEY",
     "password": "YOUR_PASSWORD"
   }
   ```

6. **Setup new wallet**:
   ```
   /setwallet
   ```

7. **Verify setup**:
   ```
   /walletstatus
   ```

### Database Migration

The database schema is automatically migrated on bot startup:

**New columns added:**
- `wallets.user_id` - Now unique (no network_key)
- `sent_transactions.state` - Transaction state tracking
- `sent_transactions.error_message` - Error details
- `dca_plans.execution_state` - Plan execution state
- `dca_plans.last_tx_hash` - Last transaction hash

**Old data:**
- Existing wallets table rows will need to be recreated
- DCA plans remain intact
- Transaction history preserved

## Benefits of New Model

### 1. Restart Safety
- Password persists in OS keyring
- Auto-send works after bot restart
- No need to re-enter password

### 2. Idempotency
- Duplicate executions prevented
- State tracking (scheduled, sending, sent, failed, blocked)
- Safe to restart during execution

### 3. RPC Failure Handling
- Retryable errors: Automatically retry
- Non-retryable errors: Fail gracefully
- Blocked transactions recovered on restart

### 4. Simplified Setup
- One wallet for all networks
- Single setup command
- No network-specific configuration

### 5. Better Security
- OS keyring integration (Windows, macOS, Linux)
- No plaintext passwords
- Private key never stored unencrypted

## Removed Commands

The following commands have been removed:

- ❌ `/setpassword NETWORK PASSWORD`
- ❌ `/clearpassword [NETWORK]`

**Reason**: Password now stored in OS keyring automatically.

## Updated Commands

### /setwallet
**Old**: `/setwallet NETWORK /path/to/keystore.json PASSWORD`
**New**: `/setwallet` (no arguments)

Reads wallet.json from project root.

### /deletewallet
**Old**: `/deletewallet NETWORK`
**New**: `/deletewallet` (no arguments)

Deletes the single wallet (all networks).

### /walletstatus
**Old**: Showed each network separately
**New**: Shows single wallet address with balances for all networks

## State Management

### Transaction States

Transactions now track state for idempotency:

- `scheduled` - Queued for execution
- `sending` - Currently being sent
- `sent` - Successfully sent (tx hash recorded)
- `failed` - Non-retryable error (schedule advanced)
- `blocked` - Retryable error (will retry, schedule NOT advanced)

### Recovery on Restart

When bot restarts:
1. Loads password from OS keyring
2. Loads keystore from disk
3. Recovers blocked transactions (resets to scheduled)
4. Continues normal operation

## Troubleshooting Migration

### "Wallet already initialized"
- Old keystore exists
- Solution: `/deletewallet` then re-run `/setwallet`

### Database errors
- Schema mismatch
- Solution: Let auto-migration run (check logs)

### Missing password after restart
- Password not in keyring
- Solution: Re-run `/setwallet` to save password

### Old keystores remain
- Legacy files not automatically deleted
- Solution: Manually delete from `keystores/` if desired

## Rollback (Emergency)

If you need to rollback to old version:

1. Restore from backup:
   ```bash
   git checkout <old_commit>
   cp -r keystores_backup keystores
   ```

2. Restore database:
   ```bash
   cp dca_backup.db dca.db
   ```

**Note**: Not recommended. New model is more robust.

## Testing Your Migration

After migration, test:

1. **Wallet status**:
   ```
   /walletstatus
   ```
   Should show wallet address and balances on all networks.

2. **DCA plan**:
   ```
   /status
   ```
   Existing plans should still be active.

3. **Manual execution**:
   ```
   /execute USDT-ARB
   ```
   Test auto-send functionality.

4. **Restart test**:
   - Stop bot
   - Start bot
   - Check logs for "Wallet password loaded from keyring"
   - Verify auto-send still works

## Support

If you encounter issues during migration:

1. Check logs: `logs/bot.log`
2. Verify database schema: `sqlite3 dca.db ".schema"`
3. Check keystore files: `ls -la keystores/`
4. Verify keyring entry (OS-specific tools)

## Summary

✅ **One wallet** for all networks
✅ **Restart-safe** auto-send
✅ **Idempotent** execution (no duplicates)
✅ **RPC failure** handling
✅ **OS keyring** integration
✅ **Simpler** setup

The new model is more robust and user-friendly!
