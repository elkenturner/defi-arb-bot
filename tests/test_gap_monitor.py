"""gap_monitor.py: batched quotes == scanner quotes, mid prices, finding a real gap, budgets, missed blocks, report."""
import io
import json
import sqlite3

import pytest

import gap_monitor as g
import scanner
from conftest import LOCAL_URL
from scanner import USDC, WETH

AERO = scanner.cs("0x940181a94A35A4569E4529A3CDfB74e38FD98631")


def targets_for(w3, token, symbol):
    with open("/dev/null", "w") as quiet:
        from contextlib import redirect_stdout
        with redirect_stdout(quiet):
            pools = scanner.filter_pools_by_liquidity(w3, scanner.find_pools(w3, token), token, 50_000)
    return [{"token": token, "symbol": symbol, "decimals": scanner.erc20(w3, token).functions.decimals().call(),
             "pools": pools, "buy_tax": 0.0, "sell_tax": 0.0}]


def test_batched_quotes_equal_single_quotes(w3):
    # Multicall3 batching must give the exact same numbers as scanner.quote(), for every venue type.
    batcher = g.Batcher(w3)
    block = w3.eth.get_block("latest")
    pools = [p for p in scanner.find_pools(w3, AERO) if p.kind in ("univ3", "aero", "cl")]
    assert {p.kind for p in pools} == {"univ3", "aero", "cl"}
    reqs, expected = [], []
    for p in pools:
        amount = scanner.usd_to_base_units(100, p.base, scanner.eth_usd_mid(w3))
        reqs.append(scanner.quote_request(w3, p, p.base, AERO, amount))
        expected.append(scanner.quote(w3, p, p.base, AERO, amount))
    raws = batcher.run(reqs, block)
    got = [scanner.decode_quote(w3, p, raw) if raw else 0 for p, raw in zip(pools, raws)]
    assert got == expected
    assert batcher.overrides_ok


def test_mid_prices_agree_with_tiny_quotes(w3):
    batcher = g.Batcher(w3)
    block = w3.eth.get_block("latest")
    t = targets_for(w3, AERO, "AERO")[0]
    raws = batcher.run([g.mid_request(p) for p in t["pools"]], block)
    for p, raw in zip(t["pools"], raws):
        if p.stable:
            continue
        mid = g.mid_price(p, raw, AERO, 18)                       # base per AERO
        probe = scanner.quote(w3, p, AERO, p.base, 10**18)        # sell 1 AERO
        implied = probe / 10**scanner.BASES[p.base][1] / (1 - p.fee)
        assert abs(implied / mid - 1) < 0.01, p.label


def test_stable_curve_marginal_price_is_one_when_balanced():
    assert g.stable_marginal_price(1000.0, 1000.0) == pytest.approx(1.0)


def test_stable_curve_marginal_price_matches_numeric_derivative():
    x, y, eps = 1200.0, 800.0, 1e-3
    k = x**3 * y + x * y**3

    def y_for(xv):                                                   # solve x^3 y + x y^3 = k for y (bisection)
        lo, hi = 0.0, 10 * y
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if xv**3 * mid + xv * mid**3 < k else (lo, mid)
        return (lo + hi) / 2
    numeric = (y_for(x - eps) - y_for(x + eps)) / (2 * eps)
    assert g.stable_marginal_price(x, y) == pytest.approx(numeric, rel=1e-4)


@pytest.fixture(scope="module")
def arb_setup(w3, make_trap):
    # Same fake mispriced token as the pipeline test: $1 in the USDC pool, $1.11 in the WETH pool.
    token = make_trap("GAPARB", renounce=True, bases=(USDC, WETH), token_side={WETH: 27_000})
    targets = targets_for(w3, token, "GAPARB")
    routes = g.build_routes(targets, scanner.anchor_pool(w3))
    return token, targets, routes


def run_block(w3, routes, tmp_path, budget):
    db = g.open_db(tmp_path / "opp.db")
    g.save_routes(db, routes)
    gas = scanner.build_gas_model(w3, 0.001, scanner.eth_usd_mid(w3))
    out = g.monitor_block(w3, g.Batcher(w3), db, routes, w3.eth.get_block("latest"), gas, budget)
    return db, out


def test_monitor_block_finds_and_stores_a_real_gap(w3, arb_setup, tmp_path):
    token, targets, routes = arb_setup
    assert len(routes) == 2
    db, (quoted, cycle_ms, winners) = run_block(w3, routes, tmp_path, budget=len(routes))
    assert quoted == 2
    rows = {r[0]: r for r in db.execute("SELECT r.path, o.gross_bps, o.fee_bps, o.net_500, o.best_net, o.best_size "
                                        "FROM obs o JOIN routes r USING (route_id)")}
    good, bad = rows["USDC > GAPARB > WETH > USDC"], rows["WETH > GAPARB > USDC > WETH"]
    assert good[1] == pytest.approx(30_000 / 27_000 * 10_000 - 10_000, rel=0.01)   # ~ +1,111 bps gross
    assert good[2] == pytest.approx(65, abs=1)                     # per-block fees: 0.3% + 0.3% + 0.05% anchor
    assert good[4] > 1 and good[3] > 1 and good[5] in g.SIZES
    assert bad[4] < 0
    assert [w[0]["path"] for w in winners] == ["USDC > GAPARB > WETH > USDC"]
    # consistent with the one-route-at-a-time scanner calculation
    ctx = scanner.Context(w3, token, 18, "GAPARB", 0.0, pools=targets[0]["pools"], eth_usd=scanner.eth_usd_mid(w3),
                          gas=scanner.build_gas_model(w3, 0.001, scanner.eth_usd_mid(w3)))
    a = next(p for p in targets[0]["pools"] if p.base == USDC)
    b = next(p for p in targets[0]["pools"] if p.base == WETH)
    assert good[3] == pytest.approx(scanner.calculate_net_out(500, a, b, ctx)["net_profit"], abs=0.05)


def test_budget_quotes_the_most_promising_route_first(w3, arb_setup, tmp_path):
    _, _, routes = arb_setup
    db, (quoted, _, _) = run_block(w3, routes, tmp_path, budget=1)
    assert quoted == 1
    rows = dict(db.execute("SELECT r.path, o.best_net FROM obs o JOIN routes r USING (route_id)").fetchall())
    assert rows["USDC > GAPARB > WETH > USDC"] is not None          # highest gross-minus-fees: quoted
    assert rows["WETH > GAPARB > USDC > WETH"] is None              # not quoted: NULL, never a guess
    assert db.execute("SELECT COUNT(*) FROM obs WHERE gross_bps IS NULL").fetchone()[0] == 0   # spreads for all


def test_plan_next_always_takes_the_newest_block():
    assert g.plan_next(None, 100) == 100
    assert g.plan_next(100, 100) is None
    assert g.plan_next(100, 104) == 104      # 101-103 count as missed via the run's block range


def test_budget_adapts_to_block_time():
    b = g.Budget(total=400, start=40)
    b.update(2500)
    assert b.k == 30
    b.update(500)
    assert b.k > 30
    for _ in range(50):
        b.update(100)
    assert b.k == 400


def test_streaks():
    assert g.longest_run([(1, 2), (2, 3), (3, -1), (4, 5), (5, 5), (6, 5), (8, 5)], 0) == 3
    assert g.longest_run([(1, 2), (2, None), (3, 2)], 0) == 1        # an unquoted block breaks a CONFIRMED streak
    # ...but not a bridged span: blocks 2 (unknown) and 4-5 (missed) weren't seen, so the gap may have stayed open
    assert g.longest_bridged_run([(1, "win"), (2, "unknown"), (3, "win"), (6, "win"), (7, "loss"), (8, "win")]) == 6
    assert g.longest_bridged_run([(1, "loss"), (2, "unknown")]) == 0


def make_report_db(path, missed_tail=0):
    db = g.open_db(path)
    db.execute("INSERT INTO routes VALUES ('r1','T','0xT','0xA','0xB','T: A -> B','USDC > T > USDC',2)")
    run = g.start_run(db, 10, "https://host", False, True, 1, 1, "results.jsonl")
    obs = [(10, 90, 60, 0.5), (11, 95, 60, 1.5), (12, 80, 60, 2.0), (13, 70, 60, None), (14, 10, 60, None),
           (15, 90, None, None)]
    for blk, gross, fee, best in obs:
        db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?)", (run, blk, 1000 + 2 * blk, 2500, 1,
                                                                 int(best is not None), 900))
        db.execute("INSERT INTO obs VALUES (?,?,?,?,?,?,?,?)", (run, blk, "r1", gross, fee, best, best,
                                                                500 if best else None))
    g.touch_run(db, run, 15 + missed_tail)
    return run


def test_report_counts_and_unknowns(tmp_path):
    make_report_db(tmp_path / "r.db", missed_tail=2)          # blocks 16-17 never done (e.g. RPC outage at the end)
    out = io.StringIO()
    r = g.report(tmp_path / "r.db", out=out)[0]
    assert (r["watched"], r["quoted"], r["gt0"], r["gt1"], r["run0"], r["best"]) == (6, 3, 3, 2, 3, 2.0)
    assert r["unknown"] == 1      # block 13: unquoted while gross (70) > fee (60). Block 14 provably lost (10 < 60)
    assert r["unreadable"] == 1   # block 15: fee unreadable
    text = out.getvalue()
    assert "6 of 8 blocks watched" in text and "2 missed" in text
    assert "coverage was complete" not in text


def test_report_claims_complete_coverage_only_when_true(tmp_path):
    db = g.open_db(tmp_path / "c.db")
    db.execute("INSERT INTO routes VALUES ('r1','T','0xT','0xA','0xB','T: A -> B','USDC > T > USDC',2)")
    run = g.start_run(db, 10, "h", True, True, 1, 1, "x")
    for blk in (10, 11):
        db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?)", (run, blk, 1000 + blk, 2500, 1, 0, 900))
        db.execute("INSERT INTO obs VALUES (?,?,?,?,?,?,?,?)", (run, blk, "r1", 5, 60, None, None, None))
    g.touch_run(db, run, 11)
    out = io.StringIO()
    g.report(tmp_path / "c.db", out=out)
    assert "coverage was complete" in out.getvalue()


def test_separate_runs_are_reported_separately(tmp_path):
    first = make_report_db(tmp_path / "r.db")
    db = g.open_db(tmp_path / "r.db")
    second = g.start_run(db, 500, "h", True, True, 1, 1, "x")
    g.touch_run(db, second, 503)
    out = io.StringIO()
    assert g.report(tmp_path / "r.db", out=out) == []            # latest run (no blocks yet) is reported alone
    assert "0 of 4 blocks" in out.getvalue()
    out = io.StringIO()
    assert len(g.report(tmp_path / "r.db", out=out, run_id=first)) == 1


def test_slipstream_fee_is_read_live_every_block(w3):
    # The per-block fee must equal pool.fee() evaluated in next-block context (= the factory's live getSwapFee).
    pools = [p for p in scanner.find_pools(w3, AERO) if p.kind == "cl"]
    block = w3.eth.get_block("latest")
    fees = g.block_fees(pools, g.Batcher(w3).run(g.fee_requests(pools), block))
    for p in pools:
        raw = w3.provider.make_request("eth_call", [{"to": p.address, "data": g.FEE}, hex(block.number), {},
                                                    scanner.next_block_overrides(block)])["result"]
        assert fees[p.address] == int(raw, 16) / 1_000_000, p.label


def test_rpc_errors_fail_the_block_without_switching_modes(w3, monkeypatch):
    batcher = g.Batcher(w3)
    assert batcher.probe_overrides(w3.eth.get_block("latest")) is True
    monkeypatch.setattr(w3.provider, "make_request",
                        lambda method, params: {"error": {"code": -32001, "message": "block not found: 0x1"}})
    with pytest.raises(RuntimeError, match="block not found"):
        batcher.run([(g.MULTICALL3, "0x")], w3.eth.get_block("latest"))
    assert batcher.overrides_ok is True                          # a lagging node is not "overrides unsupported"


def test_rpc_key_is_never_printed(w3, arb_setup, tmp_path, monkeypatch):
    token, _, _ = arb_setup
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"token": token, "symbol": "GAPARB", "status": "SAFE",
                                   "taxes": {"buy": 0, "sell": 0}}) + "\n")
    monkeypatch.setattr(g, "rpc_url", lambda: (f"{LOCAL_URL}/?apikey=SECRETKEY123", True))
    out = io.StringIO()
    g.watch(0.05, db_path=tmp_path / "w.db", results=results, out=out)
    text = out.getvalue()
    assert "SECRETKEY123" not in text and "127.0.0.1" in text
    con = sqlite3.connect(tmp_path / "w.db")
    assert con.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] >= 1
    assert "SECRETKEY123" not in str(con.execute("SELECT * FROM runs").fetchall())   # not stored in the DB either


def test_wait_for_move_triggers_on_big_move_only():
    import io
    t = {"now": 0.0}
    prices = iter([3000, 3005, 2998, 3010, 3040])           # 3040 vs low 2998 = 1.4%
    hit, move = g.wait_for_move(None, 1.0, 30, 1, out=io.StringIO(), poll=60, clock=lambda: t["now"],
                                 sleep=lambda s: t.__setitem__("now", t["now"] + s), price=lambda: next(prices))
    assert hit and move > 1.0
    t["now"] = 0.0
    calm = iter([3000 + (i % 3) for i in range(1000)])
    hit, move = g.wait_for_move(None, 1.0, 30, 1, out=io.StringIO(), poll=60, clock=lambda: t["now"],
                                 sleep=lambda s: t.__setitem__("now", t["now"] + s), price=lambda: next(calm))
    assert not hit and move < 0.1


def test_old_samples_leave_the_window():
    import io
    t = {"now": 0.0}
    prices = iter([3000] + [3020] * 100)   # the 3000 sample ages out after 30 min, so 0.67% never reaches 1%
    hit, _ = g.wait_for_move(None, 1.0, 30, 2, out=io.StringIO(), poll=600, clock=lambda: t["now"],
                              sleep=lambda s: t.__setitem__("now", t["now"] + s), price=lambda: next(prices))
    assert not hit
