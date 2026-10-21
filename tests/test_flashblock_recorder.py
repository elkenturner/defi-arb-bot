"""flashblock_recorder: tx-range bookkeeping, gap marking and the arb timing join (no network)."""
import sqlite3

import flashblock_recorder as fr


def msg(block, idx, n):
    return {"metadata": {"block_number": block}, "index": idx, "diff": {"transactions": ["0x"] * n}}


def fb_db(*msgs):
    db = fr.open_db(":memory:")
    rec = fr.Recorder(db)
    for i, m in enumerate(msgs):
        rec.add(m, float(i))
    return db


def test_tx_ranges_accumulate_per_block():
    db = fb_db(msg(100, 0, 1), msg(100, 1, 50), msg(100, 2, 10), msg(101, 0, 1), msg(101, 1, 5))
    assert fr.flashblock_of(db, 100, 0) == 0
    assert fr.flashblock_of(db, 100, 1) == 1 and fr.flashblock_of(db, 100, 50) == 1
    assert fr.flashblock_of(db, 100, 51) == 2 and fr.flashblock_of(db, 100, 60) == 2
    assert fr.flashblock_of(db, 100, 61) is None            # past the last recorded tx
    assert fr.flashblock_of(db, 101, 1) == 1                # new block restarts at tx 0


def test_missing_flashblock_or_mid_block_start_makes_block_untrusted():
    db = fb_db(msg(100, 3, 4), msg(100, 4, 4),               # joined mid-block
               msg(101, 0, 1), msg(101, 1, 3), msg(101, 3, 2),  # index 2 never arrived
               msg(102, 0, 1), msg(102, 1, 2),
               msg(104, 0, 1))                               # block 103 skipped
    assert fr.flashblock_of(db, 100, 0) is None
    assert fr.flashblock_of(db, 101, 1) is None
    assert fr.flashblock_of(db, 102, 1) == 1
    assert {b for (b,) in db.execute("SELECT block FROM gaps")} == {100, 101, 103}


def test_timing_places_arbs_relative_to_their_trigger():
    fb = fb_db(msg(100, 0, 1), msg(100, 1, 10), msg(100, 2, 10), msg(100, 3, 10))
    arbs = sqlite3.connect(":memory:")
    arbs.execute("CREATE TABLE arbs (tx, block, txi, trigger_gap, net_usd, bot, tokens)")
    arbs.executemany("INSERT INTO arbs VALUES (?,?,?,?,?,?,?)", [
        ("a", 100, 5, 1, 10.0, "b1", '["X"]'),       # trigger tx 4, same flashblock 1
        ("b", 100, 15, 8, 3.0, "b2", '["X", "Y"]'),  # trigger tx 7 in fb 1, arb in fb 2
        ("c", 100, 25, 20, 2.0, "b2", '["Y"]'),      # trigger tx 5 in fb 1, arb in fb 3
        ("d", 100, 3, None, 1.0, "b3", '["Y"]'),     # gap was open when the block started
        ("e", 999, 3, 1, 50.0, "b4", '["X"]'),       # block not recorded
    ])
    t = fr.timing(fb, arbs)
    assert t["by_delta"] == {0: [1, 10.0], 1: [1, 3.0], 2: [1, 2.0], "block start": [1, 1.0]}
    assert t["unplaced"] == 1
    toks = fr.by_token(t["rows"])
    assert toks["X"]["same fb"] == [1, 10.0] and toks["X"]["+1 fb"] == [1, 3.0]
    assert toks["Y"]["+1 fb"] == [1, 3.0] and toks["Y"]["+2+ fb"] == [1, 2.0] and toks["Y"]["block start"] == [1, 1.0]


def test_arrival_gaps_skip_untrusted_blocks_and_block_boundaries():
    db = fr.open_db(":memory:")
    rows = [(100, 0, 0.0), (100, 1, 0.5), (100, 2, 0.7), (100, 3, 0.95), (101, 0, 1.0), (101, 1, 1.4), (101, 2, 1.6)]
    db.executemany("INSERT INTO fb VALUES (?, ?, 0, 1, ?)", rows)
    db.execute("INSERT INTO gaps VALUES (101, 'reconnect')")
    assert [round(g, 2) for g in fr.arrival_gaps(db)] == [0.2, 0.25]   # fb 0 (deposits) spacing ignored
