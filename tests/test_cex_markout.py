"""cex_markout: pool pairing, markout sign and timing, per-sender classification, the decision rule, Coinbase paging."""

import pytest

import arb_history as ah
import cex_markout as cm

USDC, WETH, CBBTC = ah.USDC, ah.WETH, cm.CBBTC
ETH_POOL, BTC_POOL = "0x" + "e1" * 20, "0x" + "b1" * 20
META = {ETH_POOL: {"token0": WETH, "token1": USDC}, BTC_POOL: {"token0": USDC, "token1": CBBTC}}


def test_only_usdc_vs_weth_native_or_cbbtc_pools_are_targets():
    assert cm.pair_of({"token0": WETH, "token1": USDC}) == (WETH, 0)
    assert cm.pair_of({"token0": ah.ZERO, "token1": USDC}) == (WETH, 0)      # V4 native ETH = WETH
    assert cm.pair_of({"token0": USDC, "token1": CBBTC}) == (CBBTC, 1)
    assert cm.pair_of({"token0": WETH, "token1": CBBTC}) is None              # no USDC leg
    assert cm.pair_of({"token0": None, "token1": None}) is None


def test_markout_sign_is_the_traders_gain():
    # Trader buys 1 ETH for 2,500 USDC (pool: -1 ETH, +2,500 USDC). Coinbase later at 2,510 -> +$10; at 2,490 -> -$10.
    assert cm.swap_markout(-10**18, 2_500 * 10**6, 18, 2_510) == pytest.approx(10)
    assert cm.swap_markout(-10**18, 2_500 * 10**6, 18, 2_490) == pytest.approx(-10)
    # Trader sells 0.1 cbBTC for 7,000 USDC (pool: +0.1 BTC, -7,000 USDC). BTC later at 69,000 -> +$100.
    assert cm.swap_markout(10**7, -7_000 * 10**6, 8, 69_000) == pytest.approx(100)


def test_swap_time_is_one_second_before_the_block_timestamp():
    assert cm.block_time(110, 100, 1_000) == 1_000 + 20 - 1


def test_price_is_the_last_trade_at_or_before():
    series = ([10.0, 11.0, 13.0], [1.0, 2.0, 3.0])
    assert cm.price_at(series, 9.9) is None
    assert cm.price_at(series, 11.0) == 2.0 and cm.price_at(series, 12.9) == 2.0 and cm.price_at(series, 99) == 3.0


def test_tx_markouts_sum_a_txs_swaps_and_read_each_horizon():
    ref = (100, 1_000)                                       # block 100 has timestamp 1,000 -> swaps at t = 999
    t = 999.0
    series = {"ETH-USD": ([t - 5, t + 2, t + 12, t + 60], [2_500.0, 2_501.0, 2_505.0, 2_490.0]),
              "BTC-USD": ([0.0], [70_000.0])}
    # one tx, two swaps on the same pool: buys 0.4 + 0.6 ETH for 2,500 USDC in total
    sw = [(100, 3, 7, "0xt", ETH_POOL, None, str(-4 * 10**17), str(1_000 * 10**6), "v3"),
          (100, 3, 9, "0xt", ETH_POOL, None, str(-6 * 10**17), str(1_500 * 10**6), "v3")]
    m = cm.tx_markouts(sw, META, series, ref)
    assert m["m0"] == pytest.approx(0) and m["m2"] == pytest.approx(1) and m["m12"] == pytest.approx(5)
    assert m["m60"] == pytest.approx(-10) and m["notional_usd"] == pytest.approx(2_500)
    btc = [(100, 1, 1, "0xb", BTC_POOL, None, str(-7_000 * 10**6), str(10**7), "v3")]   # trader sells 0.1 BTC for 7,000
    assert cm.tx_markouts(btc, META, series, ref)["m12"] == pytest.approx(-0.1 * 70_000 + 7_000)
    early = {"ETH-USD": ([t + 1], [2_500.0]), "BTC-USD": ([0.0], [1.0])}
    assert cm.tx_markouts(sw, META, early, ref) is None      # no Coinbase price yet at execution: not guessed


def mk(m12, m2=None, m60=None, notional=1_000.0):
    return {"m0": 0.0, "m2": m12 if m2 is None else m2, "m12": m12, "m60": m12 if m60 is None else m60,
            "notional_usd": notional}


def test_sender_summary_keeps_consistent_private_winners_only():
    bot, bot2, bot3, small, router, loser = ("0x" + c * 40 for c in "abcdef")
    rows = ([(bot, mk(2.0))] * 30 + [(bot, mk(-1.0))] * 10 +           # 75% won, +50 total
            [(bot2, mk(1.0))] * 20 + [(bot3, mk(0.5))] * 20 +
            [(small, mk(5.0))] * 5 +                                    # too few swaps to judge
            [(router, mk(3.0))] * 40 +                                  # good flow, but from many users
            [(loser, mk(1.0, m60=-2.0))] * 30)                          # positive at +12 s, not at +60 s
    s = cm.sender_summary(rows, days=2, gas_usd={bot: 0.1}, eoas={bot: (3, 40), router: (35, 40)}, arb_bots={bot2})
    kinds = {r["sender"]: r["kind"] for r in s["senders"]}
    assert kinds == {bot: "cexdex", bot2: "cexdex", bot3: "cexdex", small: "other", router: "public", loser: "other"}
    assert s["distinct_cexdex"] == 3 and s["overlap_with_arb_contracts"] == 1
    assert s["arb"]["markout_12s_usd_day"] == pytest.approx((50 + 20 + 10) / 2)
    assert s["arb"]["gas_usd_day"] == pytest.approx(40 * 0.1 / 2)
    assert s["arb"]["net_12s_usd_day"] == pytest.approx((80 - 4) / 2)
    assert s["arb"]["net_12s_after_hedge_usd_day"] == pytest.approx((80 - 4 - 80 * 1_000 * 5e-4) / 2)
    assert s["top3_share"] == pytest.approx(1.0)
    assert s["arb"]["notional_usd_day"] == pytest.approx(80 * 1_000 / 2)
    assert s["arb"]["breakeven_hedge_bps"] == pytest.approx(76 / 80_000 * 1e4)        # net / notional


def test_decision_rule_stops_when_the_top_3_take_most():
    assert cm.decide({"top3_share": 0.8, "arb": {"net_12s_usd_day": 100}}).startswith("stop")
    assert cm.decide({"top3_share": 0.3, "arb": {"net_12s_usd_day": 100}}).startswith("continue")
    assert cm.decide({"top3_share": None, "arb": {"net_12s_usd_day": 0}}).startswith("stop")


def test_event_sender_is_the_indexed_first_topic():
    log = {"topics": [ah.T_V3, "0x" + "00" * 12 + "ab" * 20, "0x" + "00" * 12 + "cd" * 20]}
    assert cm.event_sender(log) == "0x" + "ab" * 20


def test_coinbase_public_trades_page_back_without_a_key():
    """Live: the public trade history has sub-second timestamps and contiguous ids (candles stop at 60 s)."""
    page = cm.parse_trades(cm.cb_get("/products/ETH-USD/trades?limit=100"))
    ids = [i for i, _, _ in page]
    assert ids == sorted(ids, reverse=True) and ids[0] - ids[-1] == len(ids) - 1
    assert any(t % 1 for _, t, _ in page) and all(100 < p < 100_000 for _, _, p in page)
    db = cm.open_db(":memory:")
    t_end = page[-1][1]
    cm.fetch_cex(db, "ETH-USD", t_end - 3, t_end, threads=1, log=lambda *a, **k: None)
    times, prices = cm.load_series(db, "ETH-USD")
    assert times == sorted(times) and any(t_end - 3 <= t <= t_end for t in times)
