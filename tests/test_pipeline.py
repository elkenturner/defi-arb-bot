"""pipeline.py: blacklist persistence, silent vs loud output, and the ingestion queue end to end.

The end-to-end tests run the real worker process against the session's test fork (port 8546) with one worker,
then check that every token was processed exactly once, in order, and that the fork was left untouched.
"""
import io
import json
import socket

import pytest

import pipeline
import scanner
from conftest import LOCAL_URL
from scanner import USDC, WETH

SETTINGS = scanner.ScanSettings()          # $500, min profit $1, $50k per-pool floor, 0.5% max tax
EOA = "0x000000000000000000000000000000000000dEaD"   # not a token: must come back as ERROR, never crash
DRV = "0x9d0E8f5b25384C7310CB8C6aE32C8fbeb645d083"   # real upgradeable proxy, not allowlisted


def summary(status, code, token="0x" + "1" * 40, hits=()):
    return {"token": token, "symbol": "T", "status": status, "reason_code": code, "reason": "r", "block": 1,
            "hits": list(hits)}


# ---------------------------------------------------------------- blacklist (no fork needed)

def test_blacklist_persists_atomically(tmp_path):
    path = tmp_path / "bl.json"
    bl = pipeline.Blacklist(path)
    assert bl.entries == {}
    bl.add(summary("UNSAFE", "HONEYPOT"))
    again = pipeline.Blacklist(path)
    assert "0x" + "1" * 40 in again
    assert again.entries[scanner.cs("0x" + "1" * 40)]["reason_code"] == "HONEYPOT"
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_blacklist_is_moved_aside_not_fatal(tmp_path):
    path = tmp_path / "bl.json"
    path.write_text("{not json")
    bl = pipeline.Blacklist(path)
    assert bl.entries == {} and (tmp_path / "bl.corrupt.json").exists()


@pytest.mark.parametrize("status,code,expected", [
    ("UNSAFE", "HONEYPOT", True), ("UNSAFE", "TAX", True), ("DYNAMIC_UNSAFE", "PROXY", True),
    ("DYNAMIC_UNSAFE", "OWNER_POWER", True),
    ("DYNAMIC_UNSAFE", "PROXY_PIN_MISMATCH", False),   # allowlisted token whose pin broke: re-check every cycle
    ("PROXY_WARNING", "OK", False),
    ("UNSAFE", "LIQUIDITY", False),   # market condition: re-check next cycle
    ("UNSAFE", "INFRA", False),       # our test setup failed, not the token
    ("UNSAFE", "LIVE_RPC", False), ("THIN", "LIQUIDITY", False), ("ERROR", "ERROR", False), ("SAFE", "OK", False),
])
def test_only_token_level_verdicts_are_blacklisted(status, code, expected):
    assert pipeline.should_blacklist(summary(status, code)) is expected


# ---------------------------------------------------------------- output (no fork needed)

def test_unprofitable_results_are_silent(tmp_path):
    out = io.StringIO()
    pipeline.handle_result(summary("SAFE", "OK"), pipeline.Blacklist(tmp_path / "bl.json"), out)
    pipeline.handle_result(summary("THIN", "LIQUIDITY"), pipeline.Blacklist(tmp_path / "bl.json"), out)
    assert out.getvalue() == ""


def test_profitable_route_is_loud(tmp_path):
    hit = {"route": "A -> B", "path": "USDC > T > WETH > USDC", "hops": 3, "net_profit": 12.5, "gas_usdc": 0.006,
           "amount_in": 500, "pools": ["0xA", "0xB", "0xC"],
           "fork_check": {"confirmed": True, "realized_net_usd": 12.1}}
    out = io.StringIO()
    pipeline.handle_result(summary("SAFE", "OK", hits=[hit]), pipeline.Blacklist(tmp_path / "bl.json"), out)
    text = out.getvalue()
    assert "PROFIT ROUTE" in text and "+12.5000" in text and "+12.1000 realized" in text and text.startswith("\a")


def test_unconfirmed_hit_is_silent(tmp_path):
    # Quoted profit that did NOT survive the fork re-run (e.g. a hidden tax) must not trigger the banner.
    hit = {"route": "A -> B", "path": "USDC > T > USDC", "hops": 2, "net_profit": 3.0, "gas_usdc": 0.004,
           "amount_in": 500, "pools": ["0xA", "0xB"], "min_profit": 1.0,
           "fork_check": {"confirmed": True, "realized_net_usd": -0.4}}
    out = io.StringIO()
    pipeline.handle_result(summary("SAFE", "OK", hits=[hit]), pipeline.Blacklist(tmp_path / "bl.json"), out)
    hit["fork_check"] = {"confirmed": False, "error": "execution reverted"}
    pipeline.handle_result(summary("SAFE", "OK", hits=[hit]), pipeline.Blacklist(tmp_path / "bl.json"), out)
    assert out.getvalue() == ""


def test_transport_errors_are_not_honeypot_evidence():
    import requests
    from web3.exceptions import ContractLogicError
    assert scanner.is_onchain_revert(ContractLogicError("execution reverted: TF"))
    assert scanner.is_onchain_revert(RuntimeError("transaction reverted"))
    for transient in (requests.exceptions.ReadTimeout("Read timed out"), ValueError({"code": 429}),
                      ConnectionResetError("reset by peer")):
        assert not scanner.is_onchain_revert(transient)
        with pytest.raises(type(transient)):
            scanner._sim_failure("HONEYPOT: buy reverted", transient)   # propagates -> ERROR, never blacklisted


def test_bad_addresses_in_the_list_dont_crash_the_cycle(tmp_path):
    out = io.StringIO()
    results = pipeline.run_cycle([("X", "not-an-address"), ("Y", " 0x12 ")], SETTINGS,
                                 blacklist=pipeline.Blacklist(tmp_path / "bl.json"), log_root=tmp_path, out=out)
    assert [r["status"] for r in results] == ["ERROR", "ERROR"]


def test_busy_port_is_refused():
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            pipeline.start_anvil(port)
        assert port not in pipeline.pick_free_ports(port, 3)


def test_dead_workers_dont_hang_the_coordinator(tmp_path, monkeypatch):
    # anvil that exits at once -> every worker fails at startup -> cycle ends quickly with every token ERROR.
    fake = tmp_path / "anvil"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ANVIL_BIN", str(fake))
    tokens = [("A", "0x" + "a" * 40), ("B", "0x" + "b" * 40), ("C", "0x" + "c" * 40)]
    t0 = pipeline.time.time()
    results = pipeline.run_cycle(tokens, SETTINGS, workers=2, blacklist=pipeline.Blacklist(tmp_path / "bl.json"),
                                 base_port=8650, log_root=tmp_path, out=io.StringIO())
    assert pipeline.time.time() - t0 < 60
    assert len(results) == 3 and all(r["status"] == "ERROR" for r in results)


def test_shared_fork_refuses_multiple_workers():
    with pytest.raises(ValueError):
        pipeline.run_cycle([], SETTINGS, workers=2, rpc_url=LOCAL_URL)
    with pytest.raises(ValueError):
        pipeline.run_cycle([], SETTINGS, workers=1, rpc_url="https://mainnet.base.org")


# ---------------------------------------------------------------- end to end on the test fork

@pytest.fixture(scope="module")
def cast_of_tokens(make_trap):
    """Four fake tokens with known expected outcomes, plus a non-token address."""
    return {
        "CLEAN2": make_trap("CLEAN2", renounce=True, bases=(USDC, WETH)),            # safe, priced the same: no hit
        "TAXB": make_trap("TAXB", tax_bps=500, renounce=True),                          # UNSAFE TAX -> blacklist
        "THIN1": make_trap("THIN1", renounce=True),                                     # one pool only -> THIN
        # $30k USDC vs 30k tokens ($1) in the USDC pool; $30k WETH vs 27k tokens ($1.11) in the WETH pool.
        "ARB": make_trap("ARB", renounce=True, bases=(USDC, WETH), token_side={WETH: 27_000}),
        "EOA": EOA,
    }


def fork_fingerprint(w3):
    # Accounts the safety sim (0, 1) and the hit re-run (6) use: all must be back to zero afterwards.
    return [scanner.erc20(w3, t).functions.balanceOf(w3.eth.accounts[i]).call()
            for t in (USDC, WETH) for i in (0, 1, 6)]


def test_queue_processes_tokens_sequentially_and_leaves_fork_clean(w3, cast_of_tokens, tmp_path):
    tokens = [(name, addr) for name, addr in cast_of_tokens.items()]
    before = fork_fingerprint(w3)
    out = io.StringIO()
    bl = pipeline.Blacklist(tmp_path / "bl.json")
    results = pipeline.run_cycle(tokens, SETTINGS, workers=1, blacklist=bl, rpc_url=LOCAL_URL,
                                 log_root=tmp_path / "logs", out=out)
    by_token = {scanner.cs(r["token"]): r for r in results}

    # every token answered exactly once, in queue order (single worker = strictly sequential)
    assert [scanner.cs(r["token"]) for r in results] == [scanner.cs(a) for _, a in tokens]
    assert len(by_token) == len(tokens)
    status = {name: by_token[scanner.cs(addr)]["status"] for name, addr in cast_of_tokens.items()}
    assert status == {"CLEAN2": "SAFE", "TAXB": "UNSAFE", "THIN1": "THIN", "ARB": "SAFE", "EOA": "ERROR"}

    # blacklist: only the taxed token
    assert set(bl.entries) == {scanner.cs(cast_of_tokens["TAXB"])}
    assert bl.entries[scanner.cs(cast_of_tokens["TAXB"])]["reason_code"] == "TAX"

    # the mispriced token is a confirmed hit; the clean one is silent
    arb = by_token[scanner.cs(cast_of_tokens["ARB"])]
    assert arb["hits"], "mispriced pools must produce a profitable route"
    hit = arb["hits"][0]
    assert hit["path"] == "USDC > ARB > WETH > USDC" and hit["net_profit"] > 1
    assert hit["fork_check"]["confirmed"]
    assert abs(hit["fork_check"]["realized_net_usd"] - hit["net_profit"]) < 0.01 * hit["net_profit"]
    assert by_token[scanner.cs(cast_of_tokens["CLEAN2"])]["hits"] == []
    text = out.getvalue()
    assert text.count("PROFIT ROUTE") == len(arb["hits"]) and "CLEAN2" not in text

    # results.jsonl has one line per scanned token, and every token got its own log file
    lines = (next((tmp_path / "logs").iterdir()) / "results.jsonl").read_text().splitlines()
    assert len(lines) == len(tokens) and all(json.loads(line)["token"] for line in lines)
    assert all(pipeline.Path(r["log"]).exists() for r in results)

    # the fork is exactly as before: every simulation and the hit re-run were rolled back
    assert fork_fingerprint(w3) == before

    # second cycle: the blacklisted token is skipped without touching the fork or a worker
    again = pipeline.run_cycle(tokens, SETTINGS, workers=1, blacklist=bl, rpc_url=LOCAL_URL,
                               log_root=tmp_path / "logs", out=io.StringIO())
    skipped = [r for r in again if r["status"] == "BLACKLISTED"]
    assert [scanner.cs(r["token"]) for r in skipped] == [scanner.cs(cast_of_tokens["TAXB"])]
    assert len(again) == len(tokens)


def test_parallel_workers_use_isolated_forks(tmp_path):
    # Two workers, each with its OWN anvil fork of Base, sharing one queue. Real tokens with quick verdicts:
    # DRV (unlisted proxy -> blacklist), two non-token addresses (ERROR), BRETT (full sim on 3 pools -> SAFE).
    tokens = [("DRV", DRV), ("EOA", EOA),
              ("BRETT", "0x532f27101965dd16442E59d40670FaF5eBB142E4"), ("EOA2", "0x000000000000000000000000000000000000bEEF")]
    bl = pipeline.Blacklist(tmp_path / "bl.json")
    results = pipeline.run_cycle(tokens, SETTINGS, workers=2, blacklist=bl, base_port=8660,
                                 log_root=tmp_path / "logs", out=io.StringIO())
    by_sym = {r["symbol"]: r for r in results}
    assert len(results) == 4 and len({scanner.cs(r["token"]) for r in results}) == 4
    assert by_sym["DRV"]["status"] == "DYNAMIC_UNSAFE" and by_sym["DRV"]["reason_code"] == "PROXY"
    assert by_sym["EOA"]["status"] == "ERROR" and by_sym["EOA2"]["status"] == "ERROR"
    assert by_sym["BRETT"]["status"] == "SAFE"
    # both workers did work, each on its own fork, and each verified its own sentinel after every token
    assert {r["worker"] for r in results} == {0, 1}
    rpc_of = {}
    for r in results:
        assert r["fork_marker_ok"] is True
        assert rpc_of.setdefault(r["worker"], r["rpc"]) == r["rpc"]
    assert rpc_of[0] != rpc_of[1]
    assert set(bl.entries) == {scanner.cs(DRV)}
    # and both forks were shut down
    for rpc in rpc_of.values():
        port = int(rpc.rsplit(":", 1)[1])
        assert pipeline.port_is_free(port), f"anvil still listening on {port}"


# ---------------------------------------------------------------- the upstream RPC key never leaks

SECRET = "SECRETKEY9f3a"


def test_scrub_redacts_provider_urls_in_error_text():
    url = f"https://base-mainnet.g.alchemy.com/v2/{SECRET}"
    err = f"HTTPError: 429 Client Error: Too Many Requests for url: {url}/ (key {SECRET})"
    clean = scanner.scrub(err, url)
    assert SECRET not in clean and "https://base-mainnet.g.alchemy.com" in clean
    nested = pipeline.scrub_deep({"reason": err, "hits": [{"fork_check": {"error": err}}], "n": 3}, url)
    assert SECRET not in json.dumps(nested) and nested["n"] == 3
    assert SECRET not in scanner.scrub(f"https://x.quiknode.pro/{SECRET}/ failed", f"https://x.quiknode.pro/{SECRET}/")


@pytest.fixture
def secret_upstream():
    """A forwarding proxy to the test fork whose URL carries a fake API key, like a private provider's."""
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Forward(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            req = urllib.request.Request(LOCAL_URL, body, {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    Forward.protocol_version = "HTTP/1.1"          # keep-alive: anvil opens many parallel fetches
    ThreadingHTTPServer.request_queue_size = 256
    server = ThreadingHTTPServer(("127.0.0.1", 0), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v2/{SECRET}"
    server.shutdown()


def test_upstream_key_never_reaches_output_results_or_logs(w3, secret_upstream, tmp_path, capfd):
    # One real cycle where the forks copy from an upstream whose URL holds a key: an unlisted proxy (DRV, read-only
    # checks) and a non-token (ERROR path, exception text recorded). The key must appear nowhere we write.
    out = io.StringIO()
    tokens = [("DRV", DRV), ("EOA", EOA)]
    results = pipeline.run_cycle(tokens, SETTINGS, workers=1, blacklist=pipeline.Blacklist(tmp_path / "bl.json"),
                                 fork_url=secret_upstream, base_port=8680, log_root=tmp_path / "logs", out=out)
    assert {r["status"] for r in results} == {"DYNAMIC_UNSAFE", "ERROR"}
    written = [p.read_text() for p in (tmp_path / "logs").rglob("*") if p.is_file()]
    assert any("results" in str(p) for p in (tmp_path / "logs").rglob("*.jsonl"))
    captured = capfd.readouterr()
    for text in [out.getvalue(), captured.out, captured.err, json.dumps(results, default=str), *written]:
        assert SECRET not in text
