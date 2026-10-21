"""Exchange-vs-Base (CEX-DEX) market: who trades Base WETH/USDC and cbBTC/USDC pools ahead of Coinbase's price?
Read-only: Base logs/receipts over public RPCs and Coinbase's public trade history (no API key, no orders).

For every single-pool swap on those pools in the windows already ingested by `arb_history.py`, the markout is what
the trader gained valued at Coinbase's price h seconds later:  -(pool's base-asset delta x P(t+h) + pool's USDC delta).
A CEX-DEX arbitrageur buys on Base when Base is cheap and hedges on Coinbase, so its swaps have consistently
positive markout. Swaps are grouped by the Swap event's `sender` (the contract that called the pool: a router for
users, the bot's own contract for bots); receipts are sampled per sender for gas and the EOAs behind it.

    python cex_markout.py                          # uses logs/arb_history.db + logs/arb_history_prev48.db
    python cex_markout.py --report

Timing: Base block timestamps are exactly 2 s apart and mark the END of the block's build (flashblock 0 arrives ~1.95 s
before it, the last ~0.3 s before; flashblocks.db, 2026-10-20). A swap is placed at timestamp - 1 s, +/- 1 s.
"""
import argparse
import bisect
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import arb_history as ah

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "logs" / "cex_markout.db"
ARB_DBS = [ROOT / "logs" / "arb_history_prev48.db", ROOT / "logs" / "arb_history.db"]
CB_API = "https://api.exchange.coinbase.com"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
PAIRS = {  # base token -> (Coinbase product, decimals). USDC is valued at $1.
    ah.WETH: ("ETH-USD", 18),
    CBBTC: ("BTC-USD", 8),
}
HORIZONS = (2, 12, 60)
SWAP_DELAY = 1.0          # seconds before the block timestamp a swap executes on average (+/- 1 s)
MIN_TXS = 10              # a sender needs this many swaps to be judged
MIN_WIN_RATE = 0.55       # share of its swaps positive at +12 s
PUBLIC_EOA_SHARE = 0.5    # >= half of its sampled txs from distinct EOAs = public router / user flow, not one bot
RECEIPT_SAMPLE = 200
HEDGE_BPS = 5             # sensitivity: a Coinbase taker fee / hedge slippage of 5 bp on the swap's notional

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (pool TEXT PRIMARY KEY, venue TEXT, factory TEXT, token0 TEXT, token1 TEXT, hooks TEXT);
CREATE TABLE IF NOT EXISTS senders (block INTEGER, logi INTEGER, sender TEXT, PRIMARY KEY (block, logi));
CREATE TABLE IF NOT EXISTS sender_chunks (start INTEGER PRIMARY KEY, end INTEGER);
CREATE TABLE IF NOT EXISTS cex (product TEXT, id INTEGER, t REAL, price REAL, PRIMARY KEY (product, id));
CREATE TABLE IF NOT EXISTS receipts (tx TEXT PRIMARY KEY, sender TEXT, eoa TEXT, tx_to TEXT, status INTEGER,
                                     gas_eth REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(path=DB_PATH):
    db = sqlite3.connect(path, timeout=60)
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------------------------------------------------------------
# Pure helpers

def pair_of(meta):
    """(base token, its index 0/1) for a USDC/WETH-or-native or USDC/cbBTC pool, else None."""
    t = [ah.WETH if x == ah.ZERO else x for x in (meta.get("token0"), meta.get("token1"))]
    for i in (0, 1):
        if t[i] in PAIRS and t[1 - i] == ah.USDC:
            return t[i], i
    return None


def event_sender(log):
    """The Swap event's indexed `sender` (V3/Slipstream, Aerodrome v2 and Uniswap V2 all put it in topics[1])."""
    return "0x" + log["topics"][1][-40:].lower() if len(log["topics"]) > 1 else None


def price_at(series, t):
    """Last Coinbase trade price at or before t. series = (sorted times, prices). None before the first trade."""
    times, prices = series
    i = bisect.bisect_right(times, t) - 1
    return prices[i] if i >= 0 else None


def swap_markout(d_base, d_usdc, decimals, price):
    """Trader's gain in USD at `price`. d_* = the POOL's balance change in raw units (positive = pool received)."""
    return -(d_base / 10 ** decimals * price + d_usdc / 1e6)


def block_time(block, ref_block, ref_ts):
    """Estimated execution time of a swap in `block` (Base: exactly 2 s per block, timestamp = end of build)."""
    return ref_ts + 2 * (block - ref_block) - SWAP_DELAY


def tx_markouts(swaps, meta, series, ref):
    """swaps: rows (block, txi, logi, tx, pool, sender, d0, d1) of ONE tx on target pools. -> dict or None.
    One tx can swap a pool several times; its deltas are summed."""
    block, pool = swaps[0][0], swaps[0][4]
    base, bi = pair_of(meta[pool])
    product, dec = PAIRS[base]
    d = [sum(int(s[6]) for s in swaps), sum(int(s[7]) for s in swaps)]
    d_base, d_usdc = d[bi], d[1 - bi]
    t = block_time(block, *ref)
    p0 = price_at(series[product], t)
    out = {h: price_at(series[product], t + h) for h in HORIZONS}
    if p0 is None or None in out.values() or not d_base or not d_usdc:
        return None
    return {"tx": swaps[0][3], "block": block, "pool": pool, "product": product,
            "notional_usd": abs(d_usdc) / 1e6, "m0": swap_markout(d_base, d_usdc, dec, p0),
            **{f"m{h}": swap_markout(d_base, d_usdc, dec, p) for h, p in out.items()}}


def sender_summary(rows, days, gas_usd=None, eoas=None, arb_bots=()):
    """rows: [(sender, markout dict)]. gas_usd: {sender: avg gas per tx in USD}, eoas: {sender: (distinct, sampled)}.
    -> {"senders": [...], "arb": totals over consistent-positive non-public senders, ...}. Pure.
    A sender is `cexdex` if it has >= MIN_TXS swaps, positive total markout at every horizon and wins >= MIN_WIN_RATE
    of its swaps at +12 s, and isn't public (most of its sampled txs come from distinct EOAs)."""
    gas_usd, eoas, arb_bots = gas_usd or {}, eoas or {}, set(arb_bots)
    by = defaultdict(list)
    for s, m in rows:
        by[s].append(m)
    out = []
    for s, ms in by.items():
        tot = {h: sum(m[f"m{h}"] for m in ms) for h in (0, *HORIZONS)}
        win = sum(m["m12"] > 0 for m in ms) / len(ms)
        notional = sum(m["notional_usd"] for m in ms)
        distinct, sampled = eoas.get(s, (0, 0))
        public = bool(sampled) and distinct / sampled >= PUBLIC_EOA_SHARE
        consistent = len(ms) >= MIN_TXS and all(tot[h] > 0 for h in HORIZONS) and win >= MIN_WIN_RATE
        gas = gas_usd.get(s, 0.0) * len(ms)
        out.append({"sender": s, "txs": len(ms), "notional_usd": notional, "win_rate_12s": win,
                    **{f"markout_{h}s_usd_day": tot[h] / days for h in (0, *HORIZONS)},
                    "gas_usd_day": gas / days, "net_12s_usd_day": (tot[12] - gas) / days,
                    "net_12s_after_hedge_usd_day": (tot[12] - gas - notional * HEDGE_BPS / 1e4) / days,
                    "eoas_in_sample": distinct, "receipts_sampled": sampled, "public": public,
                    "kind": "cexdex" if consistent and not public else "public" if public else "other",
                    "arb_contract": s in arb_bots})
    out.sort(key=lambda r: -r["net_12s_usd_day"])
    arb = [r for r in out if r["kind"] == "cexdex"]
    keys = [f"markout_{h}s_usd_day" for h in (0, *HORIZONS)] + ["gas_usd_day", "net_12s_usd_day",
                                                                 "net_12s_after_hedge_usd_day"]
    tot = {k: sum(r[k] for r in arb) for k in keys}
    tot["notional_usd_day"] = sum(r["notional_usd"] for r in arb) / days
    tot["breakeven_hedge_bps"] = tot["net_12s_usd_day"] / tot["notional_usd_day"] * 1e4 if arb else None
    pos = sorted((max(0.0, r["net_12s_usd_day"]) for r in arb), reverse=True)
    top3 = sum(pos[:3]) / sum(pos) if sum(pos) > 0 else None
    return {"senders": out, "arb": tot, "distinct_cexdex": len(arb), "top3_share": top3,
            "overlap_with_arb_contracts": sum(r["arb_contract"] for r in arb),
            "overlap_net_12s_usd_day": sum(r["net_12s_usd_day"] for r in arb if r["arb_contract"]),
            "all_single_pool_txs": sum(r["txs"] for r in out)}


def decide(summary):
    """The decision rule set before measuring (2026-10-21): stop if the top 3 senders take most of the market."""
    s = summary["top3_share"]
    if s is None or summary["arb"]["net_12s_usd_day"] <= 0:
        return "stop: no positive CEX-DEX market measured"
    return f"stop: top 3 take {s:.0%}" if s > 0.5 else f"continue: top 3 take only {s:.0%}"


# ---------------------------------------------------------------------------------------------------------------------
# Coinbase public trades (no key)

def cb_get(path, tries=8):
    delay = 1.0
    for attempt in range(tries):
        req = urllib.request.Request(CB_API + path, headers={"User-Agent": "defi-arb-bot/cex_markout"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise ConnectionError(f"Coinbase {path.split('?')[0]} kept failing")


def parse_trades(page):
    """Coinbase /trades page -> [(id, unix time, price)]."""
    from datetime import datetime
    return [(int(x["trade_id"]), datetime.fromisoformat(x["time"].replace("Z", "+00:00")).timestamp(),
             float(x["price"])) for x in page]


def trade_id_at(product, t):
    """Smallest trade id at or after unix time t (binary search over ids; `after=N` returns trades below N)."""
    hi = parse_trades(cb_get(f"/products/{product}/trades?limit=1"))[0][0] + 1
    lo = 1
    while hi - lo > 1000:
        mid = (lo + hi) // 2
        page = parse_trades(cb_get(f"/products/{product}/trades?limit=1&after={mid}"))
        if page and page[0][1] >= t:
            hi = mid
        else:
            lo = mid
    return lo


def fetch_cex(db, product, t_start, t_end, threads=4, log=print):
    """Every Coinbase trade of `product` in [t_start, t_end], resumable: pages of 1,000 by trade id."""
    a, b = trade_id_at(product, t_start), trade_id_at(product, t_end) + 1000
    pages = [c for c in range(a + 1000, b + 1001, 1000) if db.execute(
        "SELECT COUNT(*) FROM cex WHERE product=? AND id >= ? AND id < ?", (product, c - 1000, c)).fetchone()[0] < 1000]
    log(f"  {product}: trade ids {a:,}..{b:,} ({b - a:,} trades), {len(pages)} pages to fetch")

    def page(c):
        time.sleep(0.25 * threads)            # stay well under the public limit
        return parse_trades(cb_get(f"/products/{product}/trades?limit=1000&after={c}"))

    with ThreadPoolExecutor(threads) as ex:
        for n, rows in enumerate(ex.map(page, pages), 1):
            db.executemany("INSERT OR IGNORE INTO cex VALUES (?,?,?,?)", [(product, *r) for r in rows])
            if n % 50 == 0:
                db.commit()
                log(f"\r  {product}: {n}/{len(pages)} pages   ", end="")
    db.commit()
    got = db.execute("SELECT COUNT(*) FROM cex WHERE product=? AND id BETWEEN ? AND ?", (product, a, b)).fetchone()[0]
    log(f"\n  {product}: {got:,} trades stored, {b - a + 1 - got:,} ids missing")


def load_series(db, product):
    rows = db.execute("SELECT t, price FROM cex WHERE product=? ORDER BY t, id", (product,)).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


# ---------------------------------------------------------------------------------------------------------------------
# Base side

def window(arb_dbs):
    lo = min(sqlite3.connect(p).execute("SELECT MIN(start) FROM chunks").fetchone()[0] for p in arb_dbs)
    hi = max(sqlite3.connect(p).execute("SELECT MAX(end) FROM chunks").fetchone()[0] for p in arb_dbs)
    return lo, hi


def single_pool_swaps(arb_db):
    """{tx: [swap rows]} for txs that touched exactly one pool."""
    out = defaultdict(list)
    for r in arb_db.execute("SELECT block, txi, logi, tx, pool, sender, d0, d1, src FROM swaps WHERE tx IN "
                            "(SELECT tx FROM swaps GROUP BY tx HAVING COUNT(DISTINCT pool) = 1)"):
        out[r[3]].append(r)
    return out


def resolve_missing_pools(db, arb_dbs, rpc, log=print):
    """Pool metadata for pools seen only in single-pool txs (arb_history resolves only pools in 2+-pool txs)."""
    meta = {}
    for p in arb_dbs:
        meta |= ah.load_pool_meta(sqlite3.connect(p))
    meta |= ah.load_pool_meta(db)
    need = {}
    for p in arb_dbs:
        for pool, src in sqlite3.connect(p).execute("SELECT DISTINCT pool, src FROM swaps"):
            if pool not in meta:
                need[pool] = src
    plain = [p for p, s in need.items() if s != "v4"]
    v4 = [p for p, s in need.items() if s == "v4"]
    log(f"  pool metadata: {len(plain)} contracts + {len(v4)} V4 ids not resolved by arb_history")
    for i in range(0, len(plain), 3000):
        ah.save_pool_meta(db, ah.pool_meta_v2v3(rpc, plain[i:i + 3000], need))
        db.commit()
    if v4:                                    # PositionManager only: hook-launchpad ids are never USDC/WETH majors
        found, _ = ah.pool_meta_v4(rpc, v4, log=log)
        ah.save_pool_meta(db, found)
        db.commit()
    return meta | ah.load_pool_meta(db)


def fetch_senders(db, rpc, pools, lo, hi, threads=4, log=print, chunk=1000):
    """Swap-event senders of the non-V4 target pools (arb_history doesn't keep them). Resumable by 1,000 blocks."""
    done = {s for (s,) in db.execute("SELECT start FROM sender_chunks")}
    todo = [b for b in range(lo, hi + 1, chunk) if b not in done]
    flt = {"address": sorted(pools), "topics": [[ah.T_V3, ah.T_AERO, ah.T_UNI2]]}

    def pull(b):
        logs, _ = ah.fetch_logs(rpc, flt, b, min(hi, b + chunk - 1), 100)
        return b, [(int(lg["blockNumber"], 16), int(lg["logIndex"], 16), event_sender(lg)) for lg in logs]

    with ThreadPoolExecutor(threads) as ex:
        for n, (b, rows) in enumerate(ex.map(pull, todo), 1):
            db.executemany("INSERT OR IGNORE INTO senders VALUES (?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO sender_chunks VALUES (?,?)", (b, min(hi, b + chunk - 1)))
            db.commit()
            log(f"\r  swap senders: {n}/{len(todo)} chunks   ", end="")
    log("")


def sample_receipts(db, rpc, by_sender, threads=6, log=print):
    """Up to RECEIPT_SAMPLE receipts per sender (evenly spread over its txs): gas, tx.from, tx.to."""
    have = {t for (t,) in db.execute("SELECT tx FROM receipts")}
    todo = []
    for s, txs in by_sender.items():
        step = max(1, len(txs) // RECEIPT_SAMPLE)
        todo += [(s, t) for t in txs[::step][:RECEIPT_SAMPLE] if t not in have]
    log(f"  receipts: {len(todo):,} to fetch for {len(by_sender)} senders")

    def one(item):
        s, tx = item
        try:
            r = rpc.call("eth_getTransactionReceipt", [tx])
        except (ah.RpcError, ConnectionError):
            return None
        if not r:
            return None
        gas = (int(r["gasUsed"], 16) * int(r.get("effectiveGasPrice", "0x0"), 16) + int(r.get("l1Fee", "0x0"), 16)) / 1e18
        return tx, s, ah.L(r["from"]), ah.L(r.get("to") or ""), int(r["status"], 16), gas

    with ThreadPoolExecutor(threads) as ex:
        for n, row in enumerate(ex.map(one, todo), 1):
            if row:
                db.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?)", row)
            if n % 200 == 0:
                db.commit()
                log(f"\r  receipts: {n:,}/{len(todo):,}   ", end="")
    db.commit()
    log("")


def arb_contracts(arb_dbs):
    """Bot contracts of the atomic arbs (arbs.bot = tx.to): all 3 days, and day 2 alone (the 248)."""
    bots, day2 = set(), set()
    for p in arb_dbs:
        b = {x for (x,) in sqlite3.connect(p).execute("SELECT DISTINCT bot FROM arbs")}
        bots |= b
        if p.name == "arb_history.db":
            day2 = b
    return bots, day2


# ---------------------------------------------------------------------------------------------------------------------

def measure(db, arb_dbs, rpc, receipt_rpc, threads=4, log=print, fetch=True):
    lo, hi = window(arb_dbs)
    ref_ts = int(rpc.call("eth_getBlockByNumber", [hex(lo), False])["timestamp"], 16)
    days = (hi - lo + 1) * 2 / 86400
    log(f"cex_markout: blocks {lo:,}..{hi:,} ({days:.2f} days)")
    meta = resolve_missing_pools(db, arb_dbs, rpc, log) if fetch else (
        {k: v for p in arb_dbs for k, v in ah.load_pool_meta(sqlite3.connect(p)).items()} | ah.load_pool_meta(db))
    targets = {p for p, m in meta.items() if pair_of(m)}
    log(f"  target pools (USDC vs WETH/ETH or cbBTC): {len(targets)}")
    if fetch:
        fetch_senders(db, rpc, [p for p in targets if len(p) == 42], lo, hi, threads, log)   # V4 ids keep their sender
        for product, _ in PAIRS.values():
            fetch_cex(db, product, ref_ts - 2 * 60, ref_ts + (hi - lo) * 2 + 120, log=log)
    series = {product: load_series(db, product) for product, _ in PAIRS.values()}
    senders = dict(((b, i), s) for b, i, s in db.execute("SELECT block, logi, sender FROM senders"))
    rows, by_sender, no_sender = [], defaultdict(list), 0
    for p in arb_dbs:
        for tx, sw in single_pool_swaps(sqlite3.connect(p)).items():
            if sw[0][4] not in targets:
                continue
            s = sw[0][5] if sw[0][8] == "v4" else senders.get((sw[0][0], sw[0][2]))
            if not s:
                no_sender += 1
                continue
            m = tx_markouts(sw, meta, series, (lo, ref_ts))
            if m:
                rows.append((s, m))
                by_sender[s].append(tx)
    log(f"  single-pool txs on target pools: {len(rows):,} with markouts ({no_sender} without a sender)")
    pre = sender_summary(rows, days)                     # pick who needs receipts: consistent before the public check
    need = {r["sender"]: by_sender[r["sender"]] for r in pre["senders"] if r["kind"] == "cexdex"}
    need |= {r["sender"]: by_sender[r["sender"]] for r in pre["senders"][:50]}
    if fetch:
        sample_receipts(db, receipt_rpc, need, log=log)
    eth = sum(series["ETH-USD"][1][-100:]) / 100
    gas, eoas = {}, {}
    for s, n, g, d in db.execute("SELECT sender, COUNT(*), AVG(gas_eth), COUNT(DISTINCT eoa) FROM receipts "
                                 "GROUP BY sender"):
        gas[s], eoas[s] = g * eth, (d, n)
    bots, day2 = arb_contracts(arb_dbs)
    s = sender_summary(rows, days, gas, eoas, bots)
    s["overlap_with_day2_248"] = sum(r["sender"] in day2 for r in s["senders"] if r["kind"] == "cexdex")
    s |= {"days": days, "blocks": [lo, hi], "decision": decide(s)}
    return s


def print_report(s, n=20):
    a = s["arb"]
    print(f"\n  CEX-DEX on Base WETH/USDC + cbBTC/USDC, {s['days']:.2f} days, {s['all_single_pool_txs']:,} single-pool txs")
    print(f"  consistent-positive senders (>= {MIN_TXS} swaps, positive at +2/+12/+60 s, >= {MIN_WIN_RATE:.0%} won at "
          f"+12 s, not public): {s['distinct_cexdex']}")
    print(f"    markout/day: at execution ${a['markout_0s_usd_day']:,.0f}, +2 s ${a['markout_2s_usd_day']:,.0f}, "
          f"+12 s ${a['markout_12s_usd_day']:,.0f}, +60 s ${a['markout_60s_usd_day']:,.0f}")
    print(f"    gas ${a['gas_usd_day']:,.0f}/day -> net at +12 s ${a['net_12s_usd_day']:,.0f}/day "
          f"(after a {HEDGE_BPS} bp hedge cost ${a['net_12s_after_hedge_usd_day']:,.0f}/day)")
    if a["breakeven_hedge_bps"] is not None:
        print(f"    notional ${a['notional_usd_day'] / 1e6:,.0f}M/day: the edge after gas is {a['breakeven_hedge_bps']:.2f} bp, "
              f"what a hedge on Coinbase (fees + slippage) may cost before it's a loss")
    t3 = s["top3_share"]
    print(f"    top 3 senders take {t3:.0%}" if t3 is not None else "    top 3: n/a")
    print(f"    also atomic-arb contracts: {s['overlap_with_arb_contracts']} of them "
          f"({s.get('overlap_with_day2_248', 0)} in the day-2 248), net ${s['overlap_net_12s_usd_day']:,.0f}/day")
    print(f"  decision: {s['decision']}")
    print(f"\n  {'sender':<44}{'kind':>8}{'txs':>8}{'win12':>7}{'+2s/d':>9}{'+12s/d':>9}{'+60s/d':>9}{'gas/d':>8}"
          f"{'net12/d':>9}{'eoas':>6} arb")
    for r in s["senders"][:n]:
        print(f"  {r['sender']:<44}{r['kind']:>8}{r['txs']:>8,}{r['win_rate_12s']:>7.0%}{r['markout_2s_usd_day']:>9,.0f}"
              f"{r['markout_12s_usd_day']:>9,.0f}{r['markout_60s_usd_day']:>9,.0f}{r['gas_usd_day']:>8,.0f}"
              f"{r['net_12s_usd_day']:>9,.0f}{r['eoas_in_sample']:>3}/{r['receipts_sampled']:<3}"
              f"{' yes' if r['arb_contract'] else ''}")


def main():
    ap = argparse.ArgumentParser(description="CEX-DEX markout on Base WETH/USDC and cbBTC/USDC (read-only)")
    ap.add_argument("--rpc", default="https://base.drpc.org", help="logs + calls (eth_getLogs, 100-block ranges)")
    ap.add_argument("--receipt-rpc", default="https://gateway.tenderly.co/public/base,https://mainnet.base.org")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--report", action="store_true", help="no fetching: report from stored data")
    ap.add_argument("--json", default=str(ROOT / "logs" / "cex_markout_summary.json"))
    args = ap.parse_args()
    rpc = ah.Rpc(args.rpc, tries=20)
    urls = [u for u in args.receipt_rpc.split(",") if u]
    s = measure(open_db(args.db), ARB_DBS, rpc, ah.Rpc(urls[0], extra=urls[1:], tries=20), args.threads,
                fetch=not args.report)
    print_report(s)
    Path(args.json).write_text(json.dumps(s, indent=1, default=str))


if __name__ == "__main__":
    main()
