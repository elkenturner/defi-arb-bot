"""arb_history: swap decoding, loop detection, venue-edge profit, report math, and 3 real Base arbs re-checked.

The real arbs were found by `arb_history.py` on 2026-10-19 and validated independently: each profit recipient's
on-chain balance change from block N-1 to N equals the measured profit exactly, with no other tx touching it.
The three largest are the day's top arbs after excluding LP rebalances.
"""
import json
import os
import sqlite3

import pytest
from eth_abi import encode

import arb_history as ah
import scanner

WETH, USDC, ZERO, PM = ah.WETH, ah.USDC, scanner.ZERO, ah.POOL_MANAGER
TOK = "0x" + "11" * 20
POOL_A, POOL_B = "0x" + "aa" * 20, "0x" + "bb" * 20
BOT, EOA, PROFIT_TO = "0x" + "b0" * 20, "0x" + "e0" * 20, "0x" + "70" * 20
ETH_USD = 2_400.0


def pad(addr):
    return "0x" + "0" * 24 + addr[2:]


def word(v, signed=False):
    return encode(["int256" if signed else "uint256"], [v]).hex()


def transfer(token, frm, to, value, i):
    return {"address": token, "topics": [ah.T_TRANSFER, pad(frm), pad(to)], "data": "0x" + word(value),
            "logIndex": hex(i)}


def v3_swap(pool, d0, d1, i, sqrtp=2**96):
    return {"address": pool, "topics": [ah.T_V3, pad(BOT), pad(BOT)],
            "data": "0x" + word(d0, True) + word(d1, True) + word(sqrtp) + word(1) + word(0), "logIndex": hex(i)}


def aero_swap(pool, a0in, a1in, a0out, a1out, i):
    return {"address": pool, "topics": [ah.T_AERO, pad(BOT), pad(BOT)],
            "data": "0x" + "".join(word(x) for x in (a0in, a1in, a0out, a1out)), "logIndex": hex(i)}


def v4_swap(pid, sender, a0, a1, i):
    """V4 emits the swapper's delta: negative = the swapper paid."""
    return {"address": PM, "topics": [ah.T_V4, pid, pad(sender)],
            "data": "0x" + word(a0, True) + word(a1, True) + word(2**96) + word(1) + word(0, True) + word(500),
            "logIndex": hex(i)}


def receipt(logs, frm=EOA, to=BOT, gas=300_000, price=10**7, l1=10**9):
    return {"transactionHash": "0x" + "ab" * 32, "blockNumber": hex(100), "transactionIndex": hex(7), "from": frm,
            "to": to, "status": "0x1", "gasUsed": hex(gas), "effectiveGasPrice": hex(price), "l1Fee": hex(l1),
            "logs": logs}


META = {POOL_A: {"venue": "univ3", "factory": "", "token0": TOK, "token1": WETH, "hooks": None},
        POOL_B: {"venue": "aero", "factory": "", "token0": TOK, "token1": WETH, "hooks": None}}


def two_pool_arb(profit_to=BOT):
    """1 WETH -> 1000 TOK on A (univ3), 1000 TOK -> 1.01 WETH on B (aero). Bot keeps (or forwards) the 0.01 WETH."""
    logs = [transfer(TOK, POOL_A, BOT, 1000 * 10**18, 0), transfer(WETH, BOT, POOL_A, 10**18, 1),
            v3_swap(POOL_A, -1000 * 10**18, 10**18, 2),
            transfer(TOK, BOT, POOL_B, 1000 * 10**18, 3), transfer(WETH, POOL_B, BOT, 101 * 10**16, 4),
            aero_swap(POOL_B, 1000 * 10**18, 0, 0, 101 * 10**16, 5)]
    if profit_to != BOT:
        logs.append(transfer(WETH, BOT, profit_to, 10**16, 6))
    return receipt(logs)


def test_decode_swap_normalizes_every_source_to_the_pools_view():
    s = ah.decode_swap(v3_swap(POOL_A, -5, 7, 0))
    assert (s["src"], s["pool"], s["d0"], s["d1"]) == ("v3", POOL_A, -5, 7)
    s = ah.decode_swap(aero_swap(POOL_B, 10, 0, 0, 4, 0))
    assert (s["src"], s["d0"], s["d1"]) == ("aero", 10, -4)
    pid = "0x" + "cd" * 32
    s = ah.decode_swap(v4_swap(pid, BOT, -10, 4, 0))        # swapper paid 10 of currency0, got 4 of currency1
    assert (s["src"], s["pool"], s["sender"], s["d0"], s["d1"]) == ("v4", pid, BOT, 10, -4)
    fake_v4 = v4_swap(pid, BOT, -10, 4, 0) | {"address": "0x" + "99" * 20}
    assert ah.decode_swap(fake_v4) is None                    # V4 Swap events only count from the PoolManager


def test_loop_detection_accepts_cycles_in_any_log_order_and_rejects_multihop():
    a, b, c = "0xa", "0xb", "0xc"
    assert ah.is_loop([(a, 1, b, 1), (b, 1, a, 1)])
    assert ah.is_loop([(c, 1, a, 1), (b, 1, c, 1), (a, 1, b, 1)])      # nested callbacks emit inner swaps first
    assert not ah.is_loop([(a, 1, b, 1), (b, 1, c, 1)])                 # plain A -> B -> C trade
    assert not ah.is_loop([(a, 1, b, 1), (a, 1, b, 1), (b, 1, c, 1)])   # split route, still one-way


def test_two_pool_arb_profit_and_gas():
    r = ah.classify(two_pool_arb(), META, ETH_USD, base_fee=4 * 10**6)
    assert r["arb"] and r["start"] == WETH and r["tokens"] == [TOK] and r["venues"] == ["aero", "univ3"]
    assert r["flows"] == {WETH: str(10**16)}
    assert r["gross_usd"] == pytest.approx(0.01 * ETH_USD)
    assert r["l2_gas_usd"] == pytest.approx(300_000 * 10**7 / 1e18 * ETH_USD)
    assert r["l1_fee_usd"] == pytest.approx(1e9 / 1e18 * ETH_USD)
    assert r["priority_usd"] == pytest.approx(300_000 * 6 * 10**6 / 1e18 * ETH_USD)
    assert r["net_usd"] == pytest.approx(r["gross_usd"] - r["l2_gas_usd"] - r["l1_fee_usd"])
    assert r["reachable"] and not r["hooked"] and r["bot"] == BOT


def test_profit_sent_to_a_third_address_still_counts():
    # Real bots forward profit elsewhere in the same tx: measuring at the venue edge doesn't care where it went.
    r = ah.classify(two_pool_arb(profit_to=PROFIT_TO), META, ETH_USD, 0)
    assert r["arb"] and r["flows"] == {WETH: str(10**16)}


def test_flash_loan_from_the_pool_manager_nets_out():
    rec = two_pool_arb()
    rec["logs"] = [transfer(WETH, PM, BOT, 10**18, 90)] + rec["logs"] + [transfer(WETH, BOT, PM, 10**18, 91)]
    r = ah.classify(rec, META, ETH_USD, 0)
    assert r["arb"] and r["flows"] == {WETH: str(10**16)}


def test_user_trades_are_not_arbs():
    # Multi-hop TOK -> WETH -> USDC through two pools: no loop
    meta = META | {POOL_B: {"venue": "aero", "factory": "", "token0": WETH, "token1": USDC, "hooks": None}}
    rec = receipt([transfer(TOK, EOA, POOL_A, 10**21, 0), transfer(WETH, POOL_A, BOT, 10**18, 1),
                   v3_swap(POOL_A, 10**21, -10**18, 2), transfer(WETH, BOT, POOL_B, 10**18, 3),
                   transfer(USDC, POOL_B, EOA, 2_400 * 10**6, 4), aero_swap(POOL_B, 10**18, 0, 0, 2_400 * 10**6, 5)])
    assert ah.classify(rec, meta, ETH_USD, 0)["reason"] == "no_loop"
    # A round trip that comes back with LESS of what it paid in
    rec = two_pool_arb()
    rec["logs"][4] = transfer(WETH, POOL_B, BOT, 99 * 10**16, 4)
    rec["logs"][5] = aero_swap(POOL_B, 1000 * 10**18, 0, 0, 99 * 10**16, 5)
    assert ah.classify(rec, META, ETH_USD, 0)["reason"] == "not_up"
    # One pool only
    rec = receipt(two_pool_arb()["logs"][:3])
    assert ah.classify(rec, META, ETH_USD, 0)["reason"] == "single_pool"


def test_native_eth_leg_of_a_v4_pool_comes_from_the_swap_event():
    # V4 ETH/TOK pool (native, no log for ETH): buy TOK there with 1 ETH, sell it on aero for 1.02 WETH.
    pid = "0x" + "cd" * 32
    meta = META | {pid: {"venue": "univ4", "factory": PM, "token0": ZERO, "token1": TOK, "hooks": ZERO}}
    rec = receipt([v4_swap(pid, BOT, -10**18, 1000 * 10**18, 0), transfer(TOK, PM, BOT, 1000 * 10**18, 1),
                   transfer(TOK, BOT, POOL_B, 1000 * 10**18, 2), transfer(WETH, POOL_B, BOT, 102 * 10**16, 3),
                   aero_swap(POOL_B, 1000 * 10**18, 0, 0, 102 * 10**16, 4)])
    r = ah.classify(rec, meta, ETH_USD, 0)
    assert r["arb"] and r["flows"] == {WETH: str(2 * 10**16)} and r["venues"] == ["aero", "univ4"]
    assert r["reachable"]


def test_unresolved_v4_pool_is_not_valued():
    pid = "0x" + "ef" * 32                                  # not in META: key never resolved
    rec = receipt([v4_swap(pid, BOT, -10**18, 1000 * 10**18, 0), transfer(TOK, PM, BOT, 1000 * 10**18, 1),
                   transfer(TOK, BOT, POOL_B, 1000 * 10**18, 2), transfer(WETH, POOL_B, BOT, 102 * 10**16, 3),
                   aero_swap(POOL_B, 1000 * 10**18, 0, 0, 102 * 10**16, 4)])
    assert ah.classify(rec, META, ETH_USD, 0)["reason"] == "unknown_pool"


def test_hooked_native_eth_pool_is_not_valued():
    # A hook can settle native ETH the Swap event never shows (seen on Base: an event implying 2.9M ETH)
    pid = "0x" + "cd" * 32
    meta = META | {pid: {"venue": "univ4", "factory": PM, "token0": ZERO, "token1": TOK, "hooks": "0x" + "44" * 20}}
    rec = receipt([v4_swap(pid, BOT, -10**18, 1000 * 10**18, 0), transfer(TOK, PM, BOT, 1000 * 10**18, 1),
                   transfer(TOK, BOT, POOL_B, 1000 * 10**18, 2), transfer(WETH, POOL_B, BOT, 102 * 10**16, 3),
                   aero_swap(POOL_B, 1000 * 10**18, 0, 0, 102 * 10**16, 4)])
    assert ah.classify(rec, meta, ETH_USD, 0)["reason"] == "hooked_native"


def test_hook_contract_transfers_are_inside_the_venue():
    # Hooked WETH/TOK pool: the hook pulls TOK out of the PoolManager and pays it back (its own liquidity). That's
    # venue-internal; only the bot's transfers cross the edge.
    pid, hook = "0x" + "cd" * 32, "0x" + "44" * 20
    meta = META | {pid: {"venue": "univ4", "factory": PM, "token0": TOK, "token1": WETH, "hooks": hook}}
    rec = receipt([transfer(TOK, PM, hook, 5 * 10**21, 0), transfer(TOK, hook, PM, 5 * 10**21, 1),
                   transfer(WETH, BOT, PM, 10**18, 2), v4_swap(pid, BOT, 1000 * 10**18, -10**18, 3),
                   transfer(TOK, PM, BOT, 1000 * 10**18, 4),
                   transfer(TOK, BOT, POOL_B, 1000 * 10**18, 5), transfer(WETH, POOL_B, BOT, 101 * 10**16, 6),
                   aero_swap(POOL_B, 1000 * 10**18, 0, 0, 101 * 10**16, 7), transfer(TOK, hook, "0x" + "c1" * 20, 7, 8)])
    r = ah.classify(rec, meta, ETH_USD, 0)
    assert r["arb"] and r["hooked"] and not r["reachable"]
    assert r["flows"] == {WETH: str(10**16)}                 # the hook's 7-wei payout to someone isn't the arb's


def test_token_profit_is_priced_through_the_txs_own_swaps():
    # 2,400 USDC buys 1 WETH, which buys 1,000 TOK; TOK has no other price source
    directions = [(USDC, 2_400 * 10**6, WETH, 10**18), (WETH, 10**18, TOK, 1000 * 10**18)]
    p = ah.price_per_raw(directions, ETH_USD)
    assert p[TOK] * 10**18 == pytest.approx(2.4)


def test_summary_split_shares_and_medians():
    seed = {TOK: "SEED"}
    other = "0x" + "22" * 20
    arbs = [
        {"tokens": [TOK], "bot": "b1", "net_usd": 10, "gross_usd": 20, "priority_usd": 2, "l2_gas_usd": 9,
         "l1_fee_usd": 1, "reachable": True, "hooked": False, "prev_same_pool": 1},
        {"tokens": [other], "bot": "b1", "net_usd": 50, "gross_usd": 60, "priority_usd": 30, "l2_gas_usd": 9,
         "l1_fee_usd": 1, "reachable": False, "hooked": True, "prev_same_pool": 0},
        {"tokens": [other], "bot": "b2", "net_usd": 30, "gross_usd": 40, "priority_usd": 10, "l2_gas_usd": 9,
         "l1_fee_usd": 1, "reachable": True, "hooked": False, "prev_same_pool": 1},
        {"tokens": [ah.BASE_PAIR], "bot": "b3", "net_usd": 6, "gross_usd": 8, "priority_usd": 4, "l2_gas_usd": 1,
         "l1_fee_usd": 1, "reachable": True, "hooked": False, "prev_same_pool": 0},
        {"tokens": [other], "bot": "b4", "net_usd": 4, "gross_usd": 5, "priority_usd": 1, "l2_gas_usd": 1,
         "l1_fee_usd": 0, "reachable": True, "hooked": False, "prev_same_pool": 0},
    ]
    s = ah.summarize(arbs, hours=12, seed=seed)                    # 12 h of data -> x2 per day
    assert s["net_usd_day"] == 200
    assert s["split"]["seed"]["net_usd_day"] == 20 and s["split"]["other"]["net_usd_day"] == 168
    assert s["split"]["base_pair"]["net_usd_day"] == 12
    assert s["top3_overall"]["share"] == pytest.approx((60 + 30 + 6) / 100)
    top = s["top_tokens"][0]
    assert top["token"] == other and top["net_usd_day"] == 168 and top["top3"]["share"] == 1
    assert s["median_priority_pct_of_gross"] == pytest.approx(0.25)    # 0.1, 0.5, 0.25, 0.5, 0.2
    assert s["hooked_net_usd_day"] == 100 and s["reachable_other_net_usd_day"] == 68
    assert s["backrun_share"] == pytest.approx(0.4)


def test_backrun_context_reads_the_previous_tx():
    db = sqlite3.connect(":memory:")
    db.executescript(ah.SCHEMA)
    rows = [(5, 3, 1, "0x1", "v3", POOL_A, None, "1", "-1"), (5, 4, 2, "0x2", "v3", POOL_B, None, "1", "-1"),
            (5, 9, 3, "0x3", "v3", POOL_A, None, "1", "-1")]
    db.executemany("INSERT INTO swaps VALUES (?,?,?,?,?,?,?,?,?)", rows)
    assert ah.backrun_context(db, 5, 5, [POOL_B]) == (True, 1)
    assert ah.backrun_context(db, 5, 5, [POOL_A]) == (False, 2)
    assert ah.backrun_context(db, 5, 3, [POOL_A]) == (False, None)


def test_fetch_logs_shrinks_the_range_on_provider_limits():
    class FakeRpc:
        def __init__(self):
            self.ranges = []

        def call(self, method, params):
            f = params[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if b - a + 1 > 25:
                raise ah.RpcError("ranges over 10000 blocks are not supported on free plan")
            self.ranges.append((a, b))
            return [{"blockNumber": hex(a)}]

    rpc = FakeRpc()
    logs, span = ah.fetch_logs(rpc, {"topics": []}, 1000, 1099, 100)
    assert span == 25 and rpc.ranges == [(1000, 1024), (1025, 1049), (1050, 1074), (1075, 1099)]
    assert len(logs) == 4


def test_rpc_errors_never_show_the_url():
    url = "https://base.example.invalid/v2/SECRETKEY9f3a"
    rpc = ah.Rpc(url)
    with pytest.raises(ah.RpcError) as e:
        rpc._unwrap({"error": {"message": f"bad request to {url}"}}, "eth_getLogs")
    assert "SECRETKEY9f3a" not in str(e.value)


def test_v4_keys_are_checked_against_their_pool_id():
    key = (scanner.ZERO, scanner.USDC, 500, 10, scanner.ZERO)
    pid = scanner.v4_pool_id(key)
    assert ah.v4_meta_from_key(pid, *key)["token1"] == USDC
    assert ah.v4_meta_from_key(pid, scanner.ZERO, scanner.USDC, 3000, 60, scanner.ZERO) is None


# ---- the three validated mainnet arbs (receipts are permanent; read via the public RPC) -------------------------

KNOWN_ARBS = [
    # tx, bot contract, venues, hooked, USDC flow (raw), WETH flow (raw), backrun of the previous tx?
    ("0x4d7c0578233bfcf4215c8d087045ecf1556357387f86e06242a7215171e6247e",       # 4-hop CL3, PoolManager flash loan,
     "0x8d482bc23e1c92519537ee893ad0fd9a6f2498d2", ["cl3"], False, 1195679, None, True),     # profit to a 3rd address
    ("0x14590e4bdc3a1a6a57c24d35f08bc28bbdc5cad54b24f06d38b4af177a0d0c71",       # hookless V4 + CL2
     "0xcfcbfb02847df1bc3c0f5065ac11278dd50808d0", ["cl2", "univ4"], False, 247268, None, True),
    ("0x1a8d3da5dc2f0aa20ce3676460572605a27df55be4f73a8cc680e6ea7a23c2e8",       # the day's biggest: $1,300 on USDT
     "0xe8c36caccccdf7098ba9307875739bc82805d5b4", ["cl1", "univ3", "univ4"], False, 1300286387, None, True),
    ("0xed096868c14abcbc767ccc21041b17cedc0097996e93782488ddc70f470b7291",       # $226, not a backrun
     "0xcfcbfb02847df1bc3c0f5065ac11278dd50808d0", ["cl3", "univ4"], False, 226212183, None, False),
    ("0xd152c715aa15c0ce13a4dff6a52166c44a317b561c5260e101889166d7a0b4c2",       # $173 in WETH, kept by the EOA
     "0x25ae30c3b70addc337067ec861cad6a4d0959d35", ["cl2", "univ3"], False, None, 70982357320606609, True),
]


@pytest.mark.parametrize("tx,bot,venues,hooked,usdc,weth,backrun", KNOWN_ARBS)
def test_known_mainnet_arbs(tx, bot, venues, hooked, usdc, weth, backrun):
    rpc = ah.Rpc(os.getenv("BASE_RPC_URL", "https://mainnet.base.org"))
    r = ah.explain_tx(rpc, tx, eth_usd=ETH_USD)
    assert r["arb"], r["reason"]
    assert r["bot"] == bot and r["venues"] == venues and r["hooked"] == hooked
    assert (int(r["flows"][USDC]) if USDC in r["flows"] else None) == usdc
    assert (int(r["flows"][WETH]) if WETH in r["flows"] else None) == weth
    assert r["prev_same_pool"] == backrun
    assert r["net_usd"] < r["gross_usd"] and r["l2_gas_usd"] > 0 and r["priority_usd"] > 0


def test_real_arb_on_an_unresolvable_v4_pool_is_flagged_not_guessed():
    # A validated $4.59 arb (recipient's balance diff matched) through a hooked V4 pool the PositionManager doesn't
    # know. Guessing its currencies once booked a fake 2.9M-ETH "arb" elsewhere, so it must stay unvalued.
    rpc = ah.Rpc(os.getenv("BASE_RPC_URL", "https://mainnet.base.org"))
    r = ah.explain_tx(rpc, "0xc2dc4b5945e5206cda17ceba294415c4a247031e7d2377a2117a41461985d744", eth_usd=ETH_USD)
    assert not r["arb"] and r["reason"] == "unknown_pool"


def test_lp_rebalance_through_a_swapped_pool_is_not_an_arb():
    # Burn + Collect on pool A hands the owner tokens with nothing paid in: not swap profit (seen as $5k "arbs")
    rec = two_pool_arb()
    burn = {"address": POOL_A, "topics": [ah.topic("Burn(address,int24,int24,uint128,uint256,uint256)"), pad(BOT),
                                          "0x" + "0" * 64, "0x" + "0" * 64], "data": "0x" + word(1) * 3,
            "logIndex": hex(20)}
    rec["logs"] += [burn, transfer(WETH, POOL_A, BOT, 10**20, 21)]
    assert ah.classify(rec, META, ETH_USD, 0)["reason"] == "lp_action"
    # the same event from an unrelated contract doesn't matter
    rec["logs"] = two_pool_arb()["logs"] + [burn | {"address": "0x" + "99" * 20}]
    assert ah.classify(rec, META, ETH_USD, 0)["arb"]


def test_seed_bucket_ignores_runs_over_other_token_lists(tmp_path):
    seed_a, seed_b, seed_c, other = ("0x" + h * 20 for h in ("a1", "b2", "d4", "c3"))
    old, new = tmp_path / "20261019" / "results.jsonl", tmp_path / "20261020" / "results.jsonl"
    for f, rows in ((old, [(seed_a, "SAFE"), (seed_b, "UNSAFE"), (seed_c, "PROXY_WARNING")]),
                    (new, [(other, "SAFE"), (seed_b, "SAFE")])):   # newer run over another list, sharing one token
        f.parent.mkdir()
        f.write_text("\n".join(json.dumps({"token": t, "symbol": t[:6], "status": st}) for t, st in rows))
    os.utime(old, (1, 1))
    got = ah.safe_seed_tokens(str(tmp_path / "2*" / "results.jsonl"), seed_list=[seed_a, seed_b, seed_c])
    assert got == {seed_a: seed_a[:6], seed_c: seed_c[:6]}


def test_v4_init_locator_brackets_every_pool_with_shared_probes():
    import random
    rng = random.Random(7)
    lo, hi = ah.V4_DEPLOY_BLOCK - 1, 52_835_385
    born = {f"0x{i:064x}": rng.randrange(lo + 1, hi + 1) for i in range(300)}
    born["0x" + "ee" * 32] = hi                      # created in the very last block
    born["0x" + "ff" * 32] = lo + 1                  # created in the PoolManager's deployment block
    probes = []

    def is_init(block, ids):
        probes.append(block)
        return {p for p in ids if born[p] <= block}

    win = ah.locate_v4_inits(list(born), lo, hi, is_init, width=100)
    for pid, (a, b) in win.items():
        assert a < born[pid] <= b and b - a <= 100
    assert set(win) == set(born)
    assert len(probes) < len(born) * 20              # shared bisection, not a scan


def test_attempts_count_every_bot_tx_and_its_full_gas():
    bot, other_bot, eoa, user = "0x" + "b0" * 20, "0x" + "b1" * 20, "0x" + "e0" * 20, "0x" + "99" * 20

    def r(tx, to, frm, status, gas_used, price, l1, txi):
        return {"transactionHash": tx, "to": to, "from": frm, "status": hex(status), "gasUsed": hex(gas_used),
                "effectiveGasPrice": hex(price), "l1Fee": hex(l1), "blockNumber": hex(100), "transactionIndex": hex(txi)}
    receipts = [r("0xa1", bot, eoa, 1, 200_000, 10**7, 10**11, 0),            # bot contract
                r("0xa2", "0x" + "cc" * 20, eoa, 0, 50_000, 10**7, 0, 1),      # bot EOA calling something else
                r("0xa3", "0x" + "cc" * 20, user, 1, 50_000, 10**7, 0, 2),     # unrelated
                {**r("0xa4", None, user, 1, 1, 1, 0, 3), "to": None}]         # contract creation
    rows = ah.block_attempts(receipts, {bot, other_bot}, {eoa: bot}, eth_usd=2000.0)
    assert [x[0] for x in rows] == ["0xa1", "0xa2"]
    assert rows[0][3] == bot and rows[1][3] == bot and rows[1][5] == 0
    assert rows[0][6] == pytest.approx((200_000 * 10**7 + 10**11) / 1e18 * 2000)


def test_attempt_summary_scales_waste_and_subtracts_it():
    b1, b2 = "0x" + "b1" * 20, "0x" + "b2" * 20
    arbs = [{"tx": "0x1", "bot": b1, "net_usd": 10.0}, {"tx": "0x2", "bot": b1, "net_usd": 6.0},
            {"tx": "0x3", "bot": b2, "net_usd": 1.0}]
    attempts = [("0x1", 0, 0, b1, "e", 1, 0.5),         # an arb: its gas is already in its net
                ("0x9", 0, 1, b1, "e", 0, 0.2),         # revert
                ("0x8", 0, 2, b1, "e", 1, 0.3),         # no-op success
                ("0x7", 5, 0, b2, "f", 0, 0.1)]
    s = ah.attempt_summary(attempts, arbs, sampled_blocks=2, total_blocks=10, hours=12)
    r1 = next(r for r in s["bots"] if r["bot"] == b1)
    assert r1["arb_net_usd_day"] == pytest.approx(32.0)                    # 16 over 12 h
    assert r1["wasted_gas_usd_day"] == pytest.approx(0.5 * 5 * 2)           # (0.2 + 0.3) x 5 (1 in 5) x 2 (per day)
    assert r1["true_net_usd_day"] == pytest.approx(27.0)
    assert (r1["sample_reverted"], r1["sample_noop"], r1["attempts_per_arb"]) == (1, 1, 3.0)
    assert s["bots"][0]["bot"] == b1
    assert s["total"]["wasted_gas_usd_day"] == pytest.approx(5.0 + 0.1 * 5 * 2)   # no swaps given: both are bots


def test_v4_initialize_logs_skip_failing_windows_and_save_as_they_go(monkeypatch):
    """One window whose getLogs keeps failing must not lose every other window's keys (a 70-min run died that way at
    3,568/4,046): it's skipped and stays missing, and found keys reach on_found as they arrive."""
    good = scanner.v4_pool_id((scanner.ZERO, scanner.USDC, 500, 10, scanner.ZERO))
    bad = "0x" + "ab" * 32
    monkeypatch.setattr(ah, "multicall", lambda rpc, calls, *a, **k: [b""] * len(calls))   # PositionManager: unknown
    monkeypatch.setattr(ah, "locate_v4_inits", lambda ids, *a, **k: {good: (100, 200), bad: (300, 400)})

    def fake_logs(rpc, flt, a, b, span):
        if a > 300:
            raise ConnectionError("RPC kept failing")
        data = encode(["uint24", "int24", "address", "uint160", "int24"], [500, 10, scanner.ZERO, 2**96, 0])
        return [{"topics": [ah.T_V4_INIT, good, "0x" + "00" * 32, "0x" + "00" * 12 + scanner.USDC[2:].lower()],
                 "data": "0x" + data.hex()}], span
    monkeypatch.setattr(ah, "fetch_logs", fake_logs)
    saved = []
    found, missing = ah.pool_meta_v4(None, [good, bad], call_rpc=object(), head=500, log=lambda *a, **k: None,
                                     threads=1, on_found=saved.append)
    assert set(found) == {ah.L(good)} and found[ah.L(good)]["token1"] == scanner.USDC.lower()
    assert missing == [bad] and saved == [found]


def test_public_contracts_with_one_arb_are_not_counted_as_bots():
    """An EntryPoint or router that once carried an arb must not have its users' trades booked as failed arb attempts
    (that made the 2026-10-19 market look $3.3k/day net-negative)."""
    bot, router = "0x" + "b1" * 20, "0x" + "e1" * 20
    arbs = [{"tx": "0xa1", "bot": bot, "net_usd": 5.0}, {"tx": "0xa2", "bot": router, "net_usd": 1.0}]
    attempts = [("0xa1", 0, 0, bot, "e", 1, 0.1), ("0xl1", 0, 1, bot, "e", 1, 0.2),     # arb + a losing loop
                ("0xp1", 0, 2, bot, "e", 1, 0.3), ("0xr1", 0, 3, bot, "e", 0, 0.4),     # a plain trade + a revert
                ("0xa2", 1, 0, router, "u", 1, 0.1)] + [
                (f"0xu{i}", 1, i + 1, router, f"u{i}", 1, 1.0) for i in range(3)]       # users' plain trades
    s = ah.attempt_summary(attempts, arbs, sampled_blocks=1, total_blocks=1, hours=24,
                           swapped={"0xa1", "0xl1", "0xp1", "0xa2", "0xu0", "0xu1", "0xu2"}, loops={"0xa1", "0xl1", "0xa2"})
    kinds = {r["bot"]: r["kind"] for r in s["bots"]}
    assert kinds == {bot: "bot", router: "mixed"}                       # bot: 1 arb + 1 loop >= 1 plain trade
    assert s["total"]["contracts"] == 1 and s["total"]["wasted_gas_usd_day"] == pytest.approx(0.9)
    assert s["total"]["true_net_usd_day"] == pytest.approx(4.1)
    assert s["mixed"]["arb_net_usd_day"] == pytest.approx(1.0) and s["mixed"]["wasted_gas_usd_day"] == pytest.approx(3.0)


def test_v4_existence_probe_reads_the_pool_managers_own_storage():
    """extsload(keccak(id, 6)) must give the same sqrtPrice as StateView: the locator's pool-exists probe depends on it."""
    rpc = ah.Rpc(os.getenv("BASE_RPC_URL", "https://mainnet.base.org"))
    weth_usdc = scanner.v4_pool_id((scanner.ZERO, scanner.USDC, 500, 10, scanner.ZERO))     # native ETH/USDC 0.05%, hookless
    raw = ah.multicall(rpc, [(ah.POOL_MANAGER, "0x" + ah.SEL_EXTSLOAD + ah.v4_state_slot(weth_usdc).hex()),
                             (scanner.UNI_V4_STATE_VIEW, "0x" + scanner.selector("getSlot0(bytes32)") + weth_usdc[2:])])
    sqrtp = int.from_bytes(raw[0], "big") & ((1 << 160) - 1)
    assert sqrtp > 0 and sqrtp == int.from_bytes(raw[1][:32], "big")
    assert ah.v4_initialized(rpc, [weth_usdc], ah.V4_DEPLOY_BLOCK - 1) == set()   # before the PoolManager existed
    assert ah.v4_initialized(rpc, [weth_usdc, "0x" + "00" * 31 + "01"], int(rpc.call("eth_blockNumber", []), 16)) \
        == {weth_usdc}


def test_v4_init_locator_resumes_from_saved_windows():
    born = {f"0x{i:064x}": 1_000 + i * 7_919 for i in range(50)}
    is_init = lambda block, ids: {p for p in ids if born[p] <= block}
    saved = []
    ah.locate_v4_inits(list(born), 0, 1_000_000, is_init, on_round=saved.append)
    calls = []
    resumed = ah.locate_v4_inits(list(born), 0, 1_000_000, lambda b, ids: calls.append(b) or is_init(b, ids),
                                 windows=saved[3])
    assert all(a < born[p] <= b and b - a <= 100 for p, (a, b) in resumed.items())
    assert len(calls) < sum(len({ab for ab in w.values()}) for w in saved)   # skipped the rounds already done
