"""Uniswap V4 (hookless pools): deployment links, PoolId discovery, depth walk, quoter wiring, real swaps on the fork."""
import io
from contextlib import redirect_stdout

import gap_monitor
import scanner
from scanner import USDC, WETH, ZERO

CBBTC = scanner.cs("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf")   # deepest hookless V4 pools among the seed tokens
POOL_MANAGER_LINKED = [scanner.fn("poolManager", [], ["address"])]


def v4_pools(w3, token, base):
    return scanner.find_v4_pools(w3, token, base)


def deepest(w3, pools, token):
    eth_usd = scanner.eth_usd_mid(w3)
    return max(pools, key=lambda p: scanner.pool_liquidity_usd(w3, p, token, eth_usd))


def swap_and_measure(w3, pool, token_in, token_out, amount_in, who):
    """Quote, then do the real swap. Returns (quoted, received). Hookless V4 fees are static, so the approvals that
    mine blocks between quote and swap don't change the price."""
    expected = scanner.quote(w3, pool, token_in, token_out, amount_in)
    before = scanner.erc20(w3, token_out).functions.balanceOf(who).call()
    scanner.swap_on_fork(w3, pool, token_in, token_out, amount_in, who)
    return expected, scanner.erc20(w3, token_out).functions.balanceOf(who).call() - before


def fund(w3, who, eth=10**18):
    scanner.prepare_test_wallet(w3, who)
    scanner.send(w3, scanner.erc20(w3, WETH).functions.deposit(), who, value=eth)


def test_v4_deployment_is_linked(w3):
    # Each periphery contract must point at the one PoolManager, and the router must be the V4-capable one.
    for addr in (scanner.UNI_V4_POOL_MANAGER, scanner.UNI_V4_STATE_VIEW, scanner.UNI_V4_QUOTER,
                 scanner.UNI_V4_POSITION_MANAGER, scanner.UNI_V4_UNIVERSAL_ROUTER, scanner.PERMIT2):
        assert len(w3.eth.get_code(addr)) > 0, addr
    for addr in (scanner.UNI_V4_STATE_VIEW, scanner.UNI_V4_QUOTER, scanner.UNI_V4_POSITION_MANAGER,
                 scanner.UNI_V4_UNIVERSAL_ROUTER):
        linked = w3.eth.contract(address=addr, abi=POOL_MANAGER_LINKED).functions.poolManager().call()
        assert linked == scanner.UNI_V4_POOL_MANAGER, addr
    # Permit2 is an immutable in the router (no getter): its address is embedded in the router's runtime code
    assert bytes.fromhex(scanner.PERMIT2[2:].lower()) in bytes(w3.eth.get_code(scanner.UNI_V4_UNIVERSAL_ROUTER))


def test_pool_id_is_keccak_of_the_pool_key(w3):
    # Native ETH / USDC 0.05% (ts 10, no hooks): its PoolId, computed separately during research, must be live.
    key = (ZERO, USDC, 500, 10, ZERO)
    pid = scanner.v4_pool_id(key)
    assert pid == "0x96d4b53a38337a5733179751781178a2613306063c511b78cd02684739288c0a"
    view = w3.eth.contract(address=scanner.UNI_V4_STATE_VIEW, abi=scanner.V4_STATE_VIEW_ABI)
    assert view.functions.getLiquidity(pid).call() > 0


def test_v4_pools_discovered_for_erc20_and_native_eth(w3):
    usdc_pools, weth_pools = v4_pools(w3, CBBTC, USDC), v4_pools(w3, CBBTC, WETH)
    assert usdc_pools and all(p.kind == "univ4" and p.base == USDC and not p.native for p in usdc_pools)
    native = [p for p in weth_pools if p.native]
    assert native, "cbBTC has native-ETH V4 pools; they must be found and treated as the WETH base"
    for p in usdc_pools + weth_pools:
        key = scanner.v4_pool_key(p, CBBTC)
        assert key[4] == ZERO and scanner.v4_pool_id(key) == p.address
        assert (p.uni_fee_tier, p.tick_spacing) in scanner.UNI_V4_FEE_SPACINGS
        assert p.fee >= p.uni_fee_tier / 1_000_000          # protocol fee comes on top of the LP fee
    assert all(scanner.v4_pool_key(p, CBBTC)[0] == ZERO for p in native)


def test_hooked_pools_are_skipped_not_failed(w3, monkeypatch):
    # A hook can run arbitrary code on every swap, so hooked pools are out of scope: skipped with a log line.
    out = io.StringIO()
    hooked = (ZERO, USDC, 500, 10, "0x0000000000000000000000000000000000004444")
    with redirect_stdout(out):
        assert scanner.v4_pool_from_key(w3, hooked, USDC, WETH) is None
    assert "skipped: hooked" in out.getvalue()
    # Discovery only ever builds hookless keys
    seen = []
    monkeypatch.setattr(scanner, "v4_pool_from_key", lambda w3, key, token, base, log=print: seen.append(key))
    scanner.find_v4_pools(w3, CBBTC, WETH)
    assert seen and all(k[4] == ZERO for k in seen)
    assert {k[0] for k in seen} >= {ZERO}                   # native ETH looked up for the WETH base


def test_band_walk_matches_the_quoter(w3):
    # Swapping currency0 in until the price reaches the bottom of a small band must deliver what the walk says the
    # band holds: checks the tick-crossing math against the real V4 swap math (fees put back on the input).
    pool = deepest(w3, v4_pools(w3, CBBTC, USDC), CBBTC)
    key = scanner.v4_pool_key(pool, CBBTC)
    walk = scanner.v4_band_walk(w3, pool, band_ticks=200)
    for direction, (c_in, c_out) in (("down", (key[0], key[1])), ("up", (key[1], key[0]))):
        amount_in, held = walk[direction]
        got = scanner.quote(w3, pool, c_in, c_out, int(amount_in / (1 - pool.fee)))
        assert abs(got / held - 1) < 0.002, (direction, got, held)


def test_v4_quote_matches_real_swap_both_directions(w3):
    pool = deepest(w3, v4_pools(w3, CBBTC, USDC), CBBTC)
    alice = w3.eth.accounts[5]
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        fund(w3, alice)
        scanner.swap_on_fork(w3, scanner.anchor_pool(w3), WETH, USDC, 10**18, alice)
        expected, got = swap_and_measure(w3, pool, USDC, CBBTC, 1_000 * 10**6, alice)
        assert expected > 0 and got == expected
        expected, got = swap_and_measure(w3, pool, CBBTC, USDC, got, alice)
        assert expected > 0 and got == expected
    finally:
        w3.provider.make_request("evm_revert", [snap])


def test_native_eth_pool_swaps_in_and_out_of_weth(w3):
    # Routes see WETH; the fork swap unwraps before paying native ETH and wraps what it receives.
    pool = deepest(w3, [p for p in v4_pools(w3, CBBTC, WETH) if p.native], CBBTC)
    alice = w3.eth.accounts[5]
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        fund(w3, alice)
        weth = scanner.erc20(w3, WETH)
        w_before = weth.functions.balanceOf(alice).call()
        expected, bought = swap_and_measure(w3, pool, WETH, CBBTC, 10**17, alice)
        assert expected > 0 and bought == expected
        assert weth.functions.balanceOf(alice).call() == w_before - 10**17
        expected, got = swap_and_measure(w3, pool, CBBTC, WETH, bought, alice)
        assert expected > 0 and got == expected                # arrived as WETH, exactly the quoted amount
        assert scanner.erc20(w3, CBBTC).functions.balanceOf(alice).call() == 0
    finally:
        w3.provider.make_request("evm_revert", [snap])


def test_v4_oversize_quote_returns_zero(w3):
    pool = min(v4_pools(w3, CBBTC, USDC), key=lambda p: scanner.pool_liquidity_usd(w3, p, CBBTC,
                                                                                  scanner.eth_usd_mid(w3)))
    assert scanner.quote(w3, pool, USDC, CBBTC, 10**36) == 0
    assert scanner.quote(w3, pool, USDC, CBBTC, 2**128) == 0          # beyond the quoter's uint128 amount


def test_v4_pools_pass_the_fork_simulation_and_route(w3):
    # cbBTC end to end with V4 in the mix: liquidity floor, buy/transfer/sell on every pool (V4 included), routes.
    eth_usd = scanner.eth_usd_mid(w3)
    with redirect_stdout(io.StringIO()):
        pools = scanner.filter_pools_by_liquidity(w3, scanner.find_pools(w3, CBBTC), CBBTC, 50_000, eth_usd)
    v4 = [p for p in pools if p.kind == "univ4"]
    assert any(p.native for p in v4) and any(not p.native for p in v4)
    ctx = scanner.Context(w3, CBBTC, 8, "cbBTC", 0.0, rpc_url="http://127.0.0.1", pools=v4, eth_usd=eth_usd)
    with redirect_stdout(io.StringIO()):
        passed, reason = scanner.simulate_all_pools(CBBTC, ctx, quiet=True)
    assert passed, reason
    other = next(p for p in pools if p.kind != "univ4" and p.base == v4[0].base)
    r = scanner.calculate_net_out(500, v4[0], other, ctx)
    assert r["ok"] and r["hops"] == 2


def test_gap_monitor_reads_v4_mid_prices(w3):
    pool = deepest(w3, [p for p in v4_pools(w3, CBBTC, WETH) if p.native], CBBTC)
    target, data = gap_monitor.mid_request(pool)
    assert target == scanner.UNI_V4_STATE_VIEW
    mid = gap_monitor.mid_price(pool, w3.eth.call({"to": target, "data": data}), CBBTC, 8)
    tiny = scanner.quote(w3, pool, CBBTC, WETH, 10**4) / 10**18 / (10**4 / 10**8)   # WETH per cbBTC, after fee
    assert abs(tiny / mid - 1) < pool.fee + 0.001
