"""Record which transactions land in which Base Flashblock (read-only).

Base builds each 2s block as ~10 flashblocks (~200ms apart) and streams each one before the block closes. On-chain
history keeps no flashblock boundaries, so this records them live: for every flashblock, the block's tx index range it
added and when it reached us. `arb_history.py` arbs from the same window can then be placed in their flashblock, next
to the swap that opened their gap: same flashblock = won by being queued already (spam / priority), a later flashblock
= won by reacting to the stream, which a new bot could do too.

    python flashblock_recorder.py --hours 6            # logs/flashblocks.db
    python flashblock_recorder.py --report
    python flashblock_recorder.py --timing logs/arb_history_fb.db   # arb_history run over the recorded window
"""
import argparse
import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import brotli
import websockets

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "logs" / "flashblocks.db"
WS_URL = "wss://mainnet.flashblocks.base.org/ws"
FOCUS = ("XDP", "STONKEX", "CTR", "fBOMB", "SYND", "DRV")    # tokens whose top 3 bots took < 50% on some day


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS fb (block INTEGER, idx INTEGER, start_txi INTEGER, n INTEGER, "
               "t_recv REAL, PRIMARY KEY (block, idx))")
    db.execute("CREATE TABLE IF NOT EXISTS gaps (block INTEGER PRIMARY KEY, reason TEXT)")
    return db


class Recorder:
    """Tracks the running tx count per block. A block whose flashblocks didn't arrive 0, 1, 2, ... in order (missed
    message, reconnect) is marked in `gaps` and its tx ranges are never trusted."""

    def __init__(self, db):
        self.db, self.block, self.next_idx, self.txi = db, None, 0, 0

    def add(self, msg, t_recv):
        block = int(msg["metadata"]["block_number"])
        idx, n = int(msg["index"]), len(msg["diff"]["transactions"])
        if block != self.block:
            if self.block is not None and block != self.block + 1:
                self.db.execute("INSERT OR REPLACE INTO gaps VALUES (?, 'blocks skipped')", (self.block + 1,))
            self.block, self.next_idx, self.txi = block, 0, 0
            if idx != 0:                       # joined (or rejoined) mid-block
                self.db.execute("INSERT OR REPLACE INTO gaps VALUES (?, 'started mid-block')", (block,))
        if idx != self.next_idx:
            self.db.execute("INSERT OR REPLACE INTO gaps VALUES (?, ?)", (block, f"index {idx} after {self.next_idx - 1}"))
        self.db.execute("INSERT OR REPLACE INTO fb VALUES (?, ?, ?, ?, ?)", (block, idx, self.txi, n, t_recv))
        self.next_idx, self.txi = idx + 1, self.txi + n


async def record(db, seconds, url=WS_URL):
    rec, t_end, last_commit = Recorder(db), time.time() + seconds, time.time()
    while time.time() < t_end:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=20, ping_interval=20) as ws:
                while time.time() < t_end:
                    raw = await asyncio.wait_for(ws.recv(), 30)
                    t = time.time()
                    rec.add(json.loads(brotli.decompress(raw) if isinstance(raw, bytes) else raw), t)
                    if t - last_commit > 5:
                        db.commit()
                        last_commit = t
                        print(f"\r  block {rec.block} fb {rec.next_idx - 1}  ({(t_end - t) / 60:.0f} min left)   ",
                              end="", flush=True)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            print(f"\n  stream error ({type(e).__name__}); reconnecting")
            if rec.block is not None:
                db.execute("INSERT OR REPLACE INTO gaps VALUES (?, 'reconnect')", (rec.block,))
            rec.block = None
            await asyncio.sleep(2)
    db.commit()


def flashblock_of(db, block, txi):
    """Flashblock index holding tx `txi` of `block`, or None if not recorded / block untrusted."""
    if db.execute("SELECT 1 FROM gaps WHERE block=?", (block,)).fetchone():
        return None
    row = db.execute("SELECT idx FROM fb WHERE block=? AND start_txi<=? AND ? < start_txi + n",
                     (block, txi, txi)).fetchone()
    return row[0] if row else None


def report(db):
    blocks = db.execute("SELECT MIN(block), MAX(block), COUNT(DISTINCT block) FROM fb").fetchone()
    bad = db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
    print(f"blocks {blocks[0]}..{blocks[1]}: {blocks[2]} recorded, {bad} untrusted (gaps)")
    for idx, n, txs in db.execute("SELECT idx, COUNT(*), AVG(n) FROM fb GROUP BY idx ORDER BY idx"):
        print(f"  flashblock {idx:>2}: {n:>6} seen, {txs:5.1f} txs avg")


def timing(fb_db, arb_db):
    """Arbs (from an arb_history DB covering the recorded window) by flashblock distance from the swap that opened their
    gap (the last earlier tx in the block on one of the arb's pools). -> {"rows": [...], "by_delta": {delta: [n, net]},
    "unplaced": n}. delta 0 = same flashblock, k = k flashblocks later, "block start" = no earlier swap in the block."""
    rows, by_delta, unplaced = [], {}, 0
    for tx, block, txi, gap, net, bot, tokens in arb_db.execute(
            "SELECT tx, block, txi, trigger_gap, net_usd, bot, tokens FROM arbs"):
        fa = flashblock_of(fb_db, block, txi)
        ft = flashblock_of(fb_db, block, txi - gap) if gap else None
        if fa is None or (gap and ft is None):
            unplaced += 1
            continue
        delta = "block start" if not gap else fa - ft
        rows.append({"tx": tx, "block": block, "fb": fa, "trigger_fb": ft, "delta": delta, "net_usd": net, "bot": bot,
                     "tokens": json.loads(tokens) if tokens else []})
        d = by_delta.setdefault(delta, [0, 0.0])
        d[0] += 1
        d[1] += net
    return {"rows": rows, "by_delta": by_delta, "unplaced": unplaced}


def bucket(delta):
    return "block start" if isinstance(delta, str) else "same fb" if delta == 0 else "+1 fb" if delta == 1 else "+2+ fb"


BUCKETS = ("same fb", "+1 fb", "+2+ fb", "block start")


def by_token(rows):
    """{token: {bucket: [arbs, net_usd]}} (an arb counts once for each token it went through). Pure."""
    out = defaultdict(lambda: {b: [0, 0.0] for b in BUCKETS})
    for r in rows:
        for t in r["tokens"]:
            c = out[t][bucket(r["delta"])]
            c[0] += 1
            c[1] += r["net_usd"]
    return out


def arrival_gaps(db):
    """Seconds between consecutive flashblocks of the same block as they reached us (trusted blocks only)."""
    rows = db.execute("SELECT block, idx, t_recv FROM fb WHERE block NOT IN (SELECT block FROM gaps) "
                      "ORDER BY block, idx").fetchall()
    return [t2 - t1 for (b1, i1, t1), (b2, i2, t2) in zip(rows, rows[1:]) if b1 == b2 and i2 == i1 + 1 and i1 >= 1]


def print_timing(t, fb_db=None, symbols=None, focus=()):
    total = sum(v[1] for v in t["by_delta"].values()) or 1
    print(f"arbs placed in a flashblock: {len(t['rows'])} ({t['unplaced']} not placed: block not recorded or untrusted)")
    print("  flashblocks after the trigger swap      arbs      net    share")
    for k in sorted(t["by_delta"], key=lambda k: (isinstance(k, str), k)):
        n, net = t["by_delta"][k]
        label = "gap open at block start" if isinstance(k, str) else (
            "same flashblock (pre-queued)" if k == 0 else f"{k} later (reacted)")
        print(f"  {label:<38}{n:>6}  ${net:>8,.0f}  {net / total:>6.0%}")
    if fb_db is not None:
        g = sorted(arrival_gaps(fb_db))
        if g:
            print(f"  flashblock spacing as received: median {g[len(g) // 2] * 1000:.0f} ms, "
                  f"p10 {g[len(g) // 10] * 1000:.0f} ms, p90 {g[len(g) * 9 // 10] * 1000:.0f} ms")
    symbols = symbols or {}
    toks = by_token(t["rows"])
    rank = sorted(toks, key=lambda k: -sum(v[1] for v in toks[k].values()))
    want = [k for k in rank if symbols.get(k) in focus] + [k for k in rank[:15] if symbols.get(k) not in focus]
    print(f"\n  {'token':<14}" + "".join(f"{b:>20}" for b in BUCKETS) + "   (arbs / net $)")
    for k in want:
        print(f"  {symbols.get(k, k[:12]):<14}" + "".join(f"{toks[k][b][0]:>8} / ${toks[k][b][1]:>8,.0f}" for b in BUCKETS))


def main():
    ap = argparse.ArgumentParser(description="Record Base flashblock tx ranges (read-only)")
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--timing", help="arb_history DB covering the recorded window: place its arbs in flashblocks")
    args = ap.parse_args()
    db = open_db(args.db)
    if args.timing:
        arb_db = sqlite3.connect(args.timing)
        syms = dict(arb_db.execute("SELECT token, symbol FROM symbols"))
        print_timing(timing(db, arb_db), db, syms, focus=FOCUS)
        return
    if not args.report:
        print(f"flashblock_recorder: {WS_URL} for {args.hours} h -> {args.db}")
        asyncio.run(record(db, args.hours * 3600))
        print()
    report(db)


if __name__ == "__main__":
    main()
