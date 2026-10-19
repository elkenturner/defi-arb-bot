"""Aerodrome Slipstream (concentrated liquidity): discovery, quoter wiring, real swaps on the fork."""
import scanner
from scanner import USDC, WETH

AERO = scanner.cs("0x940181a94A35A4569E4529A3CDfB74e38FD98631")


def cl_pools(w3, token):
    return [p for p in scanner.find_pools(w3, token) if p.kind == "cl"]


def test_slipstream_pools_discovered_for_both_bases(w3):
    pools = cl_pools(w3, AERO)
    assert {p.base for p in pools} == {USDC, WETH}
    for p in pools:
        dep = next(d for d in scanner.SLIPSTREAM_DEPLOYMENTS if d["quoter"] == p.quoter)
        # every discovered pool really belongs to the factory whose quoter/router it was given
        pool = w3.eth.contract(address=p.address, abi=scanner.CL_POOL_ABI)
        assert pool.functions.factory().call() == dep["factory"]
        assert p.tick_spacing > 0 and 0 < p.fee < 0.05


def test_slipstream_quote_matches_real_swap(w3):
    # The quote must equal what the router actually delivers on the fork (same block, no tax).
    pool = max((p for p in cl_pools(w3, WETH) if p.base == USDC),
               key=lambda p: scanner.pool_liquidity_usd(w3, p, WETH, scanner.eth_usd_mid(w3)))
    alice = w3.eth.accounts[5]
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        scanner.prepare_test_wallet(w3, alice)
        scanner.send(w3, scanner.erc20(w3, WETH).functions.deposit(), alice, value=10**17)
        scanner.send(w3, scanner.erc20(w3, WETH).functions.approve(pool.router, 10**17), alice)
        # Quote in next-block context (number+1, time+2s), then mine the swap exactly there, so time-based
        # dynamic fees are identical for quote and fill.
        expected = scanner.quote(w3, pool, WETH, USDC, 10**17)
        latest = w3.eth.get_block("latest")
        w3.provider.make_request("anvil_setNextBlockTimestamp", [latest.timestamp + scanner.NEXT_BLOCK_SECONDS])
        before = scanner.erc20(w3, USDC).functions.balanceOf(alice).call()
        router = w3.eth.contract(address=pool.router, abi=scanner.CL_ROUTER_ABI)
        scanner.send(w3, router.functions.exactInputSingle(
            (WETH, USDC, pool.tick_spacing, alice, latest.timestamp + 600, 10**17, 0, 0)), alice)
        got = scanner.erc20(w3, USDC).functions.balanceOf(alice).call() - before
        assert expected > 0 and got == expected
    finally:
        w3.provider.make_request("evm_revert", [snap])


def test_cl_quote_runs_in_next_block_context(w3):
    # Block overrides must be accepted by anvil, so quotes really use next-block fees (no silent fallback).
    pool = max(cl_pools(w3, WETH), key=lambda p: scanner.pool_liquidity_usd(w3, p, WETH, scanner.eth_usd_mid(w3)))
    assert scanner.quote(w3, pool, WETH, USDC, 10**16) > 0
    assert scanner._block_overrides_ok.get(id(w3.provider), True) is True


def test_slipstream_huge_quote_returns_zero_not_crash(w3):
    # A size far beyond the pool's liquidity must come back as 0 (no quote), never an exception.
    pool = cl_pools(w3, AERO)[0]
    assert scanner.quote(w3, pool, pool.base, AERO, 10**40) == 0


def test_slipstream_routes_mix_with_other_pool_types(w3):
    ctx = scanner.Context(w3, AERO, 18, "AERO", 0.0, pools=scanner.find_pools(w3, AERO),
                          eth_usd=scanner.eth_usd_mid(w3))
    cl = next(p for p in ctx.pools if p.kind == "cl")
    legacy = next(p for p in ctx.pools if p.kind == "aero" and not p.stable and p.base == cl.base)
    r = scanner.calculate_net_out(100, cl, legacy, ctx)
    assert r["ok"] and r["hops"] == 2


def test_every_cl_factory_is_registered_and_linked(w3):
    # Aerodrome's FactoryRegistry is the source of truth for live factories; our list must match it,
    # and each quoter/router must point at its own factory.
    registry = w3.eth.contract(address=scanner.cs("0x5C3F18F06CC09CA1910767A34a20F771039E37C0"),
                               abi=[scanner.fn("poolFactories", [], ["address[]"])])
    live_cl = set(registry.functions.poolFactories().call()) - {scanner.AERO_FACTORY}
    assert live_cl == {d["factory"] for d in scanner.SLIPSTREAM_DEPLOYMENTS}
    linked = [scanner.fn("factory", [], ["address"])]
    for d in scanner.SLIPSTREAM_DEPLOYMENTS:
        for role in ("quoter", "router"):
            assert w3.eth.contract(address=d[role], abi=linked).functions.factory().call() == d["factory"]


def test_shared_tick_spacing_uses_the_right_quoter(w3):
    # WETH/USDC ts=50 exists on several factories with very different depth. Each pool's quote must
    # come from its own factory's quoter, so the deep CL3 pool can't be priced as a thin one (or vice versa).
    eth = scanner.eth_usd_mid(w3)
    ts50 = [p for p in cl_pools(w3, WETH) if p.tick_spacing == 50 and p.base == USDC]
    assert len({p.quoter for p in ts50}) == len(ts50) >= 2
    for pool in ts50:
        # Compare each quote to THAT pool's own mid price (slot0 word 0). A quote served by another factory's
        # same-ts pool would land at that other pool's price instead.
        sqrt_p = int.from_bytes(w3.eth.call({"to": pool.address, "data": "0x3850c7bd"})[:32], "big")
        token0_is_weth = int(WETH, 16) < int(USDC, 16)
        raw = (sqrt_p / 2**96) ** 2
        mid = raw * 10**12 if token0_is_weth else 1 / (raw * 10**-12)        # USDC per WETH in this pool
        small = scanner.quote(w3, pool, WETH, USDC, 10**15) / 10**6 * 1000   # 0.001 WETH, scaled to 1 WETH
        assert small > 0
        assert 1 - pool.fee - 0.002 < small / mid <= 1.0, pool.label
