"""Measure the real atomic-arbitrage market on Base from history, read-only.

Our scanners read prices at block end, but on Base an arb lands *inside* a block, right behind the swap that opened the
gap. So instead of asking "is there a gap now?", this asks "how much did arb bots actually make?". It is read-only:
nothing is signed or sent, and pools with hooks are included (we only look at them).

1. Pull every Swap event of the last N hours (chunked eth_getLogs, adaptive range, resumable via SQLite):
   Uniswap V3 + Slipstream (same event), Aerodrome v2, Uniswap V2-style forks, Uniswap V4 PoolManager (hooks too).
   Pools are labelled by their factory: univ3 / cl1 / cl2 / cl3 / aero / univ4 are venues we trade, the rest "other_*".
2. A tx is a candidate when it swaps on 2+ distinct pools and some token it paid into an earlier swap comes back out
   of a later one (a closed loop). Plain multi-hop trades (A -> B -> C) never qualify.
3. Each candidate's receipt decides. Its net flow per asset is measured at the edge of the venues from the receipt's
   ERC20 Transfer logs: out of the swapped pools / PoolManager minus into them, wherever the profit went (bots often
   pay it to a third address, or flash-borrow from the PoolManager). WETH and native ETH are one asset; native ETH legs
   of V4 pools come from the Swap event. It is an arb when the flow is worth > 0 and its biggest gain is an asset the
   tx also paid in (it ended with more of what it started with).
   Profit is valued at the Uniswap V3 WETH/USDC 0.05% anchor price, then L2 gas and the L1 data fee are subtracted.
4. `--report` prints profit per day, seed vs other tokens, the top tokens and bots, and priority fee vs profit.

    python arb_history.py --hours 24 --rpc https://base.drpc.org      # mainnet.base.org refuses eth_getLogs
    python arb_history.py --report
    python arb_history.py --tx 0x...                                     # explain one transaction

Blind spots (stated in the report): reverted arb attempts leave no Swap logs, so their gas cost is not counted; arbs
whose loop runs mostly through venues we don't pull (PancakeSwap V3, Balancer, Curve...) are missed; profit sent to a
flash-loaned token taxed on transfer is slightly over-counted.
"""
import argparse
import bisect
import glob
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eth_abi import decode, encode
from web3 import Web3

import scanner

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "logs" / "arb_history.db"
BLOCKS_PER_HOUR = 1800          # Base: 2s blocks
CHUNK_BLOCKS = 500              # unit of work committed to the DB (resume point)
FINALITY_LAG = 10               # stay this many blocks behind the head


def topic(sig):
    return "0x" + Web3.keccak(text=sig).hex().removeprefix("0x")


T_V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")       # Uniswap V3, Slipstream, V3 forks
T_AERO = topic("Swap(address,address,uint256,uint256,uint256,uint256)")          # Aerodrome v2, Velodrome-style forks
T_UNI2 = topic("Swap(address,uint256,uint256,uint256,uint256,address)")          # Uniswap V2 and its forks
T_V4 = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")  # Uniswap V4 PoolManager
T_TRANSFER = topic("Transfer(address,address,uint256)")
T_V4_INIT = topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
# Liquidity events. A tx where a swapped pool (or the PoolManager) also mints/burns/collects is an LP rebalance:
# tokens leave the pool with nothing paid in, which the venue-edge flow would book as "profit" (seen: $5k "arbs").
T_LIQUIDITY = {topic(s) for s in (
    "Mint(address,address,int24,int24,uint128,uint256,uint256)", "Burn(address,int24,int24,uint128,uint256,uint256)",
    "Collect(address,address,int24,int24,uint128,uint128)",                            # Uniswap V3 / Slipstream
    "Mint(address,uint256,uint256)", "Burn(address,uint256,uint256,address)",         # Uniswap V2 forks
    "Burn(address,address,uint256,uint256)",                                           # Aerodrome v2
    "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")}                   # Uniswap V4
V4_DEPLOY_BLOCK = 25_350_988    # first block with PoolManager code (binary search on eth_getCode)

L = str.lower
WETH, USDC, ZERO = L(scanner.WETH), L(scanner.USDC), L(scanner.ZERO)
POOL_MANAGER = L(scanner.UNI_V4_POOL_MANAGER)
# Swap sources: name -> (topic0, address filter). V4 is filtered to the PoolManager; the others are pool contracts.
SOURCES = {"v3": (T_V3, None), "aero": (T_AERO, None), "uni2": (T_UNI2, None), "v4": (T_V4, POOL_MANAGER)}
FACTORY_VENUE = {L(scanner.UNI_V3_FACTORY): "univ3", L(scanner.AERO_FACTORY): "aero",
                 **{L(d["factory"]): d["name"].lower() for d in scanner.SLIPSTREAM_DEPLOYMENTS}}
TRADED_VENUES = {"univ3", "aero", "cl1", "cl2", "cl3", "univ4"}       # what the scanner can route through today
BASE_PAIR = "(WETH/USDC)"         # token label of an arb that only touches the two base assets
RATE_LIMIT_HINTS = ("limit", "too many", "rate", "capacity", "timeout", "temporarily")


class RpcError(Exception):
    """A JSON-RPC error that retrying won't fix (bad range, too many results, unsupported...)."""


class Rpc:
    """Minimal JSON-RPC client: retries 429 / 5xx / timeouts with backoff, never shows the URL (it can carry a key).
    `extra` endpoints share the load round-robin (each request goes to the next one; a retry moves on)."""

    def __init__(self, url, max_batch=1, timeout=90, tries=8, extra=()):
        self.url, self.max_batch, self.timeout, self.tries = url, max_batch, timeout, tries
        self.urls = [url, *extra]
        self.calls = 0

    def _post(self, body):
        delay = 1.0
        for attempt in range(self.tries):
            self.calls += 1
            url = self.urls[(self.calls + attempt) % len(self.urls)]
            req = urllib.request.Request(url, json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "User-Agent": "defi-arb-bot/arb_history"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                text = e.read()[:300].decode(errors="replace")
                if e.code != 429 and e.code < 500:
                    try:                       # some providers answer a bad request with 400 + a JSON-RPC error body
                        return json.loads(text)
                    except ValueError:
                        raise RpcError(scanner.scrub(f"HTTP {e.code}: {text}", *self.urls)) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                pass
            if attempt == self.tries - 1:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise ConnectionError(f"RPC {scanner.redact_url(self.url)} kept failing after {self.tries} tries")

    def _unwrap(self, resp, method):
        if "error" in resp:
            err = resp["error"]
            msg = str(err.get("message", err) if isinstance(err, dict) else err)
            raise RpcError(scanner.scrub(f"{method}: {msg}", *self.urls))
        return resp["result"]

    def call(self, method, params):
        for attempt in range(self.tries):
            resp = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if isinstance(resp, list):
                resp = resp[0]
            if "error" in resp and any(h in str(resp["error"]).lower() for h in RATE_LIMIT_HINTS) \
                    and "range" not in str(resp["error"]).lower() and attempt < self.tries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            return self._unwrap(resp, method)

    def batch(self, calls):
        """[(method, params)] -> [result]. Sent max_batch at a time; one item failing raises."""
        out = []
        for i in range(0, len(calls), self.max_batch):
            part = calls[i:i + self.max_batch]
            if len(part) == 1:
                out.append(self.call(*part[0]))
                continue
            resp = self._post([{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(part)])
            if not isinstance(resp, list):                       # provider refused the batch: fall back to singles
                out += [self.call(m, p) for m, p in part]
                continue
            by_id = {r.get("id"): r for r in resp}
            for j, (m, p) in enumerate(part):
                r = by_id.get(j, {"error": {"message": "missing from batch"}})
                out.append(self.call(m, p) if "error" in r else r["result"])   # retry singles (rate limits etc.)
        return out


# ---------------------------------------------------------------------------------------------------------------------
# Decoding

def _int(word, signed=False):
    v = int.from_bytes(word, "big")
    return v - (1 << 256) if signed and v >> 255 else v


def decode_swap(log):
    """Raw log -> {pool, src, sender, d0, d1, sqrtp} or None. d0/d1 are the POOL's balance change (positive = the pool
    received that token), whatever the event's own sign convention."""
    t0 = L(log["topics"][0]) if log["topics"] else None
    addr = L(log["address"])
    data = bytes.fromhex(log["data"][2:])
    w = [data[i:i + 32] for i in range(0, len(data), 32)]
    if t0 == T_V3 and len(w) >= 5:
        return {"src": "v3", "pool": addr, "sender": None, "d0": _int(w[0], True), "d1": _int(w[1], True),
                "sqrtp": _int(w[2])}
    if t0 in (T_AERO, T_UNI2) and len(w) >= 4:
        a0in, a1in, a0out, a1out = (_int(x) for x in w[:4])
        return {"src": "aero" if t0 == T_AERO else "uni2", "pool": addr, "sender": None,
                "d0": a0in - a0out, "d1": a1in - a1out, "sqrtp": None}
    if t0 == T_V4 and addr == POOL_MANAGER and len(w) >= 6 and len(log["topics"]) >= 3:
        # V4 emits the SWAPPER's delta (negative = the swapper paid), the opposite of V3: flip it to the pool's view.
        return {"src": "v4", "pool": L(log["topics"][1]), "sender": "0x" + L(log["topics"][2])[-40:],
                "d0": -_int(w[0], True), "d1": -_int(w[1], True), "sqrtp": _int(w[2])}
    return None


def swap_direction(swap, meta):
    """(token_in, amount_in, token_out, amount_out) with native ETH merged into WETH, or None if unknown/degenerate."""
    m = meta.get(swap["pool"])
    if not m or not m.get("token0"):
        return None
    t0, t1 = (WETH if t == ZERO else t for t in (m["token0"], m["token1"]))
    if t0 == t1:                                   # ETH/WETH wrapper pool: not a trade
        return None
    d0, d1 = swap["d0"], swap["d1"]
    if d0 > 0 and d1 < 0:
        return t0, d0, t1, -d1
    if d1 > 0 and d0 < 0:
        return t1, d1, t0, -d0
    return None


def is_loop(directions):
    """A token paid into an earlier swap comes back out of a later one. True for every arb cycle, false for any plain
    multi-hop trade A -> B -> C (there each token only ever leaves *after* it arrived)."""
    for i, d in enumerate(directions):
        if any(e[2] == d[0] for e in directions[i + 1:]):
            return True
    return False


def price_per_raw(directions, eth_usd):
    """USD per raw unit (no decimals needed) for every token reachable from WETH/USDC through the tx's own swaps."""
    price = {WETH: eth_usd / 1e18, USDC: 1e-6}
    changed = True
    while changed:
        changed = False
        for tin, ain, tout, aout in directions:
            if tin in price and tout not in price and aout:
                price[tout] = price[tin] * ain / aout
                changed = True
            elif tout in price and tin not in price and ain:
                price[tin] = price[tout] * aout / ain
                changed = True
    return price


def trader_flows(receipt, swaps, meta, actors=()):
    """Net raw flow per asset across the edge of the venues: ERC20 Transfers out of the swapped pools / the V4
    PoolManager (and hooked pools' hook contracts, but only their transfers with tx.from / tx.to) minus Transfers into
    them. That is what the tx extracted from the pools, wherever it went (bot, EOA or
    a third profit address), and flash loans from the PoolManager or anyone else net to zero. WETH and ETH are one
    asset (Deposit/Withdrawal ignored); native ETH legs of V4 pools have no log and come from the Swap event."""
    hooks = {h for s in swaps if (h := (meta.get(s["pool"]) or {}).get("hooks")) and h != ZERO}   # venue code too
    inside = {s["pool"] for s in swaps if s["src"] != "v4"} | {POOL_MANAGER} | hooks
    flows = defaultdict(int)
    for log in receipt["logs"]:
        tp = log["topics"]
        if len(tp) != 3 or L(tp[0]) != T_TRANSFER or len(log["data"]) != 66:   # 4 topics = ERC721
            continue
        frm, to = "0x" + L(tp[1])[-40:], "0x" + L(tp[2])[-40:]
        if (frm in inside) == (to in inside):
            continue
        if (frm in hooks and to not in actors) or (to in hooks and frm not in actors):
            continue                            # a hook paying creators/fees (or being paid by others) isn't the arb
        token = L(log["address"])
        flows[token] += int(log["data"], 16) * (1 if frm in inside else -1)
    for s in swaps:
        if s["src"] == "v4" and (meta.get(s["pool"]) or {}).get("token0") == ZERO:
            flows[WETH] -= s["d0"]              # the pool received d0 native ETH
    return {t: v for t, v in flows.items() if v}


def actor_base_gain(receipt, eth_usd):
    """Rough value for a tx we can't measure properly: the WETH + USDC (ERC20) that {tx.from, tx.to} gained, before gas,
    and only if they paid in no other token (else it's a sale, not a profit: a user selling a coin gains WETH too).
    Misses native ETH and profit paid to a third address, so it is shown separately, never in the totals."""
    actors = {L(receipt["from"]), L(receipt.get("to") or "")}
    usd = {WETH: eth_usd / 1e18, USDC: 1e-6}
    flows = defaultdict(int)
    for log in receipt["logs"]:
        tp = log["topics"]
        if len(tp) != 3 or L(tp[0]) != T_TRANSFER or len(log["data"]) != 66:
            continue
        frm, to = "0x" + L(tp[1])[-40:], "0x" + L(tp[2])[-40:]
        if (frm in actors) != (to in actors):
            flows[L(log["address"])] += int(log["data"], 16) * (1 if to in actors else -1)
    if any(v < 0 for t, v in flows.items() if t not in usd):
        return 0.0
    return max(0.0, sum(v * usd[t] for t, v in flows.items() if t in usd))


def classify(receipt, meta, eth_usd, base_fee):
    """One receipt -> {"arb": bool, "reason": str, ...fields}. Pure function of its inputs (unit-tested)."""
    raw = sorted((s | {"logi": int(log["logIndex"], 16)} for log in receipt["logs"] if (s := decode_swap(log))),
                 key=lambda s: s["logi"])
    swaps = [s for s in raw if (meta.get(s["pool"]) or {}).get("token0")]
    res = {"arb": False, "tx": receipt["transactionHash"], "block": int(receipt["blockNumber"], 16),
           "txi": int(receipt["transactionIndex"], 16), "bot": L(receipt.get("to") or ""), "eoa": L(receipt["from"])}
    if int(receipt.get("status", "0x1"), 16) != 1:
        return res | {"reason": "reverted"}
    if len(swaps) < len(raw):                          # a V4 PoolId we couldn't resolve (or a non-pool emitter)
        lp = any(lg["topics"] and L(lg["topics"][0]) in T_LIQUIDITY for lg in receipt["logs"])
        return res | {"reason": "unknown_pool", "est_usd": 0.0 if lp else actor_base_gain(receipt, eth_usd)}
    if any(s["src"] == "v4" and meta[s["pool"]]["token0"] == ZERO and meta[s["pool"]]["hooks"] != ZERO
           for s in swaps):
        return res | {"reason": "hooked_native"}         # a hook can move native ETH the Swap event doesn't show
    pools = list(dict.fromkeys(s["pool"] for s in swaps))
    if len(pools) < 2:
        return res | {"reason": "single_pool"}
    directions = [d for s in swaps if (d := swap_direction(s, meta))]
    if not is_loop(directions):
        return res | {"reason": "no_loop"}
    venue = {s["pool"] for s in swaps if s["src"] != "v4"} | {POOL_MANAGER}
    if any(lg["topics"] and L(lg["topics"][0]) in T_LIQUIDITY and L(lg["address"]) in venue for lg in receipt["logs"]):
        return res | {"reason": "lp_action"}
    flows = trader_flows(receipt, swaps, meta, {res["eoa"], res["bot"]})
    price = price_per_raw(directions, eth_usd)
    if any(t not in price for t in flows):
        return res | {"reason": "unpriced_flow"}
    gross = sum(v * price[t] for t, v in flows.items())
    if gross <= 0:
        return res | {"reason": "not_up"}
    # The profit asset (largest gain) must be one the tx also paid in: it ended with more of what it started with
    start = max(flows, key=lambda t: flows[t] * price[t])
    if not any(d[0] == start for d in directions):
        return res | {"reason": "not_up"}
    gas_used, gas_price = int(receipt["gasUsed"], 16), int(receipt["effectiveGasPrice"], 16)
    l1_fee = int(receipt.get("l1Fee") or "0x0", 16)
    usd = eth_usd / 1e18
    tokens = sorted({t for d in directions for t in (d[0], d[2])} - {WETH, USDC}) or [BASE_PAIR]
    venues = sorted({meta[p]["venue"] for p in pools})
    return res | {
        "arb": True, "reason": "arb", "start": start, "tokens": tokens, "venues": venues, "pools": pools,
        "hooked": any(meta[p].get("hooks") not in (None, ZERO) for p in pools),
        "reachable": all(meta[p]["venue"] in TRADED_VENUES and meta[p].get("hooks") in (None, ZERO) for p in pools),
        "n_swaps": len(swaps), "flows": {t: str(v) for t, v in flows.items()},
        "gross_usd": gross, "l2_gas_usd": gas_used * gas_price * usd, "l1_fee_usd": l1_fee * usd,
        "priority_usd": gas_used * max(gas_price - base_fee, 0) * usd,
        "net_usd": gross - gas_used * gas_price * usd - l1_fee * usd, "eth_usd": eth_usd,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Chain reads

def multicall(rpc, calls, block="latest", chunk=150):
    """[(target, calldata hex)] -> [bytes or None] via Multicall3 aggregate3 (failures allowed)."""
    out = []
    sel = scanner.selector("aggregate3((address,bool,bytes)[])")
    for i in range(0, len(calls), chunk):
        part = [(Web3.to_checksum_address(t), True, bytes.fromhex(d[2:])) for t, d in calls[i:i + chunk]]
        data = "0x" + sel + encode(["(address,bool,bytes)[]"], [part]).hex()
        raw = rpc.call("eth_call", [{"to": scanner.MULTICALL3, "data": data, "gas": hex(200_000_000)}, block])
        out += [ret if ok else None for ok, ret in decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))[0]]
    return out


def _addr(raw):
    return "0x" + raw[-20:].hex() if raw and len(raw) == 32 else None


SEL_FACTORY, SEL_T0, SEL_T1 = (scanner.selector(s) for s in ("factory()", "token0()", "token1()"))
SEL_POOL_KEYS = scanner.selector("poolKeys(bytes25)")


def pool_meta_v2v3(rpc, pools, srcs):
    """factory/token0/token1 for pool contracts -> {pool: meta}; contracts that don't answer get no token0."""
    calls = [(p, "0x" + s) for p in pools for s in (SEL_FACTORY, SEL_T0, SEL_T1)]
    raw = multicall(rpc, calls)
    out = {}
    for i, p in enumerate(pools):
        f, a, b = (_addr(raw[3 * i + k]) for k in range(3))
        venue = FACTORY_VENUE.get(f) or {"v3": "other_v3", "aero": "other_aero", "uni2": "other_v2"}[srcs[p]]
        out[p] = {"venue": venue, "factory": f, "token0": a, "token1": b, "hooks": None}
    return out


def v4_meta_from_key(pid, c0, c1, fee, ts, hooks):
    key = (Web3.to_checksum_address(c0), Web3.to_checksum_address(c1), fee, ts, Web3.to_checksum_address(hooks))
    if scanner.v4_pool_id(key) != L(pid):
        return None                                # never trust a key that doesn't hash to the PoolId
    return {"venue": "univ4", "factory": POOL_MANAGER, "token0": L(c0), "token1": L(c1), "hooks": L(hooks),
            "fee": fee, "tick_spacing": ts}


SEL_EXTSLOAD = scanner.selector("extsload(bytes32)")
V4_POOLS_SLOT = 6        # PoolManager storage: mapping(PoolId => Pool.State) _pools; slot0 is the first word


def v4_state_slot(pid):
    """Storage slot of a pool's slot0 inside the PoolManager (checked against StateView.getSlot0 on live pools)."""
    return bytes(Web3.keccak(encode(["bytes32", "uint256"], [bytes.fromhex(pid[2:]), V4_POOLS_SLOT])))


def v4_initialized(rpc, ids, block):
    """Ids whose pool exists (slot0 sqrtPrice != 0) at `block`, via PoolManager.extsload: no StateView needed, so it
    works at any block since the PoolManager's deployment. Needs an archive eth_call."""
    raw = multicall(rpc, [(POOL_MANAGER, "0x" + SEL_EXTSLOAD + v4_state_slot(p).hex()) for p in ids], hex(block),
                    chunk=1000)
    return {p for p, r in zip(ids, raw) if r and int.from_bytes(r[:32], "big") & ((1 << 160) - 1)}


def locate_v4_inits(ids, lo, hi, is_init, width=100, threads=8, log=None, windows=None, on_round=None):
    """Narrow each pool's Initialize block to (a, b] with b - a <= `width`, by bisection shared across pools: every
    round probes the midpoint of each open interval with one batched `is_init(block, ids)` (-> the ids that exist at
    `block`). A pool exists from its Initialize on and is never un-initialized, so existence is monotone in the block.
    ~log2((hi - lo) / width) rounds, one call per distinct interval per round. Pure apart from `is_init`.
    `windows` ({id: (a, b)} from an earlier, interrupted run) resumes; `on_round(windows)` is called after every round
    (to checkpoint). -> {id: (a, b)}, assuming each id exists at `hi` and not at `lo`."""
    groups = defaultdict(list)
    for pid in ids:
        groups[tuple((windows or {}).get(pid, (lo, hi)))].append(pid)
    while any(b - a > width for a, b in groups):
        open_ = [ab for ab in groups if ab[1] - ab[0] > width]
        mids = [(a + b) // 2 for a, b in open_]
        with ThreadPoolExecutor(threads) as ex:
            res = list(ex.map(lambda am: is_init(am[1], groups[am[0]]), zip(open_, mids)))
        new = defaultdict(list, {ab: v for ab, v in groups.items() if ab[1] - ab[0] <= width})
        for (a, b), mid, alive in zip(open_, mids, res):
            for pid in groups[(a, b)]:
                new[(a, mid) if pid in alive else (mid, b)].append(pid)
        groups = new
        if on_round:
            on_round({pid: ab for ab, pids in groups.items() for pid in pids})
        if log:
            log(f"\r  locating Initialize blocks: {len(groups)} intervals, widest "
                f"{max(b - a for a, b in groups):,} blocks   ", end="")
    if log:
        log("")
    return {pid: ab for ab, pids in groups.items() for pid in pids}


def pool_meta_v4(rpc, ids, call_rpc=None, head=None, log=print, threads=8, windows=None, on_round=None):
    """PoolId -> key. First the PositionManager's poolKeys (pools that ever had a position minted through it). The rest
    (mostly hook-managed liquidity) from the PoolManager's own Initialize event: `locate_v4_inits` narrows each pool's
    creation to a 100-block window by reading its state at past blocks (`call_rpc`, archive eth_call), then that window's
    Initialize logs (`rpc`, 100-block eth_getLogs) give the key. Every key is re-hashed against its PoolId: a key is
    never inferred from transfers. -> (found, missing)."""
    raw = multicall(rpc, [(scanner.UNI_V4_POSITION_MANAGER, "0x" + SEL_POOL_KEYS +
                           encode(["bytes25"], [bytes.fromhex(pid[2:52])]).hex()) for pid in ids])
    out, missing = {}, []
    for pid, r in zip(ids, raw):
        k = decode(["address", "address", "uint24", "int24", "address"], r) if r and len(r) == 160 else None
        m = v4_meta_from_key(pid, *k) if k and (int(k[0], 16) or int(k[1], 16)) else None
        if m:
            out[pid] = m
        else:
            missing.append(pid)
    if not (missing and head):
        return out, missing
    call_rpc = call_rpc or rpc
    log(f"  {len(missing)} PoolIds unknown to the PositionManager: locating their Initialize blocks")
    win = locate_v4_inits(missing, V4_DEPLOY_BLOCK - 1, head, lambda b, ps: v4_initialized(call_rpc, ps, b),
                          threads=threads, log=log, windows=windows, on_round=on_round)
    by_win = defaultdict(list)
    for pid, ab in win.items():
        by_win[ab].append(pid)
    log(f"  {len(win)} located in {len(by_win)} windows; reading their Initialize logs")

    def logs_for(item):
        (a, b), pids = item
        found = {}
        for i in range(0, len(pids), 100):
            logs, _ = fetch_logs(rpc, {"address": POOL_MANAGER, "topics": [T_V4_INIT, pids[i:i + 100]]}, a + 1, b, 100)
            for lg in logs:
                fee, ts, hooks = decode(["uint24", "int24", "address"], bytes.fromhex(lg["data"][2:])[:96])
                m = v4_meta_from_key(L(lg["topics"][1]), "0x" + lg["topics"][2][-40:], "0x" + lg["topics"][3][-40:],
                                     fee, ts, hooks)
                if m:
                    found[L(lg["topics"][1])] = m
        return found

    with ThreadPoolExecutor(threads) as ex:
        for n, found in enumerate(ex.map(logs_for, by_win.items()), 1):
            out |= found
            log(f"\r  Initialize logs: {n}/{len(by_win)} windows, {sum(p in out for p in missing)} keys   ", end="")
    log("")
    return out, [p for p in missing if p not in out]


def anchor_address(rpc):
    data = "0x" + scanner.selector("getPool(address,address,uint24)") + encode(
        ["address", "address", "uint24"], [scanner.WETH, scanner.USDC, scanner.ANCHOR_FEE_TIER]).hex()
    return "0x" + rpc.call("eth_call", [{"to": scanner.UNI_V3_FACTORY, "data": data}, "latest"])[-40:]


def sqrtp_to_eth_usd(sqrtp):
    return (sqrtp / 2**96) ** 2 * 10**12      # token0 = WETH (18), token1 = USDC (6)


def fetch_logs(rpc, flt, start, end, span):
    """All logs in [start, end] for a filter, halving the range on provider range/size errors. -> (logs, span)."""
    out, b = [], start
    while b <= end:
        e = min(end, b + span - 1)
        try:
            out += rpc.call("eth_getLogs", [flt | {"fromBlock": hex(b), "toBlock": hex(e)}])
        except RpcError:
            if span == 1:
                raise
            span = max(1, span // 2)
            continue
        b = e + 1
    return out, span


# ---------------------------------------------------------------------------------------------------------------------
# Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chunks (start INTEGER PRIMARY KEY, end INTEGER);
CREATE TABLE IF NOT EXISTS swaps (block INTEGER, txi INTEGER, logi INTEGER, tx TEXT, src TEXT, pool TEXT,
                                  sender TEXT, d0 TEXT, d1 TEXT, PRIMARY KEY (block, logi));
CREATE INDEX IF NOT EXISTS swaps_tx ON swaps (tx);
CREATE TABLE IF NOT EXISTS attempts (tx TEXT PRIMARY KEY, block INTEGER, txi INTEGER, bot TEXT, eoa TEXT,
                                     status INTEGER, gas_usd REAL);
CREATE TABLE IF NOT EXISTS attempt_blocks (block INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS anchor (block INTEGER, logi INTEGER, sqrtp TEXT, PRIMARY KEY (block, logi));
CREATE TABLE IF NOT EXISTS pools (pool TEXT PRIMARY KEY, venue TEXT, factory TEXT, token0 TEXT, token1 TEXT,
                                  hooks TEXT);
CREATE TABLE IF NOT EXISTS basefee (block INTEGER PRIMARY KEY, fee INTEGER);
CREATE TABLE IF NOT EXISTS checked (tx TEXT PRIMARY KEY, reason TEXT, est_usd REAL);
CREATE TABLE IF NOT EXISTS receipts (tx TEXT PRIMARY KEY, z BLOB);
CREATE TABLE IF NOT EXISTS arbs (tx TEXT PRIMARY KEY, block INTEGER, txi INTEGER, bot TEXT, eoa TEXT, start TEXT,
                                 tokens TEXT, venues TEXT, pools TEXT, hooked INTEGER, reachable INTEGER,
                                 n_swaps INTEGER, gross_usd REAL, l2_gas_usd REAL, l1_fee_usd REAL,
                                 priority_usd REAL, net_usd REAL, eth_usd REAL, prev_same_pool INTEGER,
                                 trigger_gap INTEGER, flows TEXT);
CREATE TABLE IF NOT EXISTS symbols (token TEXT PRIMARY KEY, symbol TEXT);
"""


def open_db(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    if "est_usd" not in [c[1] for c in db.execute("PRAGMA table_info(checked)")]:
        db.execute("ALTER TABLE checked ADD COLUMN est_usd REAL")
    return db


def get_meta(db, k, default=None):
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))


def load_pool_meta(db):
    return {p: {"venue": v, "factory": f, "token0": a, "token1": b, "hooks": h}
            for p, v, f, a, b, h in db.execute("SELECT * FROM pools")}


def save_pool_meta(db, meta):
    db.executemany("INSERT OR REPLACE INTO pools VALUES (?,?,?,?,?,?)",
                   [(p, m["venue"], m["factory"], m["token0"], m["token1"], m["hooks"]) for p, m in meta.items()])


# ---------------------------------------------------------------------------------------------------------------------
# Pipeline

def ingest(db, rpc, start, end, log=print, span=100):
    """Swap logs for [start, end] into the DB, CHUNK_BLOCKS at a time; finished chunks are skipped on a rerun."""
    anchor = get_meta(db, "anchor")
    done = {s for (s,) in db.execute("SELECT start FROM chunks")}
    spans = dict.fromkeys(SOURCES, span)          # only ever shrinks: a range error costs a wasted request
    todo = [b for b in range(start, end + 1, CHUNK_BLOCKS) if b not in done]
    t0 = time.time()

    def pull(src, b, e):
        t, addr = SOURCES[src]
        logs, spans[src] = fetch_logs(rpc, {"topics": [t]} | ({"address": addr} if addr else {}), b, e, spans[src])
        return src, logs

    pool = ThreadPoolExecutor(len(SOURCES))
    for n, b in enumerate(todo, 1):
        e = min(end, b + CHUNK_BLOCKS - 1)
        rows, anchors = [], []
        for src, logs in pool.map(lambda src: pull(src, b, e), SOURCES):
            for lg in logs:
                s = decode_swap(lg)
                if not s or s["src"] != src:
                    continue
                blk, txi, logi = (int(lg[k], 16) for k in ("blockNumber", "transactionIndex", "logIndex"))
                rows.append((blk, txi, logi, L(lg["transactionHash"]), src, s["pool"], s["sender"],
                             str(s["d0"]), str(s["d1"])))
                if s["pool"] == anchor:
                    anchors.append((blk, logi, str(s["sqrtp"])))
        db.executemany("INSERT OR IGNORE INTO swaps VALUES (?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT OR IGNORE INTO anchor VALUES (?,?,?)", anchors)
        db.execute("INSERT OR REPLACE INTO chunks VALUES (?, ?)", (b, e))
        db.commit()
        rate = (time.time() - t0) / n
        log(f"\r  logs: chunk {n}/{len(todo)} (block {e}), {len(rows)} swaps, "
            f"~{rate * (len(todo) - n) / 60:.0f} min left   ", end="")
    pool.shutdown()
    log("")


def multi_pool_txs(db):
    return [tx for (tx,) in db.execute("SELECT tx FROM swaps GROUP BY tx HAVING COUNT(DISTINCT pool) >= 2")]


def resolve_pools(db, rpc, log=print, call_rpc=None, head=None):
    """Metadata for every pool that appears in a 2+-pool tx (the only ones a loop can use)."""
    known = load_pool_meta(db)
    rows = db.execute("SELECT DISTINCT pool, src FROM swaps WHERE tx IN "
                      "(SELECT tx FROM swaps GROUP BY tx HAVING COUNT(DISTINCT pool) >= 2)").fetchall()
    need = {p: s for p, s in rows if p not in known}
    plain = [p for p, s in need.items() if s != "v4"]
    v4 = [p for p, s in need.items() if s == "v4"]
    log(f"  pool metadata: {len(plain)} pool contracts, {len(v4)} V4 PoolIds to resolve")
    for i in range(0, len(plain), 3000):
        save_pool_meta(db, pool_meta_v2v3(rpc, plain[i:i + 3000], need))
        db.commit()
    if v4:
        found, missing = pool_meta_v4(rpc, v4, call_rpc, head, log)
        save_pool_meta(db, found)
        db.commit()
        if missing:
            log(f"  {len(missing)} V4 PoolIds unresolved: txs using them are counted as unknown_pool")
    return load_pool_meta(db)


def fetch_basefees(db, rpc, start, end):
    have = {b for (b,) in db.execute("SELECT block FROM basefee WHERE block BETWEEN ? AND ?", (start, end))}
    b = end
    while b >= start:
        if b in have:
            b -= 1
            continue
        n = min(1024, b - start + 1)
        r = rpc.call("eth_feeHistory", [hex(n), hex(b), []])
        first = int(r["oldestBlock"], 16)
        db.executemany("INSERT OR REPLACE INTO basefee VALUES (?, ?)",
                       [(first + i, int(f, 16)) for i, f in enumerate(r["baseFeePerGas"][:n])])
        b = first - 1
    db.commit()


def candidates(db, meta):
    """Txs whose swaps (in log order) form a loop, plus any 2+-pool tx touching an unresolved pool (so the report can
    count them)."""
    out = []
    by_tx = defaultdict(list)
    for tx, logi, pool, d0, d1 in db.execute(
            "SELECT tx, logi, pool, d0, d1 FROM swaps WHERE tx IN "
            "(SELECT tx FROM swaps GROUP BY tx HAVING COUNT(DISTINCT pool) >= 2) ORDER BY tx, block, logi"):
        by_tx[tx].append({"pool": pool, "d0": int(d0), "d1": int(d1)})
    for tx, swaps in by_tx.items():
        if any(not (meta.get(s["pool"]) or {}).get("token0") for s in swaps):
            out.append(tx)                                    # judged "unknown_pool"
            continue
        if is_loop([d for s in swaps if (d := swap_direction(s, meta))]):
            out.append(tx)
    return out


def eth_usd_at(anchor_rows, block):
    """Anchor price from the last anchor-pool swap before `block` (the first one if the range starts earlier)."""
    i = bisect.bisect_left(anchor_rows, (block, -1)) - 1
    return sqrtp_to_eth_usd(anchor_rows[max(i, 0)][2])


def backrun_context(db, block, txi, pools):
    """(previous tx swapped on one of these pools?, txs since the last earlier tx in the block that did)."""
    rows = db.execute(f"SELECT txi FROM swaps WHERE block=? AND txi<? AND pool IN ({','.join('?' * len(pools))})"
                      " ORDER BY txi DESC LIMIT 1", (block, txi, *pools)).fetchone()
    return (rows is not None and rows[0] == txi - 1), (txi - rows[0] if rows else None)


def save_arb(db, r):
    db.execute("INSERT OR REPLACE INTO arbs VALUES (" + ",".join("?" * 21) + ")", (
        r["tx"], r["block"], r["txi"], r["bot"], r["eoa"], r["start"], json.dumps(r["tokens"]),
        json.dumps(r["venues"]), json.dumps(r["pools"]), int(r["hooked"]), int(r["reachable"]), r["n_swaps"],
        r["gross_usd"], r["l2_gas_usd"], r["l1_fee_usd"], r["priority_usd"], r["net_usd"], r["eth_usd"],
        int(r["prev_same_pool"]), r["trigger_gap"], json.dumps(r["flows"])))


def stored_receipt(db, tx):
    row = db.execute("SELECT z FROM receipts WHERE tx=?", (tx,)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None


def judge(db, rpc, meta, log=print, threads=4, receipt_rpc=None):
    """Fetch (or reuse stored) receipts of every candidate and classify them. Receipts are kept compressed, so a
    re-judge after a classifier change (`--rejudge`) needs no RPC."""
    todo = [tx for tx in candidates(db, meta)
            if not db.execute("SELECT 1 FROM checked WHERE tx=?", (tx,)).fetchone()]
    anchor_rows = [(b, i, int(s)) for b, i, s in db.execute("SELECT block, logi, sqrtp FROM anchor ORDER BY 1, 2")]
    basefee = dict(db.execute("SELECT block, fee FROM basefee"))
    have = {tx for (tx,) in db.execute("SELECT tx FROM receipts")}
    log(f"  receipts: {len(todo)} loop-shaped txs to judge ({len(have & set(todo))} already stored)")
    t0 = time.time()
    step = 30 * threads

    src = receipt_rpc or rpc

    def one(tx):
        for _ in range(6):                    # a lagging node answers null, a capped one errors: move to the next
            try:
                r = src.call("eth_getTransactionReceipt", [tx])
            except (RpcError, ConnectionError):
                r = None
            if r:
                return r
        return None

    def fetch(part):
        return [r for r in map(one, part) if r]

    pool = ThreadPoolExecutor(threads)
    for i in range(0, len(todo), step):
        part = todo[i:i + step]
        missing = [tx for tx in part if tx not in have]
        groups = [missing[j:j + 30] for j in range(0, len(missing), 30)]
        for receipt in (r for rs in pool.map(fetch, groups) for r in rs):
            db.execute("INSERT OR REPLACE INTO receipts VALUES (?, ?)",
                       (L(receipt["transactionHash"]), zlib.compress(json.dumps(receipt).encode(), 6)))
        for tx in part:
            receipt = stored_receipt(db, tx)
            if receipt is None:
                db.execute("INSERT OR REPLACE INTO checked VALUES (?, ?, NULL)", (tx, "no_receipt"))
                continue
            blk = int(receipt["blockNumber"], 16)
            r = classify(receipt, meta, eth_usd_at(anchor_rows, blk), basefee.get(blk, 0))
            if r["arb"]:
                r["prev_same_pool"], r["trigger_gap"] = backrun_context(db, blk, r["txi"], r["pools"])
                save_arb(db, r)
            db.execute("INSERT OR REPLACE INTO checked VALUES (?, ?, ?)", (tx, r["reason"], r.get("est_usd")))
        db.commit()
        n = min(i + step, len(todo))
        log(f"\r  receipts: {n}/{len(todo)}, ~{(time.time() - t0) / n * (len(todo) - n) / 60:.0f} min left   ", end="")
    pool.shutdown()
    log("")


def collect(rpc, hours, db, log=print, to_block=None, span=100, rejudge=False, threads=4, receipt_rpc=None,
            call_rpc=None):
    head = int(rpc.call("eth_blockNumber", []), 16)
    end = to_block or head - FINALITY_LAG
    start = end - int(hours * BLOCKS_PER_HOUR) + 1
    rng = get_meta(db, "range")
    if rng and rng != [start, end]:
        if get_meta(db, "frozen"):
            start, end = rng                                  # resume the run that's already in the DB
        else:
            log(f"  DB holds a different range {rng}: resuming that one (delete {DB_PATH.name} to start over)")
            start, end = rng
    set_meta(db, "range", [start, end])
    set_meta(db, "frozen", True)
    if not get_meta(db, "anchor"):
        set_meta(db, "anchor", L(anchor_address(rpc)))
    set_meta(db, "rpc", scanner.redact_url(rpc.url))
    db.commit()
    log(f"arb_history: blocks {start}..{end} ({(end - start + 1) / BLOCKS_PER_HOUR:.1f} h), "
        f"RPC {scanner.redact_url(rpc.url)}")
    ingest(db, rpc, start, end, log, span)
    meta = resolve_pools(db, rpc, log, call_rpc or receipt_rpc, head)
    fetch_basefees(db, rpc, start, end)
    if rejudge:
        db.execute("DELETE FROM checked")
        db.execute("DELETE FROM arbs")
    judge(db, rpc, meta, log, threads, receipt_rpc)
    log(f"  done: {rpc.calls:,} RPC requests")


# ---------------------------------------------------------------------------------------------------------------------
# Failed / no-op attempts: what competing costs. A losing attempt leaves no Swap log (it reverts, or the bot's
# contract checks the price and returns early), so it is found only by reading every tx of a block.

def bot_identities(db):
    """(bot contracts, {eoa: its most-profitable bot}) from the arbs found."""
    bots = {b for (b,) in db.execute("SELECT DISTINCT bot FROM arbs") if b}
    eoa_bot = {}
    for eoa, bot, _ in db.execute("SELECT eoa, bot, SUM(net_usd) v FROM arbs GROUP BY eoa, bot ORDER BY v"):
        eoa_bot[eoa] = bot                               # ascending, so the last (largest) wins
    return bots, eoa_bot


def block_attempts(receipts, bots, eoa_bot, eth_usd):
    """Every tx of a block sent to a known bot contract or from a known bot EOA, with its full cost (L2 gas + L1 data
    fee) in USD. Pure. -> [(tx, block, txi, bot, eoa, status, gas_usd)]."""
    out = []
    for r in receipts:
        to, frm = L(r.get("to") or ""), L(r["from"])
        if to not in bots and frm not in eoa_bot:
            continue
        wei = int(r["gasUsed"], 16) * int(r["effectiveGasPrice"], 16) + int(r.get("l1Fee") or "0x0", 16)
        out.append((L(r["transactionHash"]), int(r["blockNumber"], 16), int(r["transactionIndex"], 16),
                    to if to in bots else eoa_bot[frm], frm, int(r["status"], 16), wei / 1e18 * eth_usd))
    return out


def scan_attempts(db, rpc, every=5, threads=8, log=print):
    """Sample every `every`-th block of the DB's range with eth_getBlockReceipts (resumable)."""
    start, end = get_meta(db, "range")
    bots, eoa_bot = bot_identities(db)
    anchor_rows = [(b, i, int(x)) for b, i, x in db.execute("SELECT block, logi, sqrtp FROM anchor ORDER BY 1, 2")]
    done = {b for (b,) in db.execute("SELECT block FROM attempt_blocks")}
    todo = [b for b in range(start, end + 1, every) if b not in done]
    log(f"  attempts: {len(bots)} bot contracts, {len(eoa_bot)} EOAs; {len(todo):,} sampled blocks to read "
        f"(1 in {every})")

    def one(b):
        try:
            receipts = rpc.call("eth_getBlockReceipts", [hex(b)])
        except ConnectionError:
            return b, None                    # endpoints kept failing: left unmarked, a rerun picks it up
        return b, block_attempts(receipts, bots, eoa_bot, eth_usd_at(anchor_rows, b))

    skipped = 0
    with ThreadPoolExecutor(threads) as ex:
        for n, (b, rows) in enumerate(ex.map(one, todo), 1):
            if rows is None:
                skipped += 1
                continue
            db.executemany("INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?,?,?)", rows)
            db.execute("INSERT OR IGNORE INTO attempt_blocks VALUES (?)", (b,))
            if n % 50 == 0 or n == len(todo):
                db.commit()
                log(f"\r  attempts: {n:,}/{len(todo):,} blocks ({skipped} skipped)", end="")
    db.commit()
    log(f"\n  {skipped} blocks skipped (RPC kept failing): rerun --attempts to fill them" if skipped else "")


def attempt_summary(attempts, arbs, sampled_blocks, total_blocks, hours):
    """Per bot: attempts vs arbs, and true net = arb net (all blocks) minus the gas of its non-arb txs (sampled blocks,
    scaled up). attempts: [(tx, block, txi, bot, eoa, status, gas_usd)]; arbs: dicts with tx, bot, net_usd. Pure."""
    per_day, scale = 24 / hours, total_blocks / sampled_blocks if sampled_blocks else 0
    arb_tx = {a["tx"] for a in arbs}
    net = Counter()
    for a in arbs:
        net[a["bot"]] += a["net_usd"]
    stats = defaultdict(lambda: {"txs": 0, "arbs": 0, "reverted": 0, "noop": 0, "wasted_gas": 0.0})
    for tx, _, _, bot, _, status, gas in attempts:
        st = stats[bot]
        st["txs"] += 1
        if tx in arb_tx:
            st["arbs"] += 1
        else:
            st["reverted" if status == 0 else "noop"] += 1
            st["wasted_gas"] += gas
    rows = []
    for bot in set(net) | set(stats):
        st = stats[bot]
        arb_day, waste_day = net[bot] * per_day, st["wasted_gas"] * scale * per_day
        rows.append({"bot": bot, "arb_net_usd_day": arb_day, "wasted_gas_usd_day": waste_day,
                     "true_net_usd_day": arb_day - waste_day, "sample_txs": st["txs"], "sample_arbs": st["arbs"],
                     "sample_reverted": st["reverted"], "sample_noop": st["noop"],
                     "attempts_per_arb": st["txs"] / st["arbs"] if st["arbs"] else None})
    rows.sort(key=lambda r: -r["arb_net_usd_day"])
    tot = {k: sum(r[k] for r in rows) for k in ("arb_net_usd_day", "wasted_gas_usd_day", "true_net_usd_day",
                                                "sample_txs", "sample_arbs", "sample_reverted", "sample_noop")}
    return {"bots": rows, "total": tot, "scale": scale}


def print_attempts(db, hours):
    sampled = db.execute("SELECT COUNT(*) FROM attempt_blocks").fetchone()[0]
    if not sampled:
        return None
    total = sum(e - s + 1 for s, e in db.execute("SELECT start, end FROM chunks"))
    s = attempt_summary(db.execute("SELECT * FROM attempts").fetchall(),
                        [{"tx": t, "bot": b, "net_usd": n} for t, b, n in db.execute("SELECT tx, bot, net_usd FROM arbs")],
                        sampled, total, hours)
    t = s["total"]
    print(f"\n  Attempts (1 in {s['scale']:.0f} blocks sampled, {sampled:,} blocks): {t['sample_txs']:,} bot txs = "
          f"{t['sample_arbs']:,} arbs + {t['sample_noop']:,} no-arb successes + {t['sample_reverted']:,} reverts")
    print(f"    all bots: arb net ${t['arb_net_usd_day']:,.0f}/day - gas of attempts that made nothing "
          f"${t['wasted_gas_usd_day']:,.0f}/day = true net ${t['true_net_usd_day']:,.0f}/day")
    print(f"    {'bot':<44}{'arb net/d':>10}{'waste/d':>9}{'true/d':>9}{'txs/arb':>9}{'reverts':>9}{'no-ops':>8}")
    for r in s["bots"][:25]:
        print(f"    {r['bot']:<44}{r['arb_net_usd_day']:>10,.0f}{r['wasted_gas_usd_day']:>9,.0f}"
              f"{r['true_net_usd_day']:>9,.0f}{(r['attempts_per_arb'] or 0):>9.1f}{r['sample_reverted']:>9,}"
              f"{r['sample_noop']:>8,}")
    return s


# ---------------------------------------------------------------------------------------------------------------------
# Report

def safe_seed_tokens(log_glob=str(ROOT / "logs" / "2*" / "results.jsonl"), seed_list=None):
    """{address: symbol} of seed-list tokens judged SAFE / PROXY_WARNING in the newest pipeline run that checked the seed
    list (at least half of it). Runs over other token lists (`--tokens-file`) are skipped, even if they share a token or
    two, so "seed" always means the seed list."""
    if seed_list is None:
        import pipeline
        seed_list = [a for _, a, _ in pipeline.SEED_TOKENS]
    seed_list = {L(a) for a in seed_list}
    for f in sorted(glob.glob(log_glob), key=os.path.getmtime, reverse=True):
        rows = [json.loads(x) for x in open(f) if x.strip()]
        if sum(L(r["token"]) in seed_list for r in rows) * 2 < len(seed_list):
            continue                                          # a run over some other list (may overlap a little)
        return {L(r["token"]): r["symbol"] for r in rows
                if L(r["token"]) in seed_list and r.get("status") in ("SAFE", "PROXY_WARNING")}
    return {}


def summarize(arbs, hours, seed):
    """arbs: list of dicts (tokens list, bot, net_usd, gross_usd, priority_usd, reachable, ...). Pure (unit-tested)."""
    per_day = 24 / hours if hours else 0
    total = sum(a["net_usd"] for a in arbs)

    def bucket(a):
        toks = [t for t in a["tokens"] if t != BASE_PAIR]
        if not toks:
            return "base_pair"
        return "seed" if all(t in seed for t in toks) else "other"

    split = defaultdict(lambda: {"arbs": 0, "net_usd_day": 0.0})
    for a in arbs:
        b = split[bucket(a)]
        b["arbs"] += 1
        b["net_usd_day"] += a["net_usd"] * per_day
    by_token = defaultdict(lambda: {"arbs": 0, "net": 0.0, "bots": Counter()})
    for a in arbs:                     # an arb's profit is split evenly across the tokens it went through
        for t in a["tokens"]:
            share = a["net_usd"] / len(a["tokens"])
            by_token[t]["arbs"] += 1
            by_token[t]["net"] += share
            by_token[t]["bots"][a["bot"]] += share
    bots = Counter()
    for a in arbs:
        bots[a["bot"]] += a["net_usd"]

    def top3(counter, whole):
        top = counter.most_common(3)
        return {"bots": [(b, v) for b, v in top], "share": (sum(v for _, v in top) / whole) if whole > 0 else None}

    tokens = sorted(by_token.items(), key=lambda kv: -kv[1]["net"])
    pct = [a["priority_usd"] / a["gross_usd"] for a in arbs if a["gross_usd"] > 0]
    gas_pct = [(a["l2_gas_usd"] + a["l1_fee_usd"]) / a["gross_usd"] for a in arbs if a["gross_usd"] > 0]
    reach = [a for a in arbs if a.get("reachable")]
    return {
        "hours": hours, "arbs": len(arbs), "arbs_day": len(arbs) * per_day, "net_usd_day": total * per_day,
        "gross_usd_day": sum(a["gross_usd"] for a in arbs) * per_day,
        "losing_arbs": sum(a["net_usd"] < 0 for a in arbs),
        "split": dict(split),
        "reachable_net_usd_day": sum(a["net_usd"] for a in reach) * per_day,
        "reachable_other_net_usd_day": sum(a["net_usd"] for a in reach if bucket(a) == "other") * per_day,
        "hooked_net_usd_day": sum(a["net_usd"] for a in arbs if a.get("hooked")) * per_day,
        "backrun_share": (sum(bool(a.get("prev_same_pool")) for a in arbs) / len(arbs)) if arbs else None,
        "distinct_bots": len(bots), "top3_overall": top3(bots, total),
        "top_tokens": [{"token": t, "arbs": v["arbs"], "net_usd_day": v["net"] * per_day,
                        "top3": top3(v["bots"], v["net"])} for t, v in tokens[:30]],
        "median_priority_pct_of_gross": statistics.median(pct) if pct else None,
        "median_gas_pct_of_gross": statistics.median(gas_pct) if gas_pct else None,
        "median_net_usd": statistics.median([a["net_usd"] for a in arbs]) if arbs else None,
    }


def load_arbs(db):
    cols = [c[1] for c in db.execute("PRAGMA table_info(arbs)")]
    out = []
    for row in db.execute("SELECT * FROM arbs"):
        a = dict(zip(cols, row))
        a["tokens"], a["venues"] = json.loads(a["tokens"]), json.loads(a["venues"])
        out.append(a)
    return out


def symbols(db, rpc, tokens):
    known = dict(db.execute("SELECT token, symbol FROM symbols"))
    need = [t for t in tokens if t not in known and t != BASE_PAIR]
    if need and rpc:
        for t, raw in zip(need, multicall(rpc, [(t, "0x" + scanner.selector("symbol()")) for t in need])):
            sym = "?"
            try:
                sym = decode(["string"], raw)[0] if raw and len(raw) >= 64 else raw.rstrip(b"\0").decode()
            except Exception:
                pass
            known[t] = "".join(c for c in sym if c.isprintable())[:20] or "?"
            db.execute("INSERT OR REPLACE INTO symbols VALUES (?, ?)", (t, known[t]))
        db.commit()
    return known | {BASE_PAIR: BASE_PAIR}


def print_report(db, rpc=None):
    rng = get_meta(db, "range")
    if not rng:
        print("no data: run `python arb_history.py --hours 24 --rpc ...` first")
        return None
    done = sum(e - s + 1 for s, e in db.execute("SELECT start, end FROM chunks"))
    hours = done / BLOCKS_PER_HOUR
    seed = safe_seed_tokens()
    arbs = load_arbs(db)
    s = summarize(arbs, hours, seed)
    reasons = Counter(r for (r,) in db.execute("SELECT reason FROM checked"))
    multi = db.execute("SELECT COUNT(*) FROM (SELECT tx FROM swaps GROUP BY tx HAVING COUNT(DISTINCT pool)>=2)").fetchone()[0]
    unresolved = db.execute("SELECT COUNT(DISTINCT s.pool) FROM swaps s LEFT JOIN pools p ON s.pool=p.pool "
                            "WHERE p.pool IS NULL AND s.tx IN (SELECT tx FROM swaps GROUP BY tx "
                            "HAVING COUNT(DISTINCT pool)>=2)").fetchone()[0]
    sym = symbols(db, rpc, [t["token"] for t in s["top_tokens"]] + [b for b, _ in s["top3_overall"]["bots"]])
    swaps = db.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    print(f"\nArb history: blocks {rng[0]}..{rng[1]}, {hours:.1f} h ingested "
          f"({done:,}/{rng[1] - rng[0] + 1:,} blocks), RPC {get_meta(db, 'rpc')}")
    print(f"  {swaps:,} swaps, {multi:,} txs on 2+ pools, {sum(reasons.values()):,} loop-shaped judged: "
          f"{dict(reasons.most_common())}")
    if unresolved:
        n, est = db.execute("SELECT COUNT(*), SUM(MAX(est_usd, 0)) FROM checked WHERE reason='unknown_pool'").fetchone()
        print(f"  {unresolved} V4 pools unresolved (not in the PositionManager): {n or 0:,} txs touch them and are NOT"
              f" in the totals; rough WETH/USDC gain seen at their tx.from/to: ${(est or 0) * 24 / hours:,.0f}/day"
              f" before gas (UNVERIFIED: native ETH paid in is invisible, so user trades can inflate it)")
    print(f"\nAtomic arbs: {s['arbs']:,} ({s['arbs_day']:,.0f}/day), {s['distinct_bots']} bot contracts, "
          f"{s['losing_arbs']} lost money after gas")
    print(f"  net profit after gas: ${s['net_usd_day']:,.0f}/day   (gross ${s['gross_usd_day']:,.0f}/day, "
          f"median arb ${s['median_net_usd'] or 0:,.2f})")
    names = {"seed": f"our {len(seed)} safe seed tokens", "base_pair": "WETH/USDC only", "other": "everything else"}
    for k in ("seed", "base_pair", "other"):
        b = s["split"].get(k, {"arbs": 0, "net_usd_day": 0})
        print(f"    {names[k]:<26} {b['arbs']:>7,} arbs   ${b['net_usd_day']:>10,.0f}/day")
    print(f"  reachable by us (all legs on univ3/univ4-hookless/aero/cl, no hooks): ${s['reachable_net_usd_day']:,.0f}/day"
          f" (non-seed ${s['reachable_other_net_usd_day']:,.0f}/day); via hooked V4 pools: ${s['hooked_net_usd_day']:,.0f}/day")
    t3 = s["top3_overall"]
    print(f"  top 3 bots take {t3['share'] * 100:.0f}% of net profit:" if t3["share"] is not None else "  top 3 bots: n/a")
    for b, v in t3["bots"]:
        print(f"    {b}  ${v * 24 / hours:,.0f}/day")
    if s["median_priority_pct_of_gross"] is not None:
        print(f"  median priority fee = {s['median_priority_pct_of_gross'] * 100:.1f}% of gross profit; "
              f"median all-in gas = {s['median_gas_pct_of_gross'] * 100:.1f}%; "
              f"previous tx hit the same pool in {s['backrun_share'] * 100:.0f}% of arbs (backruns)")
    print("\n  top tokens by arb profit:")
    print(f"    {'token':<12}{'address':<44}{'arbs':>7}{'net $/day':>12}  top-3 bots' share  seed?")
    for t in s["top_tokens"]:
        share = t["top3"]["share"]
        print(f"    {sym.get(t['token'], '?'):<12}{t['token']:<44}{t['arbs']:>7,}{t['net_usd_day']:>12,.0f}  "
              f"{(f'{share * 100:.0f}%' if share is not None else 'n/a'):>17}  {'yes' if t['token'] in seed else ''}")
    att = print_attempts(db, hours)
    if att:
        s["attempts"] = att
    print("\n  Blind spots: " + ("" if att else "reverted and no-op arb attempts aren't counted (run --attempts N), "
          "so bots' true net is lower;\n  ") + "loops mostly through PancakeSwap/Balancer/Curve are missed.\n"
          "  The price of non-base tokens comes from each tx's own swaps.")
    return s


# ---------------------------------------------------------------------------------------------------------------------
# One transaction (validation)

def explain_tx(rpc, tx, eth_usd=None, log=print):
    receipt = rpc.call("eth_getTransactionReceipt", [tx])
    blk = int(receipt["blockNumber"], 16)
    swaps = [s for lg in receipt["logs"] if (s := decode_swap(lg))]
    plain = {s["pool"]: s["src"] for s in swaps if s["src"] != "v4"}
    meta = pool_meta_v2v3(rpc, list(plain), plain) if plain else {}
    ids = list(dict.fromkeys(s["pool"] for s in swaps if s["src"] == "v4"))
    if ids:
        found, _ = pool_meta_v4(rpc, ids)
        meta |= found
    if eth_usd is None:
        anchor = anchor_address(rpc)
        try:
            raw = rpc.call("eth_call", [{"to": anchor, "data": "0x" + scanner.selector("slot0()")}, hex(blk - 1)])
        except RpcError:              # no archive state: fall back to the current price
            raw = rpc.call("eth_call", [{"to": anchor, "data": "0x" + scanner.selector("slot0()")}, "latest"])
        eth_usd = sqrtp_to_eth_usd(int(raw[2:66], 16))
    base_fee = int(rpc.call("eth_getBlockByNumber", [hex(blk), False])["baseFeePerGas"], 16)
    r = classify(receipt, meta, eth_usd, base_fee)
    if r["arb"] and r["txi"] > 0:
        prev = rpc.call("eth_getTransactionByBlockNumberAndIndex", [hex(blk), hex(r["txi"] - 1)])
        prev_r = rpc.call("eth_getTransactionReceipt", [prev["hash"]])
        r["prev_same_pool"] = any(s["pool"] in r["pools"] for lg in prev_r["logs"] if (s := decode_swap(lg)))
    return r


def main():
    ap = argparse.ArgumentParser(description="Measure the real atomic-arb market on Base (read-only)")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--to-block", type=int, help="window ends here (default: head - %d)" % FINALITY_LAG)
    ap.add_argument("--rpc", default=os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
                    help="upstream with eth_getLogs (default BASE_RPC_URL, else the public RPC)")
    ap.add_argument("--max-batch", type=int, default=1,
                    help="JSON-RPC batch size (dRPC free fails part of any batch > 1: parallel singles are faster)")
    ap.add_argument("--threads", type=int, default=4, help="parallel receipt fetchers")
    ap.add_argument("--receipt-rpc", default="",
                    help="comma-separated extra endpoints that share the receipt and archive eth_call load (no eth_getLogs needed: V4 Initialize blocks are located with eth_call)")
    ap.add_argument("--span", type=int, default=100, help="eth_getLogs block range per request (dRPC free: 100)")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--rejudge", action="store_true", help="re-classify every candidate (stored receipts, no refetch)")
    ap.add_argument("--json", help="also write the report summary as JSON here")
    ap.add_argument("--tx", help="classify one transaction and print the breakdown")
    ap.add_argument("--attempts", type=int, default=0, metavar="N",
                    help="read every N-th block's receipts (eth_getBlockReceipts; --receipt-rpc endpoints) to count "
                         "the known bots' reverted and no-op txs and their gas")
    args = ap.parse_args()
    rpc = Rpc(args.rpc, max_batch=args.max_batch)
    if args.tx:
        print(json.dumps(explain_tx(rpc, args.tx), indent=2, default=str))
        return
    db = open_db(args.db)
    if not args.report:
        extra = [u for u in args.receipt_rpc.split(",") if u]
        receipt_rpc = Rpc(args.rpc, max_batch=args.max_batch, extra=extra) if extra else None
        collect(rpc, args.hours, db, to_block=args.to_block, span=args.span, rejudge=args.rejudge, threads=args.threads,
                receipt_rpc=receipt_rpc)
    if args.attempts:
        extra = [u for u in args.receipt_rpc.split(",") if u]
        scan_attempts(db, Rpc(extra[0], extra=extra[1:], tries=20) if extra else rpc, every=args.attempts, threads=args.threads)
    s = print_report(db, rpc)
    if args.json and s:
        Path(args.json).write_text(json.dumps(s, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
