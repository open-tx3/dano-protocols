#!/usr/bin/env python3
"""Semantic check of Danogo CLMM single-pool swaps against recorded fixtures.

Decodes each fixture transaction with its resolved inputs, recomputes the
Danogo CLMM SDK swap (pool successor, fees, curve output, redeemers, epoch
withdrawal) and asserts the expected effects. Every negative fixture must fail
for its named reason. Also checks that the exported TII, the preprod profile,
the invoke args and the README agree, and that the generated fixtures were
built from the current `main.tx3` (by TIR digest). Standard library only.
"""
import copy
import hashlib
import json
import re
import sys
from math import isqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "dano-clmm"
FIXTURES = PROTOCOL / "fixtures"
TX = "swap_single_pool"
SWAP_ACTION = 3
BASE = 10_000
MAX_VALIDITY_SLOTS = 360  # the pool validator rejects a wider validity interval
POSITIVE = ("reference-swap.json", "generated-swap.json", "generated-reward-swap.json")


class Reject(Exception):
    """A fixture breaks an expected effect; the message is the reason id."""


class Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


def cbor(data, pos=0):
    """Decode one CBOR item; returns (value, next_pos)."""
    head = data[pos]
    major, info = head >> 5, head & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        arg = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    elif info == 31:
        arg = None
    else:
        raise ValueError(f"unsupported CBOR header {head:#x}")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if arg is None:
            chunks = []
            while data[pos] != 0xFF:
                chunk, pos = cbor(data, pos)
                chunks.append(chunk)
            return (b"".join(chunks) if major == 2 else "".join(chunks)), pos + 1
        raw = data[pos:pos + arg]
        return (raw if major == 2 else raw.decode()), pos + arg
    if major in (4, 5):
        items = []
        while (arg is None and data[pos] != 0xFF) or (arg is not None and len(items) < arg * (major - 3)):
            item, pos = cbor(data, pos)
            items.append(item)
        if arg is None:
            pos += 1
        if major == 4:
            return items, pos
        return dict(zip(map(hashable, items[0::2]), items[1::2])), pos
    if major == 6:
        value, pos = cbor(data, pos)
        if arg == 258:  # set
            return value, pos
        if arg == 2:  # bignum
            return int.from_bytes(value, "big"), pos
        return Tag(arg, value), pos
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise ValueError(f"unsupported CBOR major type {major}")


def hashable(key):
    return tuple(key) if isinstance(key, list) else key


def decode(hex_str):
    return cbor(bytes.fromhex(hex_str))[0]


def plutus(value):
    """Plutus data as nested ("constr", index, fields) / int / bytes / list."""
    if isinstance(value, Tag):
        if 121 <= value.tag <= 127:
            return ("constr", value.tag - 121, [plutus(f) for f in value.value])
        raise ValueError(f"unexpected tag {value.tag} in Plutus data")
    if isinstance(value, list):
        return [plutus(v) for v in value]
    return value


def output(raw):
    """Normalize a transaction output (map or legacy array form)."""
    if isinstance(raw, dict):
        address, value, datum_option, script_ref = raw[0], raw[1], raw.get(2), raw.get(3)
    else:
        address, value, datum_option, script_ref = raw[0], raw[1], None, None
    coin, multi = (value, {}) if isinstance(value, int) else (value[0], value[1])
    datum = None
    if datum_option is not None:
        if datum_option[0] != 1:
            raise Reject("inline-datum")
        datum = plutus(cbor(datum_option[1].value)[0])
    script_hash = None
    if script_ref is not None:
        script = cbor(script_ref.value)[0]
        script_hash = script_hash_of(script)
    return {
        "address": address.hex(),
        "lovelace": coin,
        "assets": {(p.hex(), n.hex()): q for p, names in multi.items() for n, q in names.items()},
        "datum": datum,
        "script_hash": script_hash,
    }


def script_hash_of(script):
    kind, body = script
    return hashlib.blake2b(bytes([kind]) + body, digest_size=28).hexdigest()


def ref_str(ref):
    return f"{ref[0].hex()}#{ref[1]}"


def sort_refs(refs):
    return sorted(refs, key=lambda r: (bytes.fromhex(r.split("#")[0]), int(r.split("#")[1])))


def load_tx(fx):
    """Decode a fixture into inputs, reference inputs, outputs, withdrawals and redeemers."""
    tx = decode(fx["tx_cbor"])
    body, witnesses = tx[0], tx[1]
    resolved_in = [ref_str(r) for r in decode(fx["resolved_inputs"])]
    resolved_out = [output(o) for o in decode(fx["resolved_outputs"])]
    utxos = dict(zip(resolved_in, resolved_out))
    redeemers = {}
    raw = witnesses.get(5, {})
    if isinstance(raw, dict):
        for (tag, index), (data, _units) in raw.items():
            redeemers[(tag, index)] = plutus(data)
    else:
        for tag, index, data, _units in raw:
            redeemers[(tag, index)] = plutus(data)
    return {
        "inputs": sort_refs([ref_str(r) for r in body[0]]),
        "references": sort_refs([ref_str(r) for r in body.get(18, [])]),
        "outputs": [output(o) for o in body[1]],
        "fee": body[2],
        "since_slot": body.get(8),
        "until_slot": body.get(3),
        "withdrawals": sorted((k.hex(), v) for k, v in body.get(5, {}).items()),
        "redeemers": redeemers,
        "utxos": utxos,
    }


def need(cond, reason):
    if not cond:
        raise Reject(reason)


def pool_datum(d):
    need(isinstance(d, tuple) and d[1] == 0 and len(d[2]) == 12, "pool-datum")
    f = d[2]
    return {
        "token_x": (f[0][0].hex(), f[0][1].hex()),
        "token_y": (f[1][0].hex(), f[1][1].hex()),
        "lp_fee_rate": f[2], "platform_fee_x": f[3], "platform_fee_y": f[4], "total_swap_fee": f[5],
        "sqrt_lower": (f[6][2][0], f[6][2][1]), "sqrt_upper": (f[7][2][0], f[7][2][1]),
        "min_x_change": f[8], "min_y_change": f[9], "circulating_lp": f[10], "last_withdraw_epoch": f[11],
    }


# ceil_div and sdk_quote are ported from the Danogo CLMM SDK, src/utils.ts at
# github.com/dano-finance/clmm-sdk@0416dc1 (npm danogo-clmm 0.1.6), under the
# MIT License: Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including without
# limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom
# the Software is furnished to do so, subject to the following conditions: The
# above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
def ceil_div(a, b):
    q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
    r = a - q * b
    return q if r == 0 or (a < 0) != (b < 0) else q + 1


def sdk_quote(reserve_x, reserve_y, d, delta, platform_fee_rate, reward=0):
    """`calculateConcentratedPoolSwap` from the Danogo CLMM SDK (src/utils.ts)."""
    amount_in = abs(delta)
    excluded_ada = 3_000_000 + d["total_swap_fee"] if d["token_x"] == ("", "") else 0
    x = reserve_x - d["platform_fee_x"] + reward - excluded_ada
    y = reserve_y - d["platform_fee_y"]
    pa, pb = d["sqrt_lower"], d["sqrt_upper"]
    den_ab, num_ab = pa[1] * pb[1], pa[0] * pb[0]
    root = isqrt((y * den_ab - x * num_ab) ** 2 + 4 * x * y * pa[1] * pa[1] * pb[0] * pb[0])
    liq = (y * den_ab + x * num_ab + root, 2 * (pb[0] * pa[1] - pb[1] * pa[0]))
    xv = ceil_div(liq[0] * pb[1], liq[1] * pb[0]) + x
    yv = ceil_div(liq[0] * pa[0], liq[1] * pa[1]) + y
    t_in, t_out, real_out = (xv, yv, y) if delta > 0 else (yv, xv, x)
    lp_fee = amount_in * d["lp_fee_rate"] // BASE
    platform_fee = lp_fee * platform_fee_rate // BASE
    denominator = t_in * BASE + amount_in * (BASE - d["lp_fee_rate"])
    out = (t_out * denominator - t_in * t_out * BASE) // denominator
    need(out <= real_out, "pool-out-exceeded")
    return out, platform_fee


def danogo_epoch(slot, dep):
    ep, sl = dep["danogo_epoch"], dep["slot_config"]
    posix_ms = sl["zero_time_ms"] + (slot - sl["zero_slot"]) * sl["slot_length_ms"]
    return (posix_ms - ep["boundary_ms"]) // ep["epoch_length_ms"] + ep["boundary_epoch"]


def swap_bytes(first, pool_index, delta):
    return bytes([first, SWAP_ACTION, pool_index, 0]) + (delta % (1 << 256)).to_bytes(32, "big")


def check_swap(tx, dep, exp):
    """Raise Reject naming the first effect that breaks the expectation; return decoded effects."""
    pool_hash = dep["pool_script"]["hash"]
    pools = [r for r in tx["inputs"] if tx["utxos"][r]["address"][2:58] == pool_hash]
    need(len(pools) == 1, "single-pool")
    pool_ref = pools[0]
    need(pool_ref == exp["pool_ref"], "pool-out-ref")
    pool_in = tx["utxos"][pool_ref]
    d_in = pool_datum(pool_in["datum"])
    nft = [k for k in pool_in["assets"] if k[0] == pool_hash]
    need(len(nft) == 1 and pool_in["assets"][nft[0]] == 1, "pool-nft")
    need(d_in["token_x"] == ("", ""), "token-x-ada")

    refs = tx["references"]
    cfg_ref = dep["protocol_config"]["ref"]
    need(cfg_ref in refs, "config-reference")
    need(tx["utxos"][dep["pool_script"]["ref"]]["script_hash"] == pool_hash, "pool-script-reference")
    cfg = tx["utxos"][cfg_ref]["datum"]
    need(cfg[1] == 0 and len(cfg[2]) == 2, "config-datum")
    platform_fee_rate, swap_fee = cfg[2]

    pool_index = tx["inputs"].index(pool_ref)
    config_index = refs.index(cfg_ref)
    amount_in = exp["amount_in"]
    need(tx["redeemers"].get((0, pool_index)) == swap_bytes(pool_index, pool_index, amount_in), "pool-redeemer")

    need(tx["since_slot"] is not None and tx["until_slot"] is not None, "validity-window")
    need(tx["until_slot"] - tx["since_slot"] <= MAX_VALIDITY_SLOTS, "validity-window")
    epoch = danogo_epoch(tx["since_slot"], dep)
    need(epoch == exp["current_epoch"], "epoch")
    check_account = dep["pool_script"]["reward_account"]
    withdrawals = dict(tx["withdrawals"])
    accounts = [a for a, _ in tx["withdrawals"]]
    need(withdrawals.get(check_account) == 0, "pool-check-withdrawal")
    need(tx["redeemers"].get((3, accounts.index(check_account))) == swap_bytes(config_index, pool_index, amount_in),
         "pool-check-redeemer")

    stake_hash = pool_in["address"][58:]
    stake_account = "f0" + stake_hash
    reward = 0
    if epoch > d_in["last_withdraw_epoch"]:
        need(stake_account in withdrawals, "staking-reference")
        need(any(tx["utxos"][r]["script_hash"] == stake_hash for r in refs), "staking-reference")
        reward = withdrawals[stake_account]
        need(reward == exp["staking_reward"], "staking-reward")
        need(tx["redeemers"].get((3, accounts.index(stake_account))) == swap_bytes(pool_index, pool_index, amount_in),
             "staking-redeemer")

    quote, platform_fee = sdk_quote(pool_in["lovelace"], pool_in["assets"].get(d_in["token_y"], 0),
                                    d_in, amount_in, platform_fee_rate, reward)
    need(exp["amount_out"] == quote, "amount-out")

    # The redeemer names output 0 as the pool successor.
    successors = [o for o in tx["outputs"] if o["address"] == pool_in["address"]]
    need(len(successors) == 1 and tx["outputs"][0] is successors[0], "pool-successor")
    pool_out = successors[0]
    need(pool_out["lovelace"] == pool_in["lovelace"] + amount_in + swap_fee + reward, "pool-value")
    moved = {k: v for k, v in pool_in["assets"].items()}
    moved[d_in["token_y"]] -= exp["amount_out"]
    need(pool_out["assets"] == {k: v for k, v in moved.items() if v}, "amount-out")
    d_out = pool_datum(pool_out["datum"])
    want = dict(d_in, platform_fee_x=d_in["platform_fee_x"] + platform_fee,
                total_swap_fee=d_in["total_swap_fee"] + swap_fee, last_withdraw_epoch=epoch)
    need(d_out == want, "pool-datum-update")

    trader = [r for r in tx["inputs"] if r != pool_ref]
    t_addr = {tx["utxos"][r]["address"] for r in trader}
    need(len(t_addr) == 1, "trader-input")
    t_addr = t_addr.pop()
    paid = sum(tx["utxos"][r]["lovelace"] for r in trader)
    got = sum(o["lovelace"] for o in tx["outputs"] if o["address"] == t_addr)
    need(got - paid == -amount_in - swap_fee - tx["fee"], "trader-net-ada")
    y_paid = sum(tx["utxos"][r]["assets"].get(d_in["token_y"], 0) for r in trader)
    y_got = sum(o["assets"].get(d_in["token_y"], 0) for o in tx["outputs"] if o["address"] == t_addr)
    need(y_got - y_paid == exp["amount_out"], "trader-net-token-y")

    return {
        "pool_script": pool_hash,
        "direction": "x-to-y",
        "token_y": d_in["token_y"],
        "redeemer_layout": "first|03|pool_in|pool_out=00|delta32",
        "platform_fee_rate": platform_fee_rate,
        "swap_fee": swap_fee,
        "staking_withdrawal": stake_account in withdrawals,
        "datum_fields_changed": sorted(k for k in d_out if d_out[k] != d_in[k]),
    }


def apply_patch(fx, patch):
    fx = copy.deepcopy(fx)
    for path, value in patch.items():
        node = fx
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return fx


def tii_path():
    """Locate the TII `trix build` writes for the version in trix.toml."""
    text = (PROTOCOL / "trix.toml").read_text()
    section = re.search(r"^\[protocol\]$(.*?)(?=^\[|\Z)", text, re.M | re.S).group(1)
    meta = dict(re.findall(r'^(scope|name|version)\s*=\s*"([^"]+)"', section, re.M))
    return PROTOCOL / ".tx3/tii" / meta["scope"] / meta["name"] / meta["version"] / "main.tii"


def canonical(value):
    """Decoded CBOR as JSON-ready data with map entries sorted."""
    if isinstance(value, dict):
        return sorted(([canonical(k), canonical(v)] for k, v in value.items()), key=json.dumps)
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Tag):
        return {"tag": value.tag, "value": canonical(value.value)}
    return value


def tir_digest(tii):
    """SHA-256 of the transaction's decoded TIR, so any change to main.tx3 shows.

    tx3c writes some TIR maps in varying key order, so the digest is taken over
    the decoded TIR with map entries sorted, not over its bytes.
    """
    tir = tii["transactions"][TX]["tir"]
    body = json.dumps([tir["version"], tir["encoding"], canonical(decode(tir["content"]))])
    return hashlib.sha256(body.encode()).hexdigest()


def check_args(args, fx, dep):
    """Require the invoke args to be the inputs the generated fixture was built from."""
    for key, value in fx["expected"].items():
        assert args.get(key) == value, f"invoke args {key}={args.get(key)!r} != generated fixture {value!r}"
    tx = load_tx(fx)
    refs = tx["references"]
    derived = {
        "trader_utxo": args.get("trader_utxo") if args.get("trader_utxo") in tx["inputs"] else None,
        "since_slot": tx["since_slot"],
        "until_slot": tx["until_slot"],
        "pool_index": f"{tx['inputs'].index(args['pool_ref']):02x}",
        "config_index": f"{refs.index(dep['protocol_config']['ref']):02x}",
        "delta_word": args["amount_in"].to_bytes(32, "big").hex(),
    }
    for key, value in derived.items():
        assert args.get(key) == value, f"invoke args {key}={args.get(key)!r} != generated tx {value!r}"


def check_interface(dep):
    tii = json.loads(tii_path().read_text())
    tx = tii["transactions"][TX]
    params = set(tx["params"]["properties"])
    profile = tii["profiles"]["preprod"]
    env = {"pool_script_ref": dep["pool_script"]["ref"], "protocol_config_ref": dep["protocol_config"]["ref"]}
    assert profile["environment"] == env, f"preprod env {profile['environment']} != {env}"
    assert profile["parties"].get("poolcheck") == dep["pool_script"]["reward_address"], "preprod PoolCheck"
    args = json.loads((PROTOCOL / "invoke-args" / f"{TX}.json").read_text())
    names = params | set(tii["parties"])
    assert set(args) <= names, f"invoke args not in interface: {set(args) - names}"
    assert set(tx["params"]["required"]) <= set(args), "invoke args miss a required param"
    readme = (PROTOCOL / "README.md").read_text()
    missing = [n for n in params | set(env) | {TX} if f"`{n}`" not in readme]
    assert not missing, f"README does not document {missing}"
    digest = tir_digest(tii)
    for name in (n for n in POSITIVE if n.startswith("generated")):
        fx = json.loads((FIXTURES / name).read_text())
        assert fx.get("tir_sha256") == digest, \
            f"main.tx3 builds TIR {digest}, not {name}'s; regenerate it (see README)"
    check_args(args, json.loads((FIXTURES / "generated-swap.json").read_text()), dep)


def main():
    dep = json.loads((FIXTURES / "deployment.json").read_text())
    failures = 0
    try:
        check_interface(dep)
        print("ok   interface: TII, preprod profile, invoke args and README agree")
    except AssertionError as err:
        print(f"FAIL interface: {err}")
        failures += 1
    decoded = {}
    for name in POSITIVE:
        fx = json.loads((FIXTURES / name).read_text())
        try:
            decoded[name] = check_swap(load_tx(fx), dep, fx["expected"])
            print(f"ok   {name}: {fx['tx_hash']} matches the expected effects")
        except Reject as err:
            print(f"FAIL {name}: {fx['tx_hash']} breaks {err}")
            failures += 1
    if len(decoded) == len(POSITIVE):
        shapes = list(decoded.values())
        if all(shape == shapes[0] for shape in shapes):
            print("ok   generated and reference swaps have the same decoded shape")
        else:
            print(f"FAIL shape differs: {decoded}")
            failures += 1
    for path in sorted((FIXTURES / "negative").glob("*.json")):
        neg = json.loads(path.read_text())
        fx = json.loads((FIXTURES / neg["base"]).read_text()) if "base" in neg else neg
        fx = apply_patch(fx, neg.get("patch", {}))
        try:
            check_swap(load_tx(fx), dep, fx["expected"])
            print(f"FAIL negative {path.stem}: accepted, expected rejection for {neg['reason']}")
            failures += 1
        except Reject as err:
            if str(err) == neg["reason"]:
                print(f"ok   negative {path.stem}: rejected for {err}")
            else:
                print(f"FAIL negative {path.stem}: rejected for {err}, expected {neg['reason']}")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
