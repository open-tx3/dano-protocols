# dano-protocols

Tx3 models of Dano Finance protocols.

| Protocol | Operation | Network |
|---|---|---|
| [`dano-clmm`](dano-clmm/) | `swap_single_pool`: one swap through one Danogo concentrated-liquidity pool | preprod |

Each protocol directory holds its `main.tx3`, `trix.toml`, per-network
profile, invoke-args example and checked-in comparison fixtures. CI runs
`trix check`, `trix build` and the comparison script on pull requests, `main`
and version tags.

Licensed under Apache-2.0, see [LICENSE](LICENSE).
