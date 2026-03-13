# Testing Modes

The bot supports multiple testing modes for safe development and testing without risking real funds.

## Test Modes

### 1. Dry-Run Mode

**Purpose**: Test transaction logic without broadcasting to blockchain.

**Configuration**: Set `DRY_RUN=true` in `.env`

**Behavior**:
- All transaction logic is executed (gas estimation, balance checks, transaction building)
- Transactions are signed but **NOT broadcast** to the network
- Detailed logs show what would happen
- No real transactions are sent

**Use Cases**:
- Testing transaction flow
- Verifying gas estimates
- Checking balance validations
- Testing without spending gas

**Example Log Output**:
```
[DRY RUN] Approve transaction prepared:
  Network: USDT-ARB
  From: 0x1234...abcd
  Spender: 0x5678...efgh
  Amount: 50.000000 USDT
  Gas: 46500
  Gas Price: 0.10 Gwei
  Estimated Cost: 0.00000465 ETH
  [DRY RUN] Transaction NOT sent - would approve 50.000000 USDT
```

### 2. Mock FixedFloat Mode

**Purpose**: Test bot logic without calling real FixedFloat API.

**Configuration**: Set `MOCK_FIXEDFLOAT=true` in `.env`

**Behavior**:
- FixedFloat API calls return mocked responses
- Mock order IDs, deposit addresses, and amounts are generated
- No real API calls are made
- Useful for testing without API keys or rate limits

**Mock Responses**:
- `ccies`: Returns mock list of supported currencies
- `price`: Returns mock price/limits data
- `create`: Returns mock order with generated deposit address

**Use Cases**:
- Testing without FixedFloat API keys
- Avoiding API rate limits during development
- Testing error handling
- Integration testing

**Example Log Output**:
```
[MOCK] FixedFloat API запрос: create с параметрами {...}
[MOCK] FixedFloat ответ: create, order_id=TESTA1B2C3D4E5F6
```

### 3. Testnet Mode

**Purpose**: Use testnet networks instead of mainnet.

**Configuration**: Set `USE_TESTNET=true` in `.env`

**Supported Testnets**:
- **Arbitrum Sepolia** (Chain ID: 421614)
- **BSC Testnet** (Chain ID: 97)
- **Polygon Mumbai** (Chain ID: 80001)

**Behavior**:
- All network configurations switch to testnet
- RPC endpoints point to testnet nodes
- Explorer links point to testnet explorers
- Test tokens can be used (no real value)

**Use Cases**:
- Testing with real blockchain (but testnet)
- Verifying transaction execution
- Testing gas costs on real network
- Integration testing with test tokens

**Note**: Testnet USDT contracts may not exist or may be different. The bot uses placeholder addresses in testnet mode.

## Combined Modes

You can combine multiple test modes:

```env
DRY_RUN=true
MOCK_FIXEDFLOAT=true
USE_TESTNET=true
```

This configuration:
- Uses testnet networks
- Mocks FixedFloat API
- Doesn't broadcast transactions

Perfect for comprehensive testing without any external dependencies.

## Logging

All test modes include enhanced logging:

### Sensitive Data Masking
- Private keys: `0x***...***`
- Addresses: `0x1234...abcd` (first 6 + last 4 chars)
- Passwords: `***MASKED***`

### Detailed Transaction Logs
- Gas estimation details
- Balance checks
- Transaction preparation steps
- Success/failure status

### Example Log Flow
```
=== Auto-send USDT started ===
Order ID: TEST123
Network: USDT-ARB
Wallet: 0x1234...abcd
Deposit: 0x5678...efgh
Amount: 50.000000 USDT
Dry-run: True

Check 1: Validating deposit address format...
✓ Deposit address valid: 0x5678...efgh

Check 2: Checking balances...
Balance check: 0x1234...abcd on USDT-ARB = 100.000000 USDT
✓ USDT balance: 100.000000 USDT
✓ Native balance: 0.100000 ETH

Check 3: Verifying USDT balance sufficient...
✓ USDT balance sufficient

Check 4: Estimating gas for transactions...
Gas estimation (approve): USDT-ARB, from=0x1234...abcd, spender=0x5678...efgh, amount=50.000000 USDT, gas=46500
Gas estimation (transfer): USDT-ARB, from=0x1234...abcd, to=0x5678...efgh, amount=50.000000 USDT, gas=65000
✓ Gas estimation complete:
  Approve gas: 46500
  Transfer gas: 65000
  Total gas: 111500
  Gas price: 0.10 Gwei
  Estimated cost: 0.00001115 ETH
  Required (with margin): 0.00001673 ETH

Check 5: Verifying native token balance sufficient...
✓ Native token balance sufficient
=== All checks passed, proceeding with transactions ===

Step 1: Approving 50.000000 USDT to 0x5678...efgh
[DRY RUN] Approve transaction prepared:
  Network: USDT-ARB
  From: 0x1234...abcd
  Spender: 0x5678...efgh
  Amount: 50.000000 USDT
  Gas: 46500
  Gas Price: 0.10 Gwei
  Estimated Cost: 0.00000465 ETH
  [DRY RUN] Transaction NOT sent - would approve 50.000000 USDT

Step 2: Transferring 50.000000 USDT to 0x5678...efgh
[DRY RUN] Transfer transaction prepared:
  Network: USDT-ARB
  From: 0x1234...abcd
  To: 0x5678...efgh
  Amount: 50.000000 USDT
  Gas: 65000
  Gas Price: 0.10 Gwei
  Estimated Cost: 0.00000650 ETH
  [DRY RUN] Transaction NOT sent - would transfer 50.000000 USDT

=== Auto-send completed (DRY RUN) ===
```

## Safety Guarantees

**No real transactions are sent when:**
- `DRY_RUN=true` is set
- Any test mode is enabled and properly configured

**Verification**:
- All test modes log their status at bot startup
- Transaction logs clearly indicate `[DRY RUN]` or `[MOCK]`
- No transaction hashes are generated in dry-run mode

## Configuration Examples

### Development Testing
```env
DRY_RUN=true
MOCK_FIXEDFLOAT=true
USE_TESTNET=false
```

### Testnet Integration Testing
```env
DRY_RUN=false
MOCK_FIXEDFLOAT=false
USE_TESTNET=true
```

### Full Mock Testing
```env
DRY_RUN=true
MOCK_FIXEDFLOAT=true
USE_TESTNET=true
```

## Troubleshooting

### "Transaction sent" in dry-run mode
- Check that `DRY_RUN=true` is set correctly
- Verify no typos in `.env` file
- Restart bot after changing `.env`

### Mock responses not working
- Ensure `MOCK_FIXEDFLOAT=true` in `.env`
- Check logs for `[MOCK]` prefix
- Verify `test_config.py` is imported correctly

### Testnet connection issues
- Verify testnet RPC endpoints are accessible
- Check network connectivity
- Some testnets may have rate limits

## Best Practices

1. **Always test in dry-run first** before using real networks
2. **Use testnet for integration testing** to verify real blockchain behavior
3. **Combine modes** for comprehensive testing
4. **Check logs carefully** to verify test mode is active
5. **Never commit `.env`** with test modes enabled to production
