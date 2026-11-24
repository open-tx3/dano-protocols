# Danogo CLMM — single-pool swap

[Dano Finance](https://cardano.org/apps/dano-finance/) runs Danogo, a Cardano
DeFi platform with concentrated-liquidity pools (CLMM). Each pool holds two
tokens, X and Y, and prices swaps on a curve bounded by two square-root prices.
A swap spends the pool UTxO and pays it back with the new reserves in the same
transaction: there is no order or batcher step.

This interface covers one swap through one pool on **preprod**:
`swap_single_pool` pays ADA (the pool's token X) into an ADA pool and takes its
token Y. It mirrors `submitSwap` of the Danogo CLMM SDK for a request with one
pool entry. Routing over several pools, the Y-to-X direction, liquidity and
other networks are out of scope.

Source: [`dano-finance/clmm-sdk@0416dc1`](https://github.com/dano-finance/clmm-sdk/tree/0416dc1f3531ce77eec7664ea3947f192a3aca40)
(npm `danogo-clmm@0.1.6`, MIT):
[swap builder](https://github.com/dano-finance/clmm-sdk/blob/0416dc1f3531ce77eec7664ea3947f192a3aca40/src/sdk.ts#L196-L399),
[curve](https://github.com/dano-finance/clmm-sdk/blob/0416dc1f3531ce77eec7664ea3947f192a3aca40/src/utils.ts#L18-L117),
[redeemer](https://github.com/dano-finance/clmm-sdk/blob/0416dc1f3531ce77eec7664ea3947f192a3aca40/src/redeemer.ts#L36-L125),
[preprod constants](https://github.com/dano-finance/clmm-sdk/blob/0416dc1f3531ce77eec7664ea3947f192a3aca40/src/constants.ts#L11-L17).
Tagged release source: [`v0.1.0`](https://github.com/open-tx3/dano-protocols/tree/v0.1.0/dano-clmm)
(the tag is created when the version is published).

## Operation

| Transaction | Network | What it does |
|---|---|---|
| `swap_single_pool` | preprod | Spends the pool UTxO and recreates it with `amount_in` + swap fee more lovelace and `amount_out` less token Y, updates the pool datum, and pays `amount_out` token Y to the trader. Runs the pool validator (spend), its withdraw-zero swap check, and the pool's staking script. |

Only the trader signs. The pool is checked by script, not by a key.

The pool validator accepts only the exact curve output for `amount_in`, so
`amount_out` is the SDK quote (`calculateSwapOut`), not a minimum. The SDK's
`minOutChangeAmount` is a client-side guard: compare the quote with your
minimum before building, as `submitSwap` does.

**Epoch state.** Danogo counts its own epochs: on preprod a 30-minute period,
`floor((posix_ms − 1647899091000) / 1800000) + 328`. A pool whose datum
`last_withdraw_epoch` is behind the current Danogo epoch must withdraw its
staking reward in the swap, which needs the staking script's reference UTxO
(`staking_ref`). This interface always includes that withdrawal, so it covers
the pool's **first swap of a Danogo epoch**: on preprod that is the usual case
for a live swap. A second swap through the same pool within one Danogo epoch is
outside this interface.

## Deployment (`preprod` profile)

| Item | Value |
|---|---|
| Pool validator (PlutusV3) | `04041c3c6ba87b33f2c9eb7f7dbeae3b26003c3e199d438bb99932a2` |
| `pool_script_ref` | `2e19cca74e3badcab26aef7574aa1885ba97228a254ca227ba2f79f2b75fd136#0` (holds the pool validator) |
| `protocol_config_ref` | `3775af36f485f9c97101ee5b9b360c34f0f8e12186bc9060f358b7fc8ce468a4#0`, datum: platform fee rate `1000`, swap fee `100000` lovelace |
| `PoolCheck` party | `stake_test17qzqg8pudw58kvlje84h7ld74cajvqpu8cve6suthxvn9gs6230mc` (pool validator reward account) |

The example pool is ADA/fUSDM (token Y `834a15101873b4e1ddfaa830df46792913995d8738dcde34eda27905`.`665553444d`),
NFT `5f3deeda…19f62d`, LP fee rate 30 / 10000, staking script
`889df4c3188b4e2de40317fadc6f71d958d84c33b03cc313be57bce8` (reference
`84df84b1e1365d6cd74cb292120dde51f2bcafe8651bbee2521c3cd706ce1666#1`).
Every coordinate and where it was read is in
[`fixtures/deployment.json`](fixtures/deployment.json).

## Inputs

| Name | Type | Unit | Meaning |
|---|---|---|---|
| `trader` | party | bech32 address | Wallet that pays, receives token Y and change, and provides collateral from a second ADA-only UTxO. |
| `pool` | party | bech32 address | Address of the pool UTxO: pool validator plus the pool's staking script. |
| `poolstaking` | party | bech32 stake address | Reward account of the pool's staking script. |
| `pool_ref` | UtxoRef | `txhash#index` | Current pool UTxO (the SDK's `poolOutRef`). |
| `staking_ref` | UtxoRef | `txhash#index` | UTxO holding the pool's staking script (the SDK's `stakingOutRef`). |
| `trader_utxo` | UtxoRef | `txhash#index` | Trader UTxO that pays. Pinning it fixes the input order the redeemers index. |
| `amount_in` | Int | lovelace | ADA paid into the pool, excluding the swap fee (the SDK's positive `deltaAmount`). |
| `amount_out` | Int | token Y base unit | Token Y taken from the pool: the exact curve output for `amount_in`. |
| `current_epoch` | Int | Danogo epoch | Danogo epoch at `since_slot`; recorded as the pool's `last_withdraw_epoch`. |
| `staking_reward` | Int | lovelace | Balance of the `poolstaking` reward account, withdrawn in full to the trader. |
| `pool_index` | Bytes | 1 byte | Index of `pool_ref` among the transaction inputs sorted by `txhash#index`. |
| `config_index` | Bytes | 1 byte | Index of `protocol_config_ref` among the sorted reference inputs (`pool_script_ref`, `protocol_config_ref`, `staking_ref`). |
| `delta_word` | Bytes | 32 bytes | `amount_in` as a big-endian unsigned integer. |
| `since_slot`, `until_slot` | Int | slot | Validity interval. The SDK uses now − 2 min to now + 4 min. |

Tx3 cannot yet compute an input's position or encode an integer as bytes, so
the caller derives `pool_index`, `config_index` and `delta_word` from the other
inputs. The redeemer of all three scripts is
`first byte | 0x03 | pool_index | 0x00 | delta_word`, where the first byte is
`pool_index` for the pool spend and the staking withdrawal and `config_index`
for the swap check.

[`invoke-args/swap_single_pool.json`](invoke-args/swap_single_pool.json) is a
live example: the ADA/fUSDM pool at preprod slot 139869530, 1 ADA in,
366 101 fUSDM out, Danogo epoch 82357. The pool UTxO changes on every swap, so
refresh `pool_ref`, `amount_out`, `current_epoch`, the slots and the indices
before a live build. The historical swap below is pinned in
[`fixtures/reference-swap.json`](fixtures/reference-swap.json) with every UTxO
it resolved, so its check needs no live chain.

## Expected effects

With `p` the pool input, `c` the protocol configuration datum and
`fee = ((amount_in × p.lp_fee_rate) / 10000 × c.platform_fee_rate) / 10000`
(integer division):

| Output | Value | Datum |
|---|---|---|
| Pool successor at `pool` | `p` + `amount_in` + `c.swap_fee` lovelace − `amount_out` token Y | `p`'s datum with `platform_fee_x + fee`, `total_swap_fee + c.swap_fee`, `last_withdraw_epoch = current_epoch` |
| Trader | net + `amount_out` token Y, − (`amount_in` + `c.swap_fee` + tx fee − `staking_reward`) lovelace | none |

Withdrawals: 0 from `PoolCheck`, `staking_reward` from `poolstaking`.
Reference inputs: `pool_script_ref`, `protocol_config_ref`, `staking_ref`.

Reference swap: preprod transaction
[`4ea0cf1f…6c7675`](https://preprod.cexplorer.io/tx/4ea0cf1f96a1c6db0b860b7c1697dc876f3c4a9639afcfdc0f1a360ff06c7675),
built by the SDK: 1 000 000 lovelace in, 306 721 fUSDM out, platform fee X
+300, total swap fee +100 000, Danogo epoch 79260 → 79261, staking withdrawal 0.
The [comparison report](invoke-args/comparison-report.md) decodes it next to the
transaction this interface builds.

## Use it

```bash
trix use open-tx3/dano-clmm:0.1.0
```

| Language | Package | Generate the client |
|---|---|---|
| TypeScript | [`tx3-sdk`](https://www.npmjs.com/package/tx3-sdk) (npm) | `trix codegen --plugin ts-client` |
| Rust | [`tx3-sdk`](https://crates.io/crates/tx3-sdk) (crates.io) | `trix codegen --plugin rust-client` |
| Python | [`tx3-sdk`](https://pypi.org/project/tx3-sdk/) (PyPI) | `trix codegen --plugin python-client` |
| Go | [`github.com/tx3-lang/go-sdk/sdk`](https://pkg.go.dev/github.com/tx3-lang/go-sdk/sdk) | `trix codegen --plugin go-client` |
| Java | [`land.tx3:tx3-sdk`](https://central.sonatype.com/artifact/land.tx3/tx3-sdk) (Maven Central) | `trix codegen --plugin java-client` |
| Swift | [`Tx3SDK`](https://github.com/tx3-lang/swift-sdk) (Swift Package Manager) | `trix codegen --plugin swift-client` |

To build the unsigned transaction from this directory:

```bash
trix invoke --profile preprod --skip-submit --args-json-path invoke-args/swap_single_pool.json
```

The protocol guide, Fluent tools and Skill for this version are linked here
when they are published.

## Checks

`trix check` and `trix build`, then
[`scripts/check-swap.py`](../scripts/check-swap.py), which decodes the recorded
and the generated swap with their resolved inputs, recomputes the SDK curve
output, fees, datum update, redeemers and epoch withdrawal, and requires the
[negative fixtures](fixtures/negative/) (wrong pool out-ref, wrong minimum,
absent epoch reference) to be rejected.

## License

Apache-2.0, see [LICENSE](../LICENSE). The curve arithmetic in the check
script is ported from the MIT-licensed Danogo CLMM SDK.
