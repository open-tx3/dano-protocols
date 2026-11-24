# Danogo CLMM `swap_single_pool` — comparison report

Compares the transaction built by
`trix invoke --profile preprod --skip-submit --args-json-path invoke-args/swap_single_pool.json`
(trix 0.28.0, tx3c 0.25.0) with a preprod swap built by the Danogo CLMM SDK
(`dano-finance/clmm-sdk@0416dc1`, `submitSwap` with one pool entry).
[`scripts/check-swap.py`](../../scripts/check-swap.py) asserts every row marked
"checked" from the fixtures; the rest are read from the decoded transactions.

## Transactions

| | Reference (SDK) | Generated (this interface) |
|---|---|---|
| Hash | `4ea0cf1f96a1c6db0b860b7c1697dc876f3c4a9639afcfdc0f1a360ff06c7675` | `e04d43c3b0ea63cc1ee46f182e69ca1ba5d9650c2e75e298adbe8fa884b60d4f` (unsigned, never submitted) |
| Network, slot | preprod, block 5202570, slot 134295418 | preprod, validity 139869410–139869770 |
| Fixture | [`reference-swap.json`](../fixtures/reference-swap.json) | [`generated-swap.json`](../fixtures/generated-swap.json) |
| Pool UTxO | `8dc90f23…1ba9#0` | `db368ff5…406d#0` (same pool, NFT `5f3deeda…19f62d`, later state) |
| Trader | the SDK user's wallet | a public preprod wallet; the build is never signed |

## Decoded effects

| Field | Reference | Generated | Match |
|---|---|---|---|
| Spent pool | ADA/fUSDM, validator `04041c3c…99932a2` | same | checked |
| Direction | X → Y (ADA in, fUSDM out) | same | checked |
| `amount_in` | 1 000 000 lovelace | 1 000 000 lovelace | checked |
| Swap fee to pool | 100 000 lovelace | 100 000 lovelace | checked |
| `amount_out` | 306 721 fUSDM = SDK curve output | 366 101 fUSDM = SDK curve output for the later reserves | checked (each against its own pool input) |
| Pool lovelace | 1 618 820 092 → 1 619 920 092 (+1 100 000) | 485 598 217 → 486 698 217 (+1 100 000) | checked |
| Pool fUSDM | 697 289 402 → 696 982 681 | 1 085 083 644 → 1 084 717 543 | checked |
| Datum `platform_fee_x` | 0 → 300 | 85 200 → 85 500 | checked (+((1 000 000 × 30) / 10000 × 1000) / 10000) |
| Datum `total_swap_fee` | 300 000 → 400 000 | 15 500 000 → 15 600 000 | checked |
| Datum `last_withdraw_epoch` | 79260 → 79261 | 79740 → 82357 | checked (Danogo epoch at the validity start) |
| Other datum fields | unchanged | unchanged | checked |
| Trader net | +306 721 fUSDM, −1 613 830 lovelace (1 000 000 + 100 000 + fee 513 830) | +366 101 fUSDM, −1 100 000 lovelace − placeholder fee | checked |
| Reference inputs | pool script `2e19…d136#0`, config `3775…68a4#0`, staking `84df…1666#1` | same three | checked |
| Withdrawals | 0 from pool validator, 0 from staking script `889df4c3…bce8` | same | checked |
| Spend redeemer | `00 03 00 00 ‖ 0x…0f4240` | same bytes | checked |
| Swap-check redeemer | `01 03 00 00 ‖ 0x…0f4240` (config is reference input 1) | same bytes | checked |
| Staking redeemer | `00 03 00 00 ‖ 0x…0f4240` | same bytes | checked |
| Required signers | none | none | yes |

## Script evaluation

`aiken tx simulate` (aiken 1.1.21) over each transaction and its resolved
inputs (`resolved_inputs` / `resolved_outputs` in the fixtures, the exact
output CBOR from the producing transactions):

| Transaction | Result |
|---|---|
| Reference `4ea0cf1f…` | passes: spend 57 912 mem / 19.7M cpu, swap check 855 853 / 279.5M, staking 50 802 / 19.2M |
| Generated `e04d43c3…` | passes with the same budgets |
| `amount_out` 366 102 (curve + 1), [negative fixture](../fixtures/negative/wrong-minimum.json) | swap check fails |
| `amount_out` 366 100 and 365 101 (below the curve) | swap check fails: the pool takes only the exact output |
| No staking withdrawal, [negative fixture](../fixtures/negative/absent-epoch-reference.json) | swap check fails |

```bash
aiken tx simulate --zero-time 1655683200000 --zero-slot 0 --slot-length 1000 \
  tx.hex inputs.hex outputs.hex
```

with the three files written from a fixture's `tx_cbor`, `resolved_inputs`
and `resolved_outputs`.

## Expected differences (not errors)

1. **No metadata.** The SDK attaches label 674 `{"msg": ["Danogo Multi-Pool Swap"]}`.
   Tx3 metadata takes scalar values only, so the interface omits it. No script reads it.
2. **Datum encoding.** The SDK writes the pool datum with indefinite-length
   arrays, Tx3 with definite-length ones: same Plutus data, different bytes.
   The validator accepts both.
3. **Placeholder fee and execution units.** `--skip-submit` does not evaluate
   scripts: each redeemer carries 2 000 000 mem / 2 000 000 000 steps and the
   fee is sized for them (1 696 521 lovelace). Real budgets are in the table above.
4. **Collateral return.** The SDK adds a collateral return output and total
   collateral; the TRP does not yet. The generated transaction uses a separate
   ADA-only trader UTxO as collateral.
5. **`network_id`.** Present in the generated body (field 15), absent in the reference.
6. **Inputs, amounts and slots.** The pool state, trader wallet and time differ,
   so the output amount, epoch and indices are recomputed for the later state.

## Reproduce

```bash
cd dano-clmm
trix build
trix invoke --profile preprod --skip-submit --args-json-path invoke-args/swap_single_pool.json
python3 ../scripts/check-swap.py
```

A re-run of `trix invoke` resolves the pinned UTxOs live; it fails once the
pool UTxO in the example is spent. Refresh the live inputs as the
[README](../README.md#inputs) describes.
