"""Multi-token pipeline: feeds a list of Base tokens through scanner.py's safety gate and route calculator.

Each cycle:
  1. Skip tokens already in blacklist.json (proven malicious: honeypot, tax, upgradeable proxy, owner powers).
  2. Hand the rest to worker processes. Every worker owns ONE anvil fork, so fork snapshots never interleave
     (two safety checks on one fork would revert each other's state). Workers pull tokens from a shared queue.
  3. Per token: scanner.evaluate_token() -> safety verdict -> every route. Verbose output goes to a per-token log file.
  4. The coordinator (only writer of blacklist.json and results.jsonl) records each result. Unprofitable tokens are
     logged silently. Any route with net profit after all costs > --min-profit is re-run as real swaps on the fork and
     shown in a loud banner.

Nothing is ever sent to a real network: workers only talk to their own local anvil fork.
"""
import argparse
import json
import multiprocessing as mp
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from web3 import Web3

import scanner
from scanner import MALICIOUS_CODES, USDC, WETH, ScanSettings, cs, erc20, send, swap_on_fork

ROOT = Path(__file__).resolve().parent
BLACKLIST_PATH = ROOT / "blacklist.json"
LOG_DIR = ROOT / "logs"
FORK_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_PORT = 8600                 # worker i runs its anvil on BASE_PORT + i
# Fork politely: free RPCs reset connections when anvil bursts parallel state fetches. Throttle + retry with backoff.
ANVIL_FORK_FLAGS = ["--compute-units-per-second", "150", "--retries", "10", "--fork-retry-backoff", "1000",
                    "--timeout", "45000"]
TOKEN_TIMEOUT = 900              # max seconds for one token before the coordinator gives up on it
POLL_SECONDS = 2                 # how often the coordinator checks on workers while waiting
FORK_MARKER = Web3.to_checksum_address("0x00000000000000000000000000000000000f0c4d")   # per-worker sentinel balance: proves "this is my fork"

# Core monitoring baseline: actively traded Base tokens. Every address was checked on-chain (code, symbol, a live
# pool vs USDC/WETH on a venue we scan). Copycat tokens with the same symbol are common: never edit by symbol alone.
SEED_TOKENS = [
    # (symbol, address, category). Verified on-chain 2026-10-16: code, symbol(), decimals(), live pool vs USDC/WETH.
    # Ranked by recent Base DEX volume + on-chain swap count. Copycats reuse these symbols: match by ADDRESS only.
    ("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", "major"),  # Uses 8 decimals
    ("AERO", "0x940181a94A35A4569E4529A3CDfB74e38FD98631", "defi"),
    ("VIRTUAL", "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "ecosystem"),  # Much of its liquidity sits in pools
    ("VVV", "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf", "ecosystem"),
    ("cbETH", "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "major"),  # LST that tracks ETH closely, so spreads are
    ("wstETH", "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "major"),  # LST that tracks ETH closely; margins are
    ("DRV", "0x9d0E8f5b25384C7310CB8C6aE32C8fbeb645d083", "defi"),  # USDC pools are nearly empty on the factories
    ("MORPHO", "0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842", "defi"),  # Proxy contract, so its code can be upgraded
    ("ZRO", "0x6985884C4392D348587B19cb9eAAf157F13271cd", "major"),  # Shallow liquidity on the target venues
    ("ZEN", "0xf43eB8De897Fbc7F2502483B2Bef7Bb9EA179229", "ecosystem"),  # Liquidity is almost all in one Slipstream
    ("AAVE", "0x63706e401c06ac8513145b7687A14804d17f814b", "defi"),  # Bridged token; no one checked the issuer
    ("ZORA", "0x1111111111166b7FE7bd91427724B487980aFc69", "ecosystem"),  # Most of its liquidity is on UniV4, which
    ("FLOCK", "0x5aB3D4c385B400F3aBB49e80DE2fAF6a88A7B691", "ecosystem"),  # Not in the market sweep, and no check
    ("CLANKER", "0x1bc0c42215582d5A085795f4baDbaC3ff36d1Bcb", "ecosystem"),  # Effectively WETH-only; its USDC pools
    ("BNKR", "0x22aF33FE49fD1Fa80c7149773dDe5890D3c76F3b", "ecosystem"),  # Effectively WETH-only
    ("BRETT", "0x532f27101965dd16442E59d40670FaF5eBB142E4", "meme"),  # Deep WETH liquidity but few swaps
    ("DEGEN", "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "meme"),  # Low activity right now
    ("TOSHI", "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4", "meme"),  # Copycat risk with the same symbol; this is
    ("KEYCAT", "0x9a26F5433671751C3276a065f57e5a02D2817973", "meme"),  # Canonical address, but nearly dead on the
    ("cbXRP", "0xcb585250f852C6c6bf90434AB21A00f02833a4af", "major"),  # Uses 6 decimals
    ("LINK", "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196", "major"),  # Bridged token; issuer not verified off-chain
    ("doginme", "0x6921B130D297cc43754afba22e5EAc0FBf8Db75b", "meme"),  # Symbol is lowercase
    ("weETH", "0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A", "major"),  # LST
    ("KTA", "0xc0634090F2Fe6c6d75e61Be2b949464aBB498973", "ecosystem"),  # Not in the market sweep, and no check
    ("TIG", "0x0C03Ce270B4826Ec62e7DD007f0B716068639F7B", "ecosystem"),  # Not in the market sweep, and no check
    ("WELL", "0xA88594D404727625A9437C3f886C7643872296AE", "defi"),  # Low volume; liquidity is essentially in one
    ("SKI", "0x768BE13e1680b5ebE0024C42c896E3dB59ec0149", "meme"),  # Uses 9 decimals
    ("MIGGLES", "0xB1a03EdA10342529bBF8EB700a06C60441fEf25d", "meme"),  # Low volume and thin on the target venues
    ("HIGHER", "0x0578d8A44db98B23BF096A382e016e29a5Ce0ffe", "meme"),  # Canonical address but effectively dead; no
    ("MOCHI", "0xF6e932Ca12afa26665dC4dDE7e27be02A7c02e50", "meme"),  # Canonical address but nearly dead (2 swaps in
]


# ---------------------------------------------------------------- blacklist

class Blacklist:
    """Addresses proven malicious, persisted as JSON. Only the coordinator process writes it."""

    def __init__(self, path=BLACKLIST_PATH):
        self.path = Path(path)
        self.entries = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.entries = {cs(k): v for k, v in data.items()}
            except (ValueError, AttributeError):
                # Corrupt file: keep a copy for inspection and start clean rather than crash the pipeline.
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
                print(f"warning: {self.path.name} was unreadable, moved to {backup.name}", file=sys.stderr)

    def __contains__(self, address):
        return cs(address) in self.entries

    def add(self, summary):
        self.entries[cs(summary["token"])] = {
            "symbol": summary.get("symbol"), "verdict": summary["status"], "reason_code": summary["reason_code"],
            "reason": summary.get("reason"), "block": summary.get("block"), "checked_at": now_iso()}
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True))
        os.replace(tmp, self.path)   # atomic: a crash mid-write never leaves a half-written blacklist


def should_blacklist(summary):
    """Only verdicts about the token itself are cached. Thin liquidity, RPC errors and test-setup failures are
    conditions of the moment and get re-checked next cycle."""
    return summary["status"] in ("UNSAFE", "DYNAMIC_UNSAFE") and summary.get("reason_code") in MALICIOUS_CODES


# ---------------------------------------------------------------- forks

def port_is_free(port):
    """Nothing listening on 127.0.0.1:port and the port can be bound. SO_REUSEADDR ignores sockets that were just
    closed (TIME_WAIT), which would otherwise make a port look busy for a while after its anvil has exited."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False                      # something is accepting connections there
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_free_ports(start, count, limit=200):
    """`count` ports from `start` upward that nothing is listening on (e.g. a leftover or manual anvil)."""
    ports = [p for p in range(start, start + limit) if port_is_free(p)][:count]
    if len(ports) < count:
        raise RuntimeError(f"not enough free ports in {start}-{start + limit}")
    return ports


def start_anvil(port, fork_url=FORK_URL, timeout=60, attempts=2):
    """Start OUR OWN anvil fork on `port`, retrying once: a busy upstream RPC can make anvil exit during startup."""
    for attempt in range(attempts):
        try:
            return _start_anvil_once(port, fork_url, timeout)
        except RuntimeError as e:
            if "already in use" in str(e) or attempt == attempts - 1:
                raise
            time.sleep(3)


def _start_anvil_once(port, fork_url=FORK_URL, timeout=60):
    """Start OUR OWN anvil fork on `port`. Refuses a busy port: anything already answering there is someone else's
    fork, and sharing it would interleave snapshots. Kills the child on any failure or interruption."""
    if not port_is_free(port):
        raise RuntimeError(f"port {port} is already in use; refusing to share another process's fork")
    anvil = os.getenv("ANVIL_BIN") or str(Path.home() / ".foundry" / "bin" / "anvil")
    proc = subprocess.Popen([anvil, "--fork-url", fork_url, "--port", str(port), "--silent", *ANVIL_FORK_FLAGS],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}", request_kwargs={"timeout": 60}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"anvil on port {port} exited during startup")
            try:
                if w3.eth.chain_id == 8453:
                    time.sleep(0.5)
                    if proc.poll() is not None:   # someone else answered and ours died on bind
                        raise RuntimeError(f"anvil on port {port} exited; another process owns the port")
                    return proc
            except RuntimeError:
                raise
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"anvil on port {port} did not start within {timeout}s")
    except BaseException:   # includes SystemExit from SIGTERM: never leave an orphaned fork holding the port
        stop_process(proc)
        raise


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def fund_on_fork(w3, asset, amount, who):
    """Give `who` exactly `amount` of WETH (wrap ETH) or USDC (mint via Circle's masterMinter, impersonated).
    Fork only. Minting instead of swapping keeps every pool price untouched."""
    w3.provider.make_request("anvil_setBalance", [who, hex(10**22)])
    if asset == WETH:
        send(w3, erc20(w3, WETH).functions.deposit(), who, value=amount)
        return
    usdc = w3.eth.contract(address=USDC, abi=[
        scanner.fn("masterMinter", [], ["address"]),
        scanner.fn("configureMinter", ["address", "uint256"], ["bool"], "nonpayable"),
        scanner.fn("mint", ["address", "uint256"], ["bool"], "nonpayable")])
    master = usdc.functions.masterMinter().call()
    w3.provider.make_request("anvil_impersonateAccount", [master])
    w3.provider.make_request("anvil_setBalance", [master, hex(10**20)])
    send(w3, usdc.functions.configureMinter(who, amount), master)
    w3.provider.make_request("anvil_stopImpersonatingAccount", [master])
    send(w3, usdc.functions.mint(who, amount), who)


def confirm_route_on_fork(w3, route, gas_usd, eth_usd):
    """Re-run a profitable route as real swaps (one per leg) on the fork and measure what actually comes back.
    Rolled back afterwards. Legs land in separate blocks, so this is a sanity check, not an atomic replay."""
    who = w3.eth.accounts[6]
    start_asset, start_units = route["start_asset"], route["start_units"]
    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        fund_on_fork(w3, start_asset, start_units, who)
        amount = start_units
        for pool, t_in, t_out in route["legs"]:
            before = erc20(w3, t_out).functions.balanceOf(who).call()
            swap_on_fork(w3, pool, t_in, t_out, amount, who)
            amount = erc20(w3, t_out).functions.balanceOf(who).call() - before
        sym, dec = scanner.BASES[start_asset]
        price = scanner.usd_price(start_asset, eth_usd)
        realized = (amount - start_units) / 10**dec * price - gas_usd
        return {"confirmed": True, "realized_net_usd": realized, "end_units": amount}
    except Exception as e:
        return {"confirmed": False, "error": str(e)[:200]}
    finally:
        w3.provider.make_request("evm_revert", [snapshot])


# ---------------------------------------------------------------- worker

def summarize(report, settings):
    """JSON-safe digest of scanner.evaluate_token()'s report."""
    ctx = report["ctx"]
    routes = report["routes"]
    best = routes[0] if routes else None
    return {
        "token": report["token"], "symbol": report["symbol"], "block": report["block"],
        "status": report["status"], "reason": ctx.reason, "reason_code": ctx.reason_code,
        "proxy_warning": ctx.proxy_warning or None,
        "pools_found": report["pools_found"], "pools_qualified": report["pools_qualified"],
        "taxes": {"buy": ctx.buy_tax, "transfer": ctx.transfer_tax, "sell": ctx.sell_tax},
        "warnings": list(ctx.warnings), "routes_checked": len(routes) + report["failed_routes"],
        "pools": [asdict(p) for p in ctx.pools],     # qualifying pools, so gap_monitor.py can load them directly
        "best": None if best is None else {k: best[k] for k in ("route", "path", "hops", "net_profit", "gas_usdc")},
        "hits": [],
    }


def evaluate_one(w3, rpc_url, address, settings):
    """One token, verbose output to stdout (the caller redirects it). Never raises."""
    t0 = time.time()
    try:
        report = scanner.evaluate_token(w3, address, settings, rpc_url)
        summary = summarize(report, settings)
        for r in report["routes"]:
            if r["net_profit"] <= settings.min_profit:
                break   # routes are sorted best-first
            hit = {k: r[k] for k in ("route", "path", "hops", "start", "net_profit", "gas_usdc", "amount_in")}
            hit["min_profit"] = settings.min_profit
            hit["pools"] = [p.address for p, _, _ in r["legs"]]
            hit["fork_check"] = confirm_route_on_fork(w3, r, r["gas_usdc"], report["eth_usd"])
            summary["hits"].append(hit)
            print(f"HIT {hit['path']} quoted ${hit['net_profit']:+.4f}, fork check {hit['fork_check']}")
    except Exception as e:
        summary = {"token": address, "symbol": None, "status": "ERROR", "reason_code": "ERROR",
                   "reason": f"{type(e).__name__}: {str(e)[:300]}", "hits": []}
        print(f"ERROR: {summary['reason']}")
    summary["seconds"] = round(time.time() - t0, 1)
    return summary


def _raise_exit(signum, frame):
    raise SystemExit(0)


def safe_name(symbol):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(symbol))[:32] or "token"


def worker_main(worker_id, port, rpc_url, fork_url, tasks, results, settings, log_dir):
    """Worker process: owns one fork for its whole life, evaluates tokens one at a time from the shared queue.

    Messages to the coordinator: {"ready"}, {"started": address} before each token, the token's summary after it,
    and {"worker_failed"} if the worker itself breaks. With its own fork, the worker resets it to the latest block
    before each token and checks a per-worker sentinel balance, so it can never be quietly running on a foreign fork.
    """
    signal.signal(signal.SIGTERM, _raise_exit)   # so `finally` stops our anvil when the coordinator terminates us
    anvil, current = None, None
    own_fork = rpc_url is None
    marker = hex(10**18 + worker_id)
    try:
        if own_fork:
            anvil = start_anvil(port, fork_url)
            rpc_url = f"http://127.0.0.1:{port}"
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
        results.put({"ready": worker_id, "rpc": rpc_url})
        while True:
            item = tasks.get()
            if item is None:
                break
            symbol, address = item
            current = address
            results.put({"started": address, "worker": worker_id})
            if own_fork:
                # Fresh state for every token (latest block), then stamp and verify our sentinel.
                w3.provider.make_request("anvil_reset", [{"forking": {"jsonRpcUrl": fork_url}}])
                w3.provider.make_request("anvil_setBalance", [FORK_MARKER, marker])
            log_path = Path(log_dir) / f"{safe_name(symbol)}-{address[:10]}.log"
            with open(log_path, "w") as log, redirect_stdout(log):
                print(f"worker {worker_id} | {rpc_url} | {symbol} {address}")
                summary = evaluate_one(w3, rpc_url, address, settings)
            if own_fork and hex(w3.eth.get_balance(FORK_MARKER)) != marker:
                raise RuntimeError("fork sentinel changed: another process is using this worker's fork")
            summary.update({"worker": worker_id, "rpc": rpc_url, "log": str(log_path), "fork_marker_ok": own_fork,
                            "symbol": summary.get("symbol") or symbol})
            results.put(summary)
            current = None
    except BaseException as e:
        results.put({"worker_failed": worker_id, "token": current, "error": f"{type(e).__name__}: {e}"})
    finally:
        stop_process(anvil)


# ---------------------------------------------------------------- coordinator output

def _style(text, code, out):
    color = getattr(out, "isatty", lambda: False)() and not os.getenv("NO_COLOR")
    return f"\033[{code}m{text}\033[0m" if color else text


def render_hit(summary, hit):
    check = hit["fork_check"]
    if check.get("confirmed"):
        verdict = f"fork re-run: ${check['realized_net_usd']:+.4f} realized"
    else:
        verdict = f"fork re-run FAILED: {check.get('error')}"
    lines = [
        "!" * 78,
        f"!!  PROFIT ROUTE  {summary['symbol']}  net ${hit['net_profit']:+.4f} after all costs on ${hit['amount_in']:,.0f}",
        f"!!  path   {hit['path']}   ({hit['hops']} swaps, gas ${hit['gas_usdc']:.4f})",
        f"!!  route  {hit['route']}",
        f"!!  pools  {' -> '.join(hit['pools'])}",
        f"!!  token  {summary['token']}   block {summary['block']}",
        f"!!  {verdict}",
        "!" * 78,
    ]
    return "\n".join(lines)


def handle_result(summary, blacklist, out, results_log=None):
    """Coordinator-side bookkeeping for one finished token. Silent unless there is a profitable route."""
    summary["finished_at"] = now_iso()
    if should_blacklist(summary):
        blacklist.add(summary)
        summary["blacklisted"] = True
    if results_log is not None:
        results_log.write(json.dumps(summary, default=str) + "\n")
        results_log.flush()
    for hit in summary.get("hits", []):
        if is_confirmed_hit(hit):
            out.write("\a" + _style(render_hit(summary, hit), "1;97;41", out) + "\n")
            out.flush()


def is_confirmed_hit(hit, min_profit=None):
    """Loud only when the route made money as real swaps on the fork, not just on paper."""
    check = hit.get("fork_check") or {}
    floor = hit.get("min_profit", 1.0) if min_profit is None else min_profit
    return bool(check.get("confirmed")) and check.get("realized_net_usd", float("-inf")) > floor


def ticker(out, cycle, done, total, stats):
    if getattr(out, "isatty", lambda: False)():
        out.write(f"\r[cycle {cycle}] {done}/{total} tokens | safe {stats['SAFE'] + stats['PROXY_WARNING']} | "
                  f"thin {stats['THIN']} | "
                  f"unsafe {stats['UNSAFE'] + stats['DYNAMIC_UNSAFE']} | errors {stats['ERROR']} | "
                  f"hits {stats['hits']}   ")
        out.flush()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- coordinator

def run_cycle(tokens, settings, workers=1, blacklist=None, rpc_url=None, fork_url=FORK_URL, recheck=False,
              base_port=BASE_PORT, log_root=LOG_DIR, out=sys.stdout, cycle=1):
    """Evaluate every token once. Returns the list of per-token summaries (blacklisted skips included).

    rpc_url: use an existing local fork instead of starting one per worker. A shared fork can only serve ONE worker,
    because concurrent snapshot/revert on the same fork would corrupt each other's checks.
    """
    if rpc_url is not None and workers != 1:
        raise ValueError("an existing fork (rpc_url) can only serve one worker; use workers=1 or own forks")
    if rpc_url is not None and not scanner.is_local_rpc(rpc_url):
        raise ValueError("the pipeline only runs against a local fork")
    blacklist = blacklist if blacklist is not None else Blacklist()

    seen, queue_items, summaries = set(), [], []
    for symbol, raw in tokens:
        try:
            address = cs(str(raw).strip())
        except Exception:
            summaries.append({"token": str(raw), "symbol": symbol, "status": "ERROR", "reason_code": "ERROR",
                              "reason": "not a valid address", "hits": []})
            continue
        if address in seen or address == USDC:
            continue
        seen.add(address)
        if address in blacklist and not recheck:
            summaries.append({"token": address, "symbol": symbol, "status": "BLACKLISTED",
                              "reason_code": blacklist.entries[address]["reason_code"], "hits": []})
            continue
        queue_items.append((symbol, address))

    log_dir = Path(log_root) / datetime.now().strftime("%Y%m%d-%H%M%S") / f"cycle{cycle}"
    log_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(workers, len(queue_items))) if queue_items else 0
    stats = {k: 0 for k in ("SAFE", "PROXY_WARNING", "THIN", "UNSAFE", "DYNAMIC_UNSAFE", "ERROR", "hits")}
    pending = {a for _, a in queue_items}
    symbols = dict((a, s) for s, a in queue_items)

    def record(summary, results_log):
        pending.discard(cs(summary["token"]))
        handle_result(summary, blacklist, out, results_log)
        summaries.append(summary)
        stats[summary["status"] if summary["status"] in stats else "ERROR"] += 1
        stats["hits"] += sum(is_confirmed_hit(h) for h in summary.get("hits", []))
        ticker(out, cycle, len(queue_items) - len(pending), len(queue_items), stats)

    def lost(address, reason):
        return {"token": address, "symbol": symbols.get(address), "status": "ERROR", "reason_code": "ERROR",
                "reason": reason, "hits": []}

    ctx = mp.get_context("spawn")
    tasks, results = ctx.Queue(), ctx.Queue()
    ports = pick_free_ports(base_port, workers) if (workers and rpc_url is None) else [base_port] * workers
    procs = []
    with open(log_dir.parent / "results.jsonl", "a") as results_log:
        try:
            procs = [ctx.Process(target=worker_main, daemon=True,
                                 args=(i, ports[i], rpc_url, fork_url, tasks, results, settings, str(log_dir)))
                     for i in range(workers)]
            for p in procs:
                p.start()

            # Barrier: hand out tokens only once every worker's fork is up, so the queue is shared evenly.
            ready, failed = set(), set()
            deadline = time.time() + 120
            while len(ready) + len(failed) < workers and time.time() < deadline:
                try:
                    msg = results.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    if not any(p.is_alive() for p in procs):
                        break
                    continue
                if "ready" in msg:
                    ready.add(msg["ready"])
                elif "worker_failed" in msg:
                    failed.add(msg["worker_failed"])
                    print(f"\nworker {msg['worker_failed']} failed to start: {msg['error']}", file=sys.stderr)
            for item in queue_items if ready else []:
                tasks.put(item)
            for _ in range(workers):
                tasks.put(None)

            # Collect. Poll often so a dead worker is noticed at once, not after TOKEN_TIMEOUT.
            in_flight = {}     # worker id -> (address, started_at)
            alive_ids = set(ready)
            while pending and alive_ids:
                try:
                    msg = results.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    msg = None
                if msg is None:
                    for wid in list(alive_ids):
                        if not procs[wid].is_alive():           # died without a word (SIGKILL, OOM)
                            alive_ids.discard(wid)
                            if wid in in_flight:
                                record(lost(in_flight.pop(wid)[0], "worker died mid-token"), results_log)
                    for wid, (addr, t0) in list(in_flight.items()):
                        if time.time() - t0 > TOKEN_TIMEOUT:
                            procs[wid].terminate()
                            alive_ids.discard(wid)
                            in_flight.pop(wid)
                            record(lost(addr, f"timed out after {TOKEN_TIMEOUT}s"), results_log)
                    continue
                if "started" in msg:
                    in_flight[msg["worker"]] = (msg["started"], time.time())
                elif "worker_failed" in msg:
                    wid = msg["worker_failed"]
                    alive_ids.discard(wid)
                    in_flight.pop(wid, None)
                    print(f"\nworker {wid} failed: {msg['error']}", file=sys.stderr)
                    if msg.get("token") and cs(msg["token"]) in pending:
                        record(lost(cs(msg["token"]), f"worker failed: {msg['error'][:200]}"), results_log)
                elif "token" in msg:
                    in_flight.pop(msg.get("worker"), None)
                    record(msg, results_log)
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=15)
        for address in sorted(pending):   # never answered: no worker left to do it
            record(lost(address, "not processed: no worker available"), results_log)

    skipped = sum(s["status"] == "BLACKLISTED" for s in summaries)
    out.write(f"\r[cycle {cycle}] done: {len(queue_items)} scanned, {skipped} skipped (blacklisted) | "
              f"safe {stats['SAFE']} | proxy-warning {stats['PROXY_WARNING']} | thin {stats['THIN']} | "
              f"unsafe {stats['UNSAFE'] + stats['DYNAMIC_UNSAFE']} | "
              f"errors {stats['ERROR']} | confirmed hits {stats['hits']} | logs {log_dir}\n")
    out.flush()
    return summaries


def load_tokens(args):
    if args.tokens:
        return [(a.strip()[:8], a.strip()) for a in args.tokens.split(",") if a.strip()]
    if args.tokens_file:
        data = json.loads(Path(args.tokens_file).read_text())
        return [(t["symbol"], t["address"]) for t in data]
    return [(sym, addr) for sym, addr, _ in SEED_TOKENS]


def main():
    ap = argparse.ArgumentParser(description="Multi-token pipeline over scanner.py (safety gate + route calculator)")
    ap.add_argument("--workers", type=int, default=1, help="parallel workers, each with its own anvil fork")
    ap.add_argument("--loop", action="store_true", help="keep cycling (fresh forks every cycle)")
    ap.add_argument("--interval", type=int, default=300, help="seconds between cycle starts with --loop")
    ap.add_argument("--tokens", help="comma-separated addresses instead of the seed list")
    ap.add_argument("--tokens-file", help='JSON list of {"symbol", "address"} instead of the seed list')
    ap.add_argument("--recheck", action="store_true", help="ignore blacklist.json for this run")
    ap.add_argument("--blacklist", default=str(BLACKLIST_PATH))
    ap.add_argument("--rpc", help="use this existing LOCAL fork instead of starting one (forces 1 worker)")
    ap.add_argument("--fork-url", default=FORK_URL, help="upstream RPC the forks copy from (env BASE_RPC_URL)")
    ap.add_argument("--base-port", type=int, default=BASE_PORT)
    ap.add_argument("--amount", type=float, default=500)
    ap.add_argument("--settle", choices=["native", "usdc"], default="native")
    ap.add_argument("--min-profit", type=float, default=1.0)
    ap.add_argument("--min-liquidity", type=float, default=50_000)
    ap.add_argument("--max-tax", type=float, default=0.5)
    args = ap.parse_args()

    settings = ScanSettings(amount=args.amount, settle=args.settle, min_profit=args.min_profit,
                            min_liquidity=args.min_liquidity, max_tax=args.max_tax)
    tokens = load_tokens(args)
    blacklist = Blacklist(args.blacklist)
    print(f"pipeline: {len(tokens)} tokens, {args.workers} worker(s), {len(blacklist.entries)} blacklisted, "
          f"${args.amount:,.0f} trades, hit threshold ${args.min_profit:.2f}, upstream {args.fork_url}")
    cycle = 1
    try:
        while True:
            started = time.time()
            run_cycle(tokens, settings, args.workers, blacklist, args.rpc, args.fork_url, args.recheck,
                      args.base_port, cycle=cycle)
            if not args.loop:
                break
            cycle += 1
            time.sleep(max(0, args.interval - (time.time() - started)))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
