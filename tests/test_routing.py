"""Multi-base routing: WETH-paired pools, triangular cycles, USD->WETH sizing, gas scaling."""
import scanner
from scanner import USDC, WETH, Pool, build_legs

A_USDC = Pool("A/USDC", "aero", "0x" + "1" * 40, 0.003, base=USDC)
B_USDC = Pool("B/USDC", "aero", "0x" + "2" * 40, 0.003, base=USDC)
A_WETH = Pool("A/WETH", "aero", "0x" + "3" * 40, 0.003, base=WETH)
B_WETH = Pool("B/WETH", "aero", "0x" + "4" * 40, 0.003, base=WETH)
ANCHOR = Pool("anchor", "univ3", "0x" + "5" * 40, 0.0005, uni_fee_tier=500, base=USDC)
TOKEN = "0x" + "9" * 40


def swaps(legs):
    return [(p.label, a, b) for p, a, b in legs]


def test_legs_same_base_is_two_swaps():
    assert build_legs(A_USDC, B_USDC, TOKEN, ANCHOR)[1] == [(A_USDC, USDC, TOKEN), (B_USDC, TOKEN, USDC)]
    start, legs = build_legs(A_WETH, B_WETH, TOKEN, ANCHOR)
    assert start == WETH and len(legs) == 2


def test_legs_mixed_base_is_triangle_through_anchor():
    start, legs = build_legs(A_USDC, B_WETH, TOKEN, ANCHOR)
    assert start == USDC
    assert swaps(legs) == [("A/USDC", USDC, TOKEN), ("B/WETH", TOKEN, WETH), ("anchor", WETH, USDC)]
    start, legs = build_legs(A_WETH, B_USDC, TOKEN, ANCHOR)
    assert start == WETH
    assert swaps(legs) == [("A/WETH", WETH, TOKEN), ("B/USDC", TOKEN, USDC), ("anchor", USDC, WETH)]


def test_legs_settle_usdc_wraps_weth_legs_with_anchor():
    start, legs = build_legs(A_WETH, B_WETH, TOKEN, ANCHOR, settle="usdc")
    assert start == USDC
    assert swaps(legs) == [("anchor", USDC, WETH), ("A/WETH", WETH, TOKEN),
                           ("B/WETH", TOKEN, WETH), ("anchor", WETH, USDC)]
    assert len(build_legs(A_USDC, B_WETH, TOKEN, ANCHOR, settle="usdc")[1]) == 3
    assert len(build_legs(A_USDC, B_USDC, TOKEN, ANCHOR, settle="usdc")[1]) == 2


def test_usd_to_weth_units():
    assert scanner.usd_to_base_units(500, USDC, 2500.0) == 500 * 10**6
    assert scanner.usd_to_base_units(500, WETH, 2500.0) == 2 * 10**17


def test_gas_scales_with_swaps(w3):
    gas = scanner.build_gas_model(w3, 0.001, scanner.eth_usd_mid(w3))
    assert gas.units(2) == 300_000 and gas.units(3) == 420_000 and gas.units(4) == 540_000
    assert gas.cost_wei(2) < gas.cost_wei(3) < gas.cost_wei(4)


def test_anchor_mid_matches_quote(w3):
    # Mid price and a real 1 WETH quote should agree within the 0.05% fee + a little impact.
    mid = scanner.eth_usd_mid(w3)
    quoted = scanner.quote(w3, scanner.anchor_pool(w3), WETH, USDC, 10**18) / 10**6
    assert 0.998 < quoted / mid < 1.0


def test_weth_only_token_is_found_and_simulated_in_weth(make_trap, check):
    token = make_trap("WONLY", renounce=True, bases=(WETH,))
    ctx = check(token)
    assert [p.base for p in ctx.pools] == [WETH]
    assert ctx.verdict == "SAFE"


def test_weth_only_tax_token_is_unsafe(make_trap, check):
    ctx = check(make_trap("WTAX", tax_bps=300, bases=(WETH,)))
    assert ctx.verdict == "UNSAFE"
    assert abs(ctx.transfer_tax - 0.03) < 1e-6


def test_triangular_cycle_on_fork(w3, make_trap, check):
    # Same token on a USDC pool and a WETH pool -> both 3-swap triangles, consistent USD accounting.
    token = make_trap("TRI", renounce=True, bases=(USDC, WETH))
    ctx = check(token)
    assert ctx.verdict == "SAFE" and len(ctx.pools) == 2
    ctx.eth_usd = scanner.eth_usd_mid(w3)
    ctx.gas = scanner.build_gas_model(w3, 0.001, ctx.eth_usd)
    p_usdc, p_weth = sorted(ctx.pools, key=lambda p: p.base != USDC)
    r = scanner.calculate_net_out(500, p_usdc, p_weth, ctx)
    assert r["ok"] and r["hops"] == 3 and r["path"] == "USDC > TRI > WETH > USDC"
    assert r["start_units"] == 500 * 10**6
    assert abs(r["gas_usdc"] - ctx.gas.cost_usd(3)) < 1e-12
    expected = (r["end_units"] - r["start_units"]) / 10**6 - r["gas_usdc"]
    assert abs(r["net_profit"] - expected) < 1e-9
    # Independent check: recompute legs 1-2 with Aerodrome's volatile x*y=k formula on the pools' real
    # balances, and leg 3 with a direct anchor quote. Must match the scanner's end amount exactly.
    def xyk(amount_in, pool, t_in, t_out):
        r_in = scanner.erc20(w3, t_in).functions.balanceOf(pool.address).call()
        r_out = scanner.erc20(w3, t_out).functions.balanceOf(pool.address).call()
        amount_in -= amount_in * round(pool.fee * 10_000) // 10_000
        return amount_in * r_out // (r_in + amount_in)
    tokens = xyk(r["start_units"], p_usdc, USDC, token)
    weth = xyk(tokens, p_weth, token, WETH)
    usdc_back = scanner.quote(w3, scanner.anchor_pool(w3), WETH, USDC, weth)
    assert abs(r["end_units"] - usdc_back) <= 2
    # $500 into $60k-deep pools: ~1.6% impact per pool leg + 0.65% fees -> roughly -$19.
    assert -25 < r["net_profit"] < -15

    r2 = scanner.calculate_net_out(500, p_weth, p_usdc, ctx)
    assert r2["ok"] and r2["path"] == "WETH > TRI > USDC > WETH"
    assert r2["start_units"] == scanner.usd_to_base_units(500, WETH, ctx.eth_usd)


def test_honeypot_on_shallower_pool_is_caught(make_trap, check):
    # Review finding: sells into the USDC pool revert, but the WETH pool is deeper and clean.
    # Every pool must be simulated, or routes selling into the USDC pool would pass as SAFE.
    token = make_trap("HP2", honeypot=True, renounce=True, bases=(USDC, WETH),
                      side_usd={USDC: 30_000, WETH: 40_000})
    ctx = check(token)
    assert len(ctx.pools) == 2
    assert max(ctx.pools, key=lambda p: p.liquidity_usd).base == WETH   # the trap is NOT on the deepest pool
    assert ctx.verdict == "UNSAFE"


def test_tax_token_on_two_bases_reports_tax(make_trap, check):
    # Tax token with a USDC and a WETH pool: every pool is simulated, worst tax lands in ctx.
    ctx = check(make_trap("TAX2P", tax_bps=200, renounce=True, bases=(USDC, WETH)))
    assert ctx.verdict == "UNSAFE"
    assert abs(ctx.transfer_tax - 0.02) < 1e-6
