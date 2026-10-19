"""Block-by-block gap monitor: does money exist between Base pools, and for how long?

The pipeline looks at each token once per ~15-minute cycle. Gaps between pools live for seconds, so this watches
every new block instead:
  1. Load the tokens that passed safety (SAFE / PROXY_WARNING) from the latest pipeline results.jsonl, with their
     qualifying pools. No safety re-checks here.
  2. On each new block, read every pool's mid price in ONE batched call (Multicall3) -> gross spread for every
     2-pool route (buy in pool A, sell in pool B), before fees.
  3. Fully quote routes, most promising first (gross spread minus fees), at $100/$250/$500/$1,000/$2,500, in next-block
     context, using scanner.py's quote encoding and decoding. Fees, price impact, measured token tax and gas included.
  4. Write everything to logs/opportunities.db, one run_id per run. A route not fully quoted in a block gets NULL nets
     (never a guess). Every block from the run's first to last head is either 'done' or counted as missed, so
     outages and slow blocks always show. Slipstream fees are read live every block (they're dynamic).

Read-only: only eth_call against BASE_RPC_URL (from .env, never printed). Nothing is ever signed or sent.

    python gap_monitor.py --minutes 120      # watch
    python gap_monitor.py --report           # what did we see?
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from itertools import permutations
from pathlib import Path

from dotenv import load_dotenv
from eth_abi import decode, encode
from web3 import Web3

import scanner
from scanner import BASES, USDC, WETH, Pool, cs

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "logs" / "opportunities.db"
LOG_DIR = ROOT / "logs"
PUBLIC_RPC = "https://mainnet.base.org"
MULTICALL3 = cs("0xcA11bde05977b3631167028862bE2a173976CA11")   # same address on every chain; code verified on Base
AGGREGATE3 = "0x" + scanner.selector("aggregate3((address,bool,bytes)[])")
SIZES = (100, 250, 500, 1000, 2500)          # USD trade sizes tried for every fully quoted route
CHUNK = 40                                   # quotes per Multicall3 eth_call (keeps each call well under RPC gas caps)
CHUNK_GAS = 50_000_000
PARALLEL = 8                                 # concurrent eth_calls per round
BLOCK_BUDGET_MS = 1600                       # aim to finish each block's work inside one 2s Base block
POLL_SECONDS = 0.25


# ---------------------------------------------------------------- setup

def rpc_url():
    """BASE_RPC_URL from .env / environment, else the public endpoint. Returns (url, is_private)."""
    load_dotenv(ROOT / ".env")
    url = os.getenv("BASE_RPC_URL")
    return (url, True) if url else (PUBLIC_RPC, False)


redact = scanner.redact_url


def latest_results(log_dir=LOG_DIR):
    files = sorted(Path(log_dir).glob("*/results.jsonl"), key=lambda f: f.stat().st_mtime)
    if not files:
        raise SystemExit("no pipeline results found: run `python pipeline.py` first")
    return files[-1]


def load_targets(w3, results_path, min_liquidity=50_000):
    """Tokens that passed safety in that pipeline run, with their qualifying pools and measured taxes.
    Uses the pools stored in results.jsonl when present, else rediscovers them (pool discovery only, no safety)."""
    targets = []
    eth_usd = scanner.eth_usd_mid(w3)
    for line in Path(results_path).read_text().splitlines():
        r = json.loads(line)
        if r.get("status") not in ("SAFE", "PROXY_WARNING"):
            continue
        token = cs(r["token"])
        if r.get("pools"):
            pools = [Pool(**p) for p in r["pools"]]
        else:
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
                pools = scanner.filter_pools_by_liquidity(w3, scanner.find_pools(w3, token), token, min_liquidity,
                                                          eth_usd)
        if len(pools) < 2:
            continue
        taxes = r.get("taxes") or {}
        targets.append({"token": token, "symbol": r.get("symbol") or token[:8],
                        "decimals": scanner.erc20(w3, token).functions.decimals().call(),
                        "pools": pools, "buy_tax": taxes.get("buy"), "sell_tax": taxes.get("sell")})
    return targets


def route_pool_label(pool):
    """Pool label without a fee for Slipstream (its fee changes block to block; the per-block fee is in the DB)."""
    if pool.kind == "cl":
        return f"Aero {pool.label.split()[1]} /{BASES[pool.base][0]}"
    return pool.label


def build_routes(targets, anchor):
    """Every ordered 2-pool route per token (settle native: 2 swaps, or 3 with the anchor when bases differ)."""
    routes = []
    for t in targets:
        for a, b in permutations(t["pools"], 2):
            start, legs = scanner.build_legs(a, b, t["token"], anchor, "native")
            path = " > ".join([BASES[start][0]] + [t["symbol"] if o == t["token"] else BASES[o][0] for _, _, o in legs])
            routes.append({"id": f"{t['token']}|{a.address}|{b.address}", "target": t, "a": a, "b": b,
                           "start": start, "legs": legs,
                           "label": f"{t['symbol']}: {route_pool_label(a)} -> {route_pool_label(b)}", "path": path})
    return routes


# ---------------------------------------------------------------- batched eth_call

class Batcher:
    """Runs many (target, calldata) reads as Multicall3 aggregate3 eth_calls, chunked and in parallel, all pinned to
    one block and evaluated in next-block context (block overrides), like scanner.call_next_block."""

    def __init__(self, w3):
        self.w3 = w3
        self.overrides_ok = True
        self.pool = ThreadPoolExecutor(max_workers=PARALLEL)
        self.calls_made = 0

    def _one(self, chunk, block):
        calls = [(t, True, bytes.fromhex(d[2:]) if isinstance(d, str) else bytes(d)) for t, d in chunk]
        data = AGGREGATE3 + encode(["(address,bool,bytes)[]"], [calls]).hex()
        tx = {"to": MULTICALL3, "data": data, "gas": hex(CHUNK_GAS)}
        tag = hex(block.number)
        params = [tx, tag, {}, scanner.next_block_overrides(block)] if self.overrides_ok else [tx, tag]
        resp = self.w3.provider.make_request("eth_call", params)
        self.calls_made += 1
        if "error" in resp:
            # Transient (lagging node, rate limit, timeout): fail this block so it counts as missed. Never silently
            # switch modes mid-run; whether overrides work is decided once, by probe_overrides().
            raise RuntimeError(f"eth_call failed: {str(resp['error'])[:200]}")
        results = decode(["(bool,bytes)[]"], bytes.fromhex(resp["result"][2:]))[0]
        return [ret if ok else None for ok, ret in results]

    def probe_overrides(self, block, attempts=3):
        """Decide once, at startup, whether this RPC supports eth_call block overrides: the same tiny call with and
        without them. Overrides are off only if the plain call works and the override call keeps failing."""
        tx = {"to": MULTICALL3, "data": AGGREGATE3 + encode(["(address,bool,bytes)[]"], [[]]).hex()}
        for _ in range(attempts):
            plain = self.w3.provider.make_request("eth_call", [tx, hex(block.number)])
            with_ov = self.w3.provider.make_request("eth_call", [tx, hex(block.number), {},
                                                                 scanner.next_block_overrides(block)])
            if "result" in with_ov:
                self.overrides_ok = True
                return True
            if "result" in plain:
                self.overrides_ok = False
            time.sleep(1)
        return self.overrides_ok

    def run(self, requests, block):
        """requests: list of (target, calldata). Returns raw return bytes (None for a failed sub-call), same order."""
        chunks = [requests[i:i + CHUNK] for i in range(0, len(requests), CHUNK)]
        out = []
        for res in self.pool.map(lambda c: self._one(c, block), chunks):
            out.extend(res)
        return out


# ---------------------------------------------------------------- mid prices (gross spread)

SLOT0 = "0x3850c7bd"
GET_RESERVES = "0x" + scanner.selector("getReserves()")


def mid_request(pool):
    if pool.kind == "univ4":     # singleton: the pool's slot0 lives in the PoolManager, read through StateView
        view = Web3().eth.contract(address=scanner.UNI_V4_STATE_VIEW, abi=scanner.V4_STATE_VIEW_ABI)
        return (scanner.UNI_V4_STATE_VIEW, view.functions.getSlot0(pool.address)._encode_transaction_data())
    return (pool.address, SLOT0 if pool.kind in ("univ3", "cl") else GET_RESERVES)


def stable_marginal_price(x, y):
    """Marginal price dy/dx of Aerodrome's stable curve x^3*y + x*y^3 = k at reserves (x, y), both normalized."""
    return (3 * x * x * y + y ** 3) / (x ** 3 + 3 * x * y * y)


def mid_price(pool, raw, token, token_dec):
    """Price of 1 token in the pool's base asset (human units), from slot0 or reserves. None if unreadable."""
    if raw is None:
        return None
    base_dec = BASES[pool.base][1]
    base_currency = scanner.v4_currency(pool, pool.base) if pool.kind == "univ4" else pool.base   # native ETH = 0x0
    token_is_0 = int(token, 16) < int(base_currency, 16)      # pools sort their two tokens by address
    if pool.kind in ("univ3", "cl", "univ4"):
        sqrt_p = int.from_bytes(bytes(raw)[:32], "big")
        if sqrt_p == 0:
            return None
        raw_price = (sqrt_p / 2**96) ** 2                     # token1 per token0, smallest units
        if token_is_0:
            return raw_price * 10 ** (token_dec - base_dec)
        return 1 / (raw_price * 10 ** (base_dec - token_dec))
    r0, r1 = decode(["uint256", "uint256"], bytes(raw)[:64])
    r_tok, r_base = (r0, r1) if token_is_0 else (r1, r0)
    if r_tok == 0 or r_base == 0:
        return None
    x, y = r_tok / 10**token_dec, r_base / 10**base_dec
    return stable_marginal_price(x, y) if pool.stable else y / x


def gross_spread_bps(route, mids, eth_usd):
    """Sell price in B over buy price in A, minus 1, in bps, both in USD. Before any fee or price impact."""
    pa, pb = mids.get(route["a"].address), mids.get(route["b"].address)
    if not pa or not pb:
        return None
    usd_a = pa * scanner.usd_price(route["a"].base, eth_usd)
    usd_b = pb * scanner.usd_price(route["b"].base, eth_usd)
    return (usd_b / usd_a - 1) * 10_000


# ---------------------------------------------------------------- full quotes

def quote_routes(w3, batcher, routes, block, eth_usd, gas, sizes=SIZES):
    """Fully quote (route, size) pairs through every leg, one batched round per leg. Returns
    {route_id: {size: net_usd or None}}. Leg-by-leg: each leg's exact output is the next leg's input."""
    state = {}   # (route_id, size) -> amount in hand (smallest units) or None if dead
    for r in routes:
        for size in sizes:
            state[(r["id"], size)] = scanner.usd_to_base_units(size, r["start"], eth_usd)
    starts = dict(state)
    max_legs = max((len(r["legs"]) for r in routes), default=0)
    by_id = {r["id"]: r for r in routes}
    for i in range(max_legs):
        keys, reqs, index = [], [], {}
        for (rid, size), amount in state.items():
            r = by_id[rid]
            if amount is None or i >= len(r["legs"]) or amount <= 0:
                continue
            pool, t_in, t_out = r["legs"][i]
            req = scanner.quote_request(w3, pool, t_in, t_out, amount)
            if req not in index:                       # identical quotes (same pool, direction, amount) asked once
                index[req] = len(reqs)
                reqs.append(req)
            keys.append(((rid, size), index[req], pool, t_in, t_out))
        if not reqs:
            break
        raws = batcher.run(reqs, block)
        for key, idx, pool, t_in, t_out in keys:
            raw = raws[idx]
            out = scanner.decode_quote(w3, pool, raw) if raw else 0
            t = by_id[key[0]]["target"]
            out = scanner.after_tax(out, t_in, t_out, t["token"], t["buy_tax"], t["sell_tax"])
            state[key] = out if out > 0 else None
    nets = {}
    for (rid, size), end in state.items():
        r = by_id[rid]
        dec = BASES[r["start"]][1]
        if end is None:
            net = None
        else:
            net = (end - starts[(rid, size)]) / 10**dec * scanner.usd_price(r["start"], eth_usd) \
                - gas.cost_usd(len(r["legs"]))
        nets.setdefault(rid, {})[size] = net
    return nets


# ---------------------------------------------------------------- per-block fees

FEE = "0x" + scanner.selector("fee()")
ANCHOR_FEE = scanner.ANCHOR_FEE_TIER / 1_000_000
LOSS_MARGIN_BPS = 0.5     # gross must trail fees by at least this much before an unquoted block counts as a sure loss


def fee_requests(pools):
    """Slipstream fees are dynamic (and spike in the block right after a trade), so read each CL pool's fee() in every
    block's batch, in next-block context. In that context pool.fee() equals the factory's getSwapFee (checked live)."""
    return [(p.address, FEE) for p in pools if p.kind == "cl"]


def block_fees(pools, raws):
    """{pool address: fee fraction} for this block: live for Slipstream (None if unreadable), static for the rest."""
    fees, it = {}, iter(raws)
    for p in pools:
        if p.kind == "cl":
            raw = next(it)
            fees[p.address] = int.from_bytes(bytes(raw)[:32], "big") / 1_000_000 if raw else None
        else:
            fees[p.address] = p.fee
    return fees


def route_fee_bps(route, fees):
    total = 0.0
    for pool, _, _ in route["legs"]:
        f = ANCHOR_FEE if pool.address == route.get("anchor_address") else fees.get(pool.address)
        if f is None:
            return None
        total += f
    return total * 10_000


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, ended_at REAL,
    start_block INTEGER, end_block INTEGER, rpc_host TEXT, private INTEGER, overrides INTEGER, tokens INTEGER,
    routes INTEGER, results_file TEXT);
CREATE TABLE IF NOT EXISTS routes (route_id TEXT PRIMARY KEY, symbol TEXT, token TEXT, pool_a TEXT, pool_b TEXT,
    label TEXT, path TEXT, hops INTEGER);
CREATE TABLE IF NOT EXISTS blocks (run_id INTEGER, block INTEGER, ts INTEGER, eth_usd REAL, routes_total INTEGER,
    routes_quoted INTEGER, cycle_ms INTEGER, PRIMARY KEY (run_id, block));
CREATE TABLE IF NOT EXISTS obs (run_id INTEGER, block INTEGER, route_id TEXT, gross_bps REAL, fee_bps REAL,
    net_500 REAL, best_net REAL, best_size INTEGER, PRIMARY KEY (run_id, block, route_id));
CREATE INDEX IF NOT EXISTS obs_route ON obs (run_id, route_id, block);
"""


def open_db(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def save_routes(db, routes):
    db.executemany("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?)",
                   [(r["id"], r["target"]["symbol"], r["target"]["token"], r["a"].address, r["b"].address,
                     r["label"], r["path"], len(r["legs"])) for r in routes])
    db.commit()


def start_run(db, start_block, rpc_host, private, overrides, tokens, routes, results_file):
    cur = db.execute("INSERT INTO runs (started_at, ended_at, start_block, end_block, rpc_host, private, overrides, "
                     "tokens, routes, results_file) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (time.time(), time.time(), start_block, start_block, rpc_host, int(private), int(overrides),
                      tokens, routes, str(results_file)))
    db.commit()
    return cur.lastrowid


def touch_run(db, run_id, head):
    """Every block between start_block and end_block is either 'done' (has a blocks row) or missed: outages, slow
    blocks and RPC errors all count, without any separate bookkeeping."""
    db.execute("UPDATE runs SET end_block = MAX(end_block, ?), ended_at = ? WHERE run_id = ?", (head, time.time(), run_id))
    db.commit()


# ---------------------------------------------------------------- one block

def monitor_block(w3, batcher, db, routes, block, gas, quote_budget, sizes=SIZES, anchor=None, run_id=0):
    """Everything for one block. quote_budget = how many routes to fully quote (most promising first).
    Returns (routes_quoted, cycle_ms, list of (route, best_net, best_size) with best_net > 0)."""
    t0 = time.time()
    anchor = anchor or scanner.anchor_pool(w3)
    pools = {}
    for r in routes:
        r["anchor_address"] = anchor.address
        for p in (r["a"], r["b"]):
            pools[p.address] = (p, r["target"])
    plist = [p for p, _ in pools.values()]
    reqs = [mid_request(anchor)] + [mid_request(p) for p in plist] + fee_requests(plist)
    raws = batcher.run(reqs, block)
    eth_usd = mid_price(anchor, raws[0], WETH, 18)
    if not eth_usd:
        raise RuntimeError("anchor price unreadable")
    mids = {p.address: mid_price(p, raw, t["token"], t["decimals"])
            for (p, t), raw in zip(pools.values(), raws[1:1 + len(plist)])}
    fees = block_fees(plist, raws[1 + len(plist):])
    gas.eth_usd = eth_usd

    spreads = {r["id"]: gross_spread_bps(r, mids, eth_usd) for r in routes}
    fee_bps = {r["id"]: route_fee_bps(r, fees) for r in routes}

    def headroom(r):
        g, f = spreads[r["id"]], fee_bps[r["id"]]
        return -1e9 if g is None or f is None else g - f

    ranked = sorted(routes, key=headroom, reverse=True)
    chosen = [r for r in ranked if headroom(r) > -1e9][:max(0, quote_budget)]
    nets = quote_routes(w3, batcher, chosen, block, eth_usd, gas, sizes) if chosen else {}

    rows, winners = [], []
    for r in routes:
        n = nets.get(r["id"])
        net_500 = best = best_size = None
        if n:
            net_500 = n.get(500)
            valid = {s: v for s, v in n.items() if v is not None}
            if valid:
                best_size = max(valid, key=valid.get)
                best = valid[best_size]
                if best > 0:
                    winners.append((r, best, best_size))
        rows.append((run_id, block.number, r["id"], spreads[r["id"]], fee_bps[r["id"]], net_500, best, best_size))
    cycle_ms = int((time.time() - t0) * 1000)
    db.executemany("INSERT OR REPLACE INTO obs VALUES (?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?,?)",
               (run_id, block.number, block.timestamp, eth_usd, len(routes), len(chosen), cycle_ms))
    db.commit()
    return len(chosen), cycle_ms, winners


def plan_next(last_done, latest):
    """Which block to work on next. None = no new block yet. (Skipped blocks need no bookkeeping: the run's block
    range minus the 'done' blocks is the missed set.)"""
    if last_done is not None and latest <= last_done:
        return None
    return latest


class Budget:
    """How many routes to fully quote per block, adapted so a block's work fits inside one Base block."""

    def __init__(self, total, start=40, target_ms=BLOCK_BUDGET_MS):
        self.total, self.k, self.target = total, min(start, total), target_ms

    def update(self, cycle_ms):
        if cycle_ms > self.target:
            self.k = max(1, int(self.k * 0.75))
        elif cycle_ms < 0.75 * self.target and self.k < self.total:
            self.k = min(self.total, int(self.k * 1.25) + 1)


# ---------------------------------------------------------------- volatility trigger

def biggest_move(samples):
    """Largest high-low range in a window of (time, price) samples, as a fraction of the low."""
    prices = [p for _, p in samples]
    return (max(prices) - min(prices)) / min(prices) if prices else 0.0


def wait_for_move(w3, pct, window_min, max_wait_h, out=sys.stdout, poll=12, clock=time.time, sleep=time.sleep,
                  price=None):
    """Hold off watching until ETH/USD (anchor pool mid) moves `pct` percent within `window_min` minutes: the
    volatile moments when gaps are most likely. Returns (triggered, move_pct). Gives up after `max_wait_h` hours."""
    price = price or (lambda: scanner.eth_usd_mid(w3))
    window, start = deque(), clock()
    out.write(f"waiting for ETH to move {pct}% within {window_min} min (max wait {max_wait_h} h)\n")
    while clock() - start < max_wait_h * 3600:
        try:
            now = clock()
            window.append((now, price()))
            while window and window[0][0] < now - window_min * 60:
                window.popleft()
            move = 100 * biggest_move(window)
            out.write(f"\rETH {window[-1][1]:.2f} | {window_min}-min range {move:.2f}% | waited "
                      f"{(now - start) / 60:.0f} min   ")
            out.flush()
            if move >= pct:
                out.write(f"\nTRIGGERED: ETH moved {move:.2f}% in {window_min} min\n")
                return True, move
        except Exception as e:
            out.write(f"\nwarning: {type(e).__name__}: {str(e)[:120]}\n")
        sleep(poll)
    out.write(f"\nno {pct}% move within {max_wait_h} h\n")
    return False, 100 * biggest_move(window)


# ---------------------------------------------------------------- loop

def watch(minutes, db_path=DB_PATH, results=None, out=sys.stdout):
    url, private = rpc_url()
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
    batcher = Batcher(w3)
    overrides = batcher.probe_overrides(w3.eth.get_block("latest"))
    out.write(f"gap monitor | rpc {redact(url)} ({'private' if private else 'PUBLIC, rate-limited: expect reduced coverage'})"
              f" | next-block context {'ON' if overrides else 'NOT SUPPORTED by this RPC (Slipstream fees read at latest)'}"
              f" | read-only\n")
    results = results or latest_results()
    targets = load_targets(w3, results)
    anchor = scanner.anchor_pool(w3)
    routes = build_routes(targets, anchor)
    db = open_db(db_path)
    save_routes(db, routes)
    gas = scanner.build_gas_model(w3, 0.001, scanner.eth_usd_mid(w3))
    budget = Budget(len(routes))
    start_block = w3.eth.block_number
    run_id = start_run(db, start_block, redact(url), private, overrides, len(targets), len(routes), results)
    out.write(f"run {run_id}: watching {len(targets)} tokens, {len(routes)} routes from {results} for {minutes} min\n")
    deadline, last_done, n_done, slow, errors = time.time() + minutes * 60, None, 0, 0, 0
    head = start_block
    while time.time() < deadline:
        try:
            head = w3.eth.block_number
            touch_run(db, run_id, head)
            target = plan_next(last_done, head)
            if target is None:
                time.sleep(POLL_SECONDS)
                continue
            block = w3.eth.get_block(target)
            if n_done and n_done % 300 == 0:   # refresh base fee + L1 fee about every 10 minutes
                gas = scanner.build_gas_model(w3, 0.001, scanner.eth_usd_mid(w3))
            quoted, cycle_ms, winners = monitor_block(w3, batcher, db, routes, block, gas, budget.k, anchor=anchor,
                                                      run_id=run_id)
            budget.update(cycle_ms)
            slow += cycle_ms > 2000
            last_done, n_done = target, n_done + 1
            for r, best, size in winners:
                if best > 1:
                    out.write(f"\n!! block {target}: {r['label']} {r['path']} net ${best:+.2f} at ${size}\n")
            missed = head - start_block + 1 - n_done
            out.write(f"\rblock {target} | {quoted}/{len(routes)} routes quoted in {cycle_ms} ms | "
                      f"done {n_done} missed {max(0, missed)} | over-budget {slow} | errors {errors}   ")
            out.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:   # RPC hiccup: the block simply isn't 'done', so it counts as missed
            errors += 1
            out.write(f"\nwarning: {type(e).__name__}: {str(e)[:150]}\n")
            time.sleep(1)
    try:
        touch_run(db, run_id, w3.eth.block_number)   # blocks until the very end count, so trailing outages show
    except Exception:
        pass
    out.write(f"\nstopped run {run_id}: {n_done} blocks done, {slow} over the 2s block time, {errors} errors. "
              f"Report: python gap_monitor.py --report\n")
    return run_id


# ---------------------------------------------------------------- report

def classify(o, fee_fallback=None):
    """One route-block: 'win' (quoted, best net > 0), 'loss' (quoted <= 0, or unquoted with gross provably below
    fees), 'unknown' (unquoted but gross beat fees, or prices/fees unreadable)."""
    _, gross, fee, best = o[0], o[1], o[2], o[3]
    if best is not None:
        return "win" if best > 0 else "loss"
    if gross is None or fee is None:
        return "unreadable"
    return "loss" if gross <= fee - LOSS_MARGIN_BPS else "unknown"


def longest_run(blocks_values, threshold):
    """Longest streak of consecutive block numbers whose value > threshold (a gap or a NULL breaks the streak)."""
    best = run = 0
    prev = None
    for block, value in blocks_values:
        if value is not None and value > threshold and prev is not None and block == prev + 1 and run > 0:
            run += 1
        elif value is not None and value > threshold:
            run = 1
        else:
            run = 0
        best = max(best, run)
        prev = block
    return best


def longest_bridged_run(observations):
    """Longest span (in blocks) from one profitable block to a later one with no observed LOSS in between. Missed or
    unknown blocks inside the span don't break it: they weren't seen, so they can't prove the gap closed.
    observations: [(block, classification)] sorted by block."""
    best, first = 0, None
    last_win = None
    for block, kind in observations:
        if kind == "win":
            if first is None:
                first = block
            last_win = block
            best = max(best, last_win - first + 1)
        elif kind == "loss":
            first = last_win = None
    return best


def report(db_path=DB_PATH, top=25, out=sys.stdout, run_id=None):
    db = sqlite3.connect(db_path)
    if run_id is None:
        row = db.execute("SELECT MAX(run_id) FROM runs").fetchone()
        run_id = row[0] if row else None
    run = db.execute("SELECT started_at, ended_at, start_block, end_block, rpc_host, private, overrides, tokens, routes "
                     "FROM runs WHERE run_id = ?", (run_id,)).fetchone() if run_id else None
    if not run:
        out.write("no runs recorded yet\n")
        return []
    t0, t1, lo, hi, host, private, overrides, n_tokens, n_routes = run
    done = db.execute("SELECT COUNT(*) FROM blocks WHERE run_id = ?", (run_id,)).fetchone()[0]
    span = hi - lo + 1
    missed = max(0, span - done)
    cycle = [c for (c,) in db.execute("SELECT cycle_ms FROM blocks WHERE run_id = ?", (run_id,))]
    cov = db.execute("SELECT AVG(routes_quoted * 1.0 / routes_total) FROM blocks WHERE run_id = ?", (run_id,)).fetchone()[0]
    eth = db.execute("SELECT ts, eth_usd FROM blocks WHERE run_id = ? AND eth_usd IS NOT NULL ORDER BY block",
                     (run_id,)).fetchall()
    move_line = ""
    if eth:
        lo_p, hi_p = min(p for _, p in eth), max(p for _, p in eth)
        win, worst = deque(), 0.0
        for ts, p in eth:                       # biggest 30-minute range during the run
            win.append((ts, p))
            while win[0][0] < ts - 1800:
                win.popleft()
            worst = max(worst, biggest_move(win))
        move_line = (f"ETH during the run: ${lo_p:,.0f}-${hi_p:,.0f} ({100 * (hi_p - lo_p) / lo_p:.2f}% range; "
                     f"biggest 30-min move {100 * worst:.2f}%).\n")
    out.write(f"Run {run_id}: blocks {lo}..{hi} ({(t1 - t0) / 60:.0f} min) on {host} "
              f"({'private' if private else 'public'} RPC, next-block context {'on' if overrides else 'OFF'}).\n"
              f"{done} of {span} blocks watched ({100 * done / span:.1f}%), {missed} missed. "
              f"Routes fully quoted per watched block: {100 * (cov or 0):.0f}% on average. "
              f"Median block work: {statistics.median(cycle) if cycle else 0:.0f} ms. {n_tokens} tokens, {n_routes} routes.\n"
              f"{move_line}\n")
    rows = []
    for rid, label, path in db.execute("SELECT route_id, label, path FROM routes WHERE route_id IN "
                                       "(SELECT DISTINCT route_id FROM obs WHERE run_id = ?)", (run_id,)).fetchall():
        obs = db.execute("SELECT block, gross_bps, fee_bps, best_net, best_size FROM obs WHERE run_id=? AND route_id=? "
                         "ORDER BY block", (run_id, rid)).fetchall()
        kinds = [(o[0], classify(o)) for o in obs]
        quoted = [o for o in obs if o[3] is not None]
        best = max(quoted, key=lambda o: o[3], default=None)
        fees = [o[2] for o in obs if o[2] is not None]
        rows.append({
            "label": label, "path": path, "watched": len(obs), "quoted": len(quoted),
            "unknown": sum(k == "unknown" for _, k in kinds), "unreadable": sum(k == "unreadable" for _, k in kinds),
            "gt0": sum(o[3] > 0 for o in quoted), "gt1": sum(o[3] > 1 for o in quoted),
            "run0": longest_run([(o[0], o[3]) for o in obs], 0),
            "bridged0": longest_bridged_run(kinds),
            "best": best[3] if best else None, "best_size": best[4] if best else None,
            "best_block": best[0] if best else None,
            "max_headroom": max((o[1] - o[2] for o in obs if o[1] is not None and o[2] is not None), default=None),
            "fee_bps": statistics.median(fees) if fees else None,
        })
    rows.sort(key=lambda r: -1e9 if r["best"] is None else r["best"], reverse=True)
    out.write(f"{'route':<58}{'watched':>8}{'quoted':>7}{'unknwn':>7}{'>$0':>6}{'>$1':>6}{'run':>5}{'span':>6}"
              f"{'best net':>10}{'@size':>7}{'max g-f':>9}{'fee':>6}\n")
    for r in rows[:top]:
        best = "     n/a" if r["best"] is None else f"{r['best']:+8.2f}"
        head = "     n/a" if r["max_headroom"] is None else f"{r['max_headroom']:+7.1f}b"
        fee = "  n/a" if r["fee_bps"] is None else f"{r['fee_bps']:4.1f}b"
        out.write(f"{r['label'][:57]:<58}{r['watched']:>8}{r['quoted']:>7}{r['unknown']:>7}{r['gt0']:>6}{r['gt1']:>6}"
                  f"{r['run0']:>5}{r['bridged0']:>6}{best:>10}{(r['best_size'] or 0):>7}{head:>9}{fee:>6}\n")
    total0 = sum(r["gt0"] for r in rows)
    total1 = sum(r["gt1"] for r in rows)
    unknown = sum(r["unknown"] for r in rows)
    unreadable = sum(r["unreadable"] for r in rows)
    out.write(f"\n{len(rows)} routes. Route-blocks with best net > $0: {total0}; > $1: {total1}.\n"
              f"Not measured: {missed} missed blocks x {n_routes} routes = {missed * n_routes} route-blocks; "
              f"{unknown} unquoted route-blocks where gross spread beat fees (profit possible, not measured); "
              f"{unreadable} with unreadable prices or fees.")
    if missed == 0 and unknown == 0 and unreadable == 0:
        out.write(" -> coverage was complete: every other route-block provably lost money.")
    out.write("\nColumns: run = longest streak of consecutive watched blocks with net > $0; span = longest stretch from "
              "one profitable block to another with no observed loss in between (missed/unmeasured blocks don't break "
              "it); max g-f = best gross spread minus fees (an upper bound on net before impact and gas). Nets include "
              f"fees, price impact, measured tax and gas; 'best' is over ${', $'.join(str(s) for s in SIZES)}. A gap "
              "seen at block end is an UPPER BOUND on what we could capture: other bots see it too.\n")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Block-by-block arbitrage gap monitor for Base (read-only)")
    ap.add_argument("--minutes", type=float, default=120)
    ap.add_argument("--report", action="store_true", help="print the report from logs/opportunities.db and exit")
    ap.add_argument("--run", type=int, help="run id to report (default: latest)")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--results", help="pipeline results.jsonl to load tokens from (default: latest)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--wait-for-move", type=float, metavar="PCT",
                    help="start watching only once ETH moves PCT%% within --move-window minutes")
    ap.add_argument("--move-window", type=float, default=30)
    ap.add_argument("--max-wait", type=float, default=24, help="hours to wait for the move before watching anyway")
    args = ap.parse_args()
    if args.report:
        report(args.db, args.top, run_id=args.run)
        return
    if args.wait_for_move:
        url, _ = rpc_url()
        wait_for_move(Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20})), args.wait_for_move,
                      args.move_window, args.max_wait)
    watch(args.minutes, args.db, args.results)


if __name__ == "__main__":
    main()
