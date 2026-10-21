# DeFi Arb Bot: Project Handoff (for planning with Claude Cowork)

_Written 2026-10-16 by Claude Code at the end of the build sessions. Project folder: `~/defi-arb-bot`.
Everything here was checked against the code, tests and logs in that folder. `CLAUDE.md` in the same folder is the
detailed technical "project brain" that Claude Code reads automatically. This file is the story plus the plan._

---

## 0. How to use this document (for Claude Cowork)

Ken (the owner) wants your help to **plan the next steps** and **write prompts he can paste into Claude Code**, which
does the actual building in `~/defi-arb-bot`. You can open the folder directly. The most useful files are `HANDOFF.md`
(this), `CLAUDE.md` (rules and technical detail), `scanner.py`, `pipeline.py`, `tests/`, and `logs/*/results.jsonl`
(real scan results).

Ken prefers **plain language with everyday analogies before jargon**. He's an entrepreneur, not a full-time
Solidity engineer. When you write prompts for Claude Code, use the style in §10.

---

## 1. The project in plain language

**The idea.** The same token often trades at slightly different prices in different "shops" (liquidity pools) on Base,
a cheap Ethereum Layer-2 network. If you buy where it's cheap and sell where it's dear, in one instant transaction, you
keep the difference, *if* it's bigger than all the costs: shop fees, the price moving against you as you trade
("price impact"), and network fees (gas).

**The bigger vision.** An autonomous, self-improving bot. The self-learning (reinforcement-learning) part is
**deliberately postponed** until a simple rule works: "trade only if net profit after ALL costs > $X".

**What exists today.** The bot's eyes and immune system, but no hands yet:
- **Eyes (`scanner.py`)**: for any token, it finds every pool on Uniswap V3, Aerodrome classic, and Aerodrome
  Slipstream (three generations), paired with USDC or WETH. It prices every possible round-trip route exactly as the
  pools would fill it, then subtracts every cost.
- **Immune system (also `scanner.py`)**: before trusting a token it checks for scams. These include honeypots (you can
  buy but never sell), hidden taxes, owners who can pause, blacklist or re-tax trading later, and contracts whose code
  can be swapped. It does this by actually test-trading on a private copy of the blockchain (a "fork").
- **Conveyor belt (`pipeline.py`)**: feeds 30 verified Base tokens through the eyes and immune system on repeat,
  remembers proven scams in `blacklist.json`, stays quiet when nothing is profitable, and sounds a loud alarm only when
  a route is profitable **and** that profit is confirmed by re-running the trades on the private copy.
- **Hands (an on-chain contract that actually trades)**: **not built yet, on purpose.** Nothing has ever been sent to the
  real network. No private keys exist in the project.

**Where it stands.** Two full scans of all 30 tokens found **zero profitable routes**. The closest were wstETH
(−$0.10 on a $500 trade) and cbBTC (−$0.12). That's normal for big, busy tokens: other bots close gaps within
seconds. The machinery works. Whether there's money in it is still unproven.

---

## Outcome (2026-10-21): atomic DEX-to-DEX arbitrage on Base is closed

**The question.** Can a bot earn money after all costs by buying a token in one Base pool and selling it in another,
in one atomic transaction?

**What was measured, and the key number from each** (all read-only or on a local fork; nothing was ever sent):
| Measurement | Key number |
|---|---|
| Scanner, 30 seed tokens, two full scans (2026-10-16) | 0 profitable routes; best wstETH −$0.10, cbBTC −$0.12 per $500 |
| Gap monitor, every block for 2 h (2026-10-17) | 7 of ~1.5M route-blocks above $0 (best +$0.01), none above $1. A 2 h watch after a sharp ETH move (2026-10-19): best $0.05 |
| Size search, $100–$5k | the best size was always the smallest ($100): routes lose to fees, not to gas |
| Uniswap V4 hookless pools | only cbBTC gains pools above the $50k floor; best $100 net −$0.02 |
| Arb history, what real bots earned (3 days, 2026-10-17..19) | $4.0–5.0k/day for all bots (248–312 contracts); median arb ≈ $0.00; the 13 safe seed tokens $171–2,929/day (one cbBTC event) |
| Unknown V4 pools (4,298) | the rough "≤ $14k/day" turned out to be $30–36/day |
| Failed attempts (1 in 5 blocks, day 2) | ~10 txs per arb; arb-only bots net $2.7k/day after $1.2k/day of wasted gas |
| Flashblock timing (3.1 h, 3,073 arbs) | 98% of profit ($2,478 incl. one $2,039 ZORA arb) landed in the same flashblock as the swap that opened the gap; one flashblock later: $37 (avg $0.02/arb); open at block start: $20 |

**Verdict: stop.** The profit goes to transactions that were already queued when the gap opened, not to bots that see
the gap and react. The leader (`0xe8c3…`) nets **~$1.37k/day**, the top 3 together $2.04k of the $2.73k/day true net,
the next 7 another $0.82k, and **everyone below the top 10 loses money collectively (−$137/day; 83 of 162 bot contracts
are net negative)**. A new bot reacting within one flashblock on the least concentrated tokens was estimated at
−$15 to +$60/day, most likely about zero, before server and RPC costs.

**What would have to change to revisit it:**
- **Ordering rules.** A Base backrun auction or bundle service that places a paying tx right after a given trigger
  would make reacting as good as being pre-queued.
- **Cheaper failure.** If failed or reverted txs stopped costing gas (sequencer revert protection), or gas fell well
  below ~$0.013 per attempt, pre-queuing/spamming becomes affordable for a newcomer.
- **New venues with real volume** that the leaders don't cover yet (PancakeSwap V3, Balancer, Curve are not even
  measured here).
- **A different regime:** sustained dislocations (depegs, hacks, a new chain-wide token launch), or the leader leaving.
- **How to check:** re-run `arb_history.py` (3+ days) and `flashblock_recorder.py --timing`. Revisit if reacting arbs
  (one or more flashblocks after the trigger) on tokens that pass the safety gate exceed ~$200/day for a week.

## 2. Strategy and ground rules (agreed during the build)

1. **Rules first, learning later.** The only decision rule: *trade if net profit after all costs > X* (default $1).
   The RL / "agentic self-improving" loop waits until the rule-based version is trusted. This matches an earlier
   review of a trading-bot idea (Sept 2026): start with simple rules, test on data, paper-trade first.
2. **Simulate on a local fork before writing any execution logic.** This is a hard rule in `CLAUDE.md`. Foundry's
   `anvil` makes a private copy of Base; all "test trades" happen there and are rolled back.
3. **Atomic, same-block trading is the target design.** The eventual trade is one transaction that does every swap or
   reverts. That's why minting tokens and admin upgrades by trusted issuers are treated as warnings, not blockers:
   they'd have to land in our exact block, ahead of us, to hurt us. (Ken's own argument, accepted.)
4. **No private keys in code or git, and no real-money transaction without Ken's explicit go-ahead for that specific step.**
5. **Profit is always net**: swap fees + price impact (both baked into real pool quotes) + L2 gas + L1 data fee.
   A raw price gap is never treated as profit.

---

## 3. What's in the folder

| File | What it is | Size |
|---|---|---|
| `CLAUDE.md` | Project brain: rules, safety model, addresses, commands. Claude Code loads it automatically | 276 lines |
| `scanner.py` | Pool discovery, quoting, route math, gas model, the whole safety gate. CLI for one token | 1,216 lines |
| `pipeline.py` | Multi-token coordinator: 30-token seed list, worker processes, blacklist, alarm output | 566 lines |
| `tests/` | 64 regression tests (pytest) + fake scam tokens in Solidity (`Trap.sol`, `ProxyTrap.sol`) | ~870 lines |
| `logs/<run>/results.jsonl` | One line per token per scan; per-token detail logs beside it | 2 runs so far |
| `blacklist.json` | Proven-bad tokens (5 entries now) | local cache, gitignored |
| `requirements.txt` | web3, python-dotenv, pytest | |

**Version control:** local git repo since 2026-10-19 (no remote). `business/` is untracked (not part of the bot).

**Tech stack:** Python 3.9 + web3.py; Foundry (`anvil` for forks, `forge` to compile test contracts) installed at
`~/.foundry/bin`; public RPC `https://mainnet.base.org` (rate-limited; a private RPC is recommended before scaling).

### Commands
```bash
cd ~/defi-arb-bot && source .venv/bin/activate

python scanner.py                                   # WETH on live Base, read-only
anvil --fork-url https://mainnet.base.org           # terminal 1: private copy of Base
RPC_URL=http://127.0.0.1:8545 python scanner.py --token <address>   # terminal 2: full safety check + routes

python pipeline.py --workers 2                      # one cycle over 30 tokens, ~15 min
python pipeline.py --workers 2 --loop --interval 600   # keep scanning; Ctrl-C to stop
python pipeline.py --recheck                        # ignore blacklist.json for this run

pytest -v tests/                                    # 64 tests, ~12 min (starts its own fork on port 8546)
```
Useful flags: `--amount 500` (trade size, USD), `--min-profit 1.0` (the X), `--min-liquidity 50000` (per-pool floor),
`--max-tax 0.5` (percent), `--settle native|usdc`.

---

## 4. How it works (one level deeper)

### Venues and routes
- **Venues:** Uniswap V3 (4 fee tiers), Aerodrome classic v2 (volatile/stable), Aerodrome **Slipstream** (concentrated
  liquidity). Slipstream has **three live factory generations** (CL1/CL2/CL3), confirmed by Aerodrome's on-chain
  registry. Each pool must be quoted through its *own* generation's quoter: the wrong one silently prices a different
  pool ($2,060 vs $2,429 for 1 WETH in testing).
- **Bases:** every token's pools are found against **USDC and WETH**. Most small Base tokens trade against WETH.
- **Routes:** for every ordered pair of qualifying pools: 2-swap loops (USDC→T→USDC, WETH→T→WETH) and 3-swap
  "triangles" through a trusted WETH/USDC anchor pool. `--settle usdc` forces start/end in USDC (up to 4 swaps).
- **Sizing:** `--amount` is in USD and is converted to WETH at the anchor's current mid price when a loop starts in WETH.
- **Gas:** 60k + 120k gas per swap, priced at the block's base fee + 0.001 gwei tip, plus the L1 data fee. About
  $0.004–0.008 per trade today, so gas is almost never the problem. Fees and price impact are.
- **Quote correctness guards:** quotes capped at 10M gas (a failure means "no quote", never a crash); partial-fill
  quotes are rejected; Slipstream quotes are priced as of the *next* block, because its fees spike in the block right
  after a trade.

### Safety gate (`is_safe_token`): layers, in order
1. **Per-pool liquidity floor ($50k).** Each pool must clear it on its own, valued as 2 × its smaller side.
2. **Bytecode scan:** looks for risky functions (pause, blacklist, mint, tax setters, trading limits, upgrade).
3. **Controllers:** who can still act (`owner()`, `admin()`, proxy admin slots).
4. **Upgradeable proxy check:** blocked (`DYNAMIC_UNSAFE`) unless on the **Trusted Proxy Allowlist** (below).
5. **Fork test-trade on EVERY pool:** buy → transfer to a second wallet → sell. A revert = honeypot; a shortfall =
   measured tax (>0.5% fails). Only a *real on-chain revert* counts as a honeypot. Network errors become "ERROR, retry".
6. **Owner probes:** on the fork, impersonate the owner, switch on each risky function with extreme values
   (99% fee, pause, blacklist us…), and re-run the test trade. If trading breaks → `DYNAMIC_UNSAFE`.

**Verdicts:** `SAFE` · `PROXY_WARNING` (passes; upgradeable by a trusted issuer) · `UNSAFE` (a failed check, **or no
pool at all above the liquidity floor**: reason code `LIQUIDITY`, not cached) · `DYNAMIC_UNSAFE` (an owner could
break it later) · `THIN` (passed safety, but only 1 pool above the floor).

**Blacklist rule:** only proven token-level problems are cached forever: `HONEYPOT`, `TAX`, `PROXY`
(unlisted), `OWNER_POWER`. Thin liquidity, network errors, test-setup failures and `PROXY_PIN_MISMATCH` are re-checked
every cycle.

### Trusted Proxy Allowlist (`TRUSTED_PROXIES` in scanner.py)
Five issuers' upgradeable tokens pass with `PROXY_WARNING`. Each entry pins the **token address + its whole chain of
control** (admins, who owns the admins, every multisig signer, threshold, modules, guard, timelock delays), read
on-chain at Base block 52,689,443 and cross-checked with the issuers' docs by two independent researchers. Any change
to that chain means blocked again until a human re-pins. (Exception: weETH's 4-of-7 Safe holds the timelock's roles and
is not pinned. See §7.)

| Token | Issuer | Who can upgrade | Attribution confidence |
|---|---|---|---|
| cbBTC | Coinbase | 2 single-key wallets (same as Coinbase's L1 cbBTC). Instant | high |
| cbETH | Coinbase | 3-of-6 multisig, no timelock | medium |
| wstETH | Lido | Lido DAO vote via governance bridge | high |
| weETH | ether.fi | 3-day timelock + 4-of-7 multisig | medium |
| MORPHO | Morpho | 5-of-9 multisig, no timelock | high |

### Pipeline (`pipeline.py`)
- **30-token seed list**, every address verified on-chain (copycat scam tokens reuse these symbols, so match by
  address only): cbBTC, AERO, VIRTUAL, VVV, cbETH, wstETH, DRV, MORPHO, ZRO, ZEN, AAVE, ZORA, FLOCK, CLANKER, BNKR,
  BRETT, DEGEN, TOSHI, KEYCAT, cbXRP, LINK, doginme, weETH, KTA, TIG, WELL, SKI, MIGGLES, HIGHER, MOCHI.
- **One private fork per worker, never shared.** Two safety checks on one fork would undo each other. Each fork is
  reset to the latest block before each token.
- **Output:** silent for losers; a red banner + terminal bell only for a route that is profitable **and** re-confirmed
  by real swaps on the fork. Everything goes to `logs/<run>/results.jsonl`.

---

## 5. How the build went: decision log

Each phase was one prompt from Ken. Claude Code built it, then ran independent "adversarial review" agents that tried
to break it, and fixed what they found. Notable corrections are listed, because they show where prompts were imprecise.

| # | Ken asked for | What got built / decided | Corrections & lessons |
|---|---|---|---|
| 1 | `cloud.md` brain + simple price-spread scanner | `CLAUDE.md` + spread scanner (WETH/USDC) | File must be named **`CLAUDE.md`**, or Claude Code won't auto-load it |
| 2 | Quote-based net-profit calculator, `is_safe_token` stub | Real quoter calls, gas incl. L1 fee, liquidity floor | Quotes already include swap fees, so subtracting 0.3% again would double-count |
| 3 | Per-pool liquidity + anvil buy/transfer/sell test | Fork simulation with snapshot/rollback; Foundry installed | Gas on anvil was 100× too high (default tip). Fixed |
| 4 | Bytecode scan + owner impersonation probes | Selector scan, controller discovery, probes, first test suite | `0x84563224` is not `renounceOwnership` (it's `0x715018a6`). cbBTC first slipped through (old proxy layout) |
| 5 | USDC + WETH bases, triangular routes, gas per swap | Multi-base routing, gas model, `--settle` | Review found the sim only tested the deepest pool; fixed to test **every** pool |
| 6 | Aerodrome Slipstream | 3 factory generations, per-generation quoters | Partial-fill quotes, next-block dynamic fees, test funding moved prices; all fixed |
| 7 | Pipeline over top-30 tokens + blacklist + alarms | `pipeline.py`, researched seed list, workers | 11 review findings, incl. **RPC timeout falsely labelled honeypot → permanent blacklist**, worker silently using someone else's fork, banner for unconfirmed hits |
| 8 | Proxy allowlist for 5 issuers | Pinned controller-chain fingerprints, `PROXY_WARNING` | Pin = token + whole control chain (an admin address alone can be copied). Fingerprint reads fail closed |

**Ken's decisions on record:** mint = warning only; trusted-issuer proxies = warning; blacklist deleted and rebuilt
after the allowlist.

---

## 6. Results so far

| Scan | Safe | Proxy-warning | Thin | Unsafe | Hits | Best routes ($500) |
|---|---|---|---|---|---|---|
| 2026-10-16 00:52 (before allowlist) | 9 | – | 7 | 14 | 0 | VIRTUAL −$0.34, AERO −$0.86 |
| 2026-10-16 10:00 (after allowlist) | 8 | 5 | 8 | 9 | 0 | **wstETH −$0.10, cbBTC −$0.12**, VIRTUAL −$0.39, weETH −$0.58, AERO −$0.62 |

"Unsafe" includes 4 tokens (KEYCAT, SKI, MIGGLES, HIGHER) with **no pool above the $50k floor**. Those are dead
markets, not scams. The real token-level failures are the 10 (run 1) / 5 (run 2) `DYNAMIC_UNSAFE` tokens.

Blacklisted now: DEGEN (owner can pause trading), DRV, FLOCK, WELL, cbXRP (upgradeable, not allowlisted;
cbXRP is Coinbase's and could be pinned).

**Gap monitor, 2026-10-17 (2 hours, blocks 52742252–52745852, public RPC, read-only):** 13 tokens, 424 two-pool routes,
3,593 of 3,601 blocks watched (8 missed). Each block, routes were quoted most-promising-first; no route whose gross spread
beat its fees went unquoted. Result: **7 route-blocks out of ~1.5 million made more than $0, best +$0.01; zero made more
than $1.** No profitable stretch lasted more than 2 blocks. Best gross-minus-fees gap ever seen: +3 bps (VIRTUAL).

**Honest read:** on busy tokens, prices across venues are kept within fees by professional bots. The scanner shows
best-route losses from about 10¢ (wstETH) to about $18 (doginme) per $500. Most tokens land between $0.10 and $4. Real opportunities, if any, are more likely during volatile moments, on
venues not yet covered (Uniswap V4), at different trade sizes, or on newer/smaller tokens. None of that is proven.

---

**Session 2026-10-19 (git, size search, Uniswap V4, private-RPC wiring):**
- **Uniswap V4 (hookless pools)** is now a fourth venue. Only **cbBTC** gains pools above the $50k floor (5 new: $2.7M
  USDC 0.05%, $986k native-ETH 0.05%, …; 21 qualifying pools now). Every other seed token's hookless V4 pools are tiny.
  ZORA/Clanker/memecoin V4 liquidity sits in **hooked** pools, which are out of scope, so V4 did not rescue ZORA.
- **Size search, full cycle (2 workers):** no hits. For every route, the best size was **$100, the smallest tried**:
  losses scale with size, i.e. the routes lose to fees, not to gas or impact. Same picture as before, from a new angle.
  Best $100 nets: wstETH −$0.01, cbBTC / VIRTUAL −$0.02.
- **Found and fixed:** every default anvil test wallet has an **EIP-7702 delegation on Base** (someone attached code to
  the public test keys). The safety sim now strips it on the fork first (`prepare_test_wallet`).
- **RPC cost of one 30-token cycle** (`rpc_bench.py`, public RPC, size search on):
  | workers | time | upstream calls | 429 rate-limited | 401 |
  |---|---|---|---|---|
  | 2 | 27.5 min | 21,553 | 586 | 316 |
  | 4 | 19.6 min | 23,001 | 1,812 | 338 |
  ~72% of calls are `eth_getStorageAt` (anvil copying state). Roughly **22k calls per cycle**: every 15 min 24/7 that's
  ~2.1M calls/day, which needs a paid plan. One cycle per hour is ~530k/day. More workers buy speed, not fewer calls, and on
  the public RPC 4 workers get throttled 3× as often.
- Between the two benchmark runs (30 min apart), cbETH's main Slipstream pool fell from $216k to $23k (an LP left), so
  cbETH went THIN. Pool depth on long-tail venues really does change by the hour.

**Arb market history, 2026-10-19 (`arb_history.py`, last 24 h = blocks 52,792,186–52,835,385, read-only).**
The question changed from "is there a gap at block end?" to "what did real arb bots actually earn?", because Base arbs
land inside blocks. 1.65M swaps → 138,741 txs on 2+ pools → 41,849 loop-shaped → **24,052 atomic arbs by 248 bot contracts**.
| | per day |
|---|---|
| Net profit after gas, all bots | **$4,855** (gross $6,592) |
| our 13 safe seed tokens | $171 |
| WETH/USDC only | $323 |
| everything else | $4,360 (on venues we already scan, no hooks: $3,985) |
| hooked V4 pools (measurable ones) | $152 |
- **Lumpy:** only 314 arbs cleared $1 net, 58 cleared $10 and they made 73% of the day. One USDT dislocation
  (a single-block backrun, not a lasting gap) was **$1,985, 41% of the day**; the single biggest arb was $1,300 (bot `0xe8c3…`, USDT/USDC across
  CL1/V3/V4). The typical arb earns cents: median net ≈ $0.00, median gas = 82% of gross.
- **Competition:** top 3 bots take 43% overall, but **per token the top 3 take 90–100%** for most of the top 30
  (exceptions: XDP 33%, STONKEX 40%, CTR 46%, cbBTC 46%, SYND 48%). Median priority fee = 18.5% of gross profit; 16% of
  arbs are direct backruns of the previous tx.
- **Not counted at first:** 7,232 loop-shaped txs on 1,885 V4 pools with unknown keys and a rough "≤ $14k/day" estimate.
  Resolved on 2026-10-20 (below): that turned out to be ~$30/day. PancakeSwap/Balancer/Curve loops aren't pulled.
- Errors caught on the way, now regression-tested: profit paid to third addresses (missed by an actor-balance view),
  guessed V4 keys (fake 2.9M-ETH arb), LP Burn+Collect rebalances posing as $5k arbs.
- Validated: 5 arbs (incl. the day's 3 largest) match their recipients' on-chain balance change exactly.

**Blind spots closed, 2026-10-20 (3 days: blocks 52,705,786–52,835,385, plus 6 h of live Flashblocks).**
- **Unknown V4 pools:** all 4,298 PoolIds resolved from the PoolManager's own `Initialize` events (bisecting each pool's
  creation block by reading PoolManager storage at past blocks, then one 100-block log query). The rough ≤ $14k/day
  (day 2) / $12k/day (prior 48 h) gross became **$30 and $36/day net, all on hooked pools, hookless $0**: 2/3 of those
  txs weren't loops once their tokens were known (plain trades), most of the rest were LP actions. Totals after the
  re-judge: $4,885/day (day 2), $4,536/day (prior 48 h).
- **Failed attempts (1 in 5 blocks of day 2):** 159 arb-bot contracts sent ~10 txs per arb: 2,027 arbs, 9,745 reverts,
  686 losing loops, 8.5k early-return no-ops in the sample. **$3,930/day of arbs − $1,238/day of wasted gas = $2,692/day
  true net** (≈ $0.013 per failed tx). A first pass said −$3.3k/day: it counted ERC-4337 EntryPoints and SwapRouter02
  (which once carried an arb) as bots and their users' trades as waste. Fixed and tested.
- **React or pre-queued (Flashblocks):** of arbs placed in their flashblock (3,073, over 3.1 h of trusted blocks), the
  profit sits in the **same flashblock as the swap that opened the gap: $2,478 (98%)**; without one $2,039 ZORA arb
  (also same flashblock, 3 txs behind its trigger) $439, avg $0.42/arb.
  Arbs one flashblock later: $37 (avg $0.02); two or more later: $1; gap already open at block start: $20. For XDP,
  STONKEX, CTR, fBOMB, SYND and DRV together: same flashblock $72, later $9, block start $1.
- **Verdict on a new "react within 1 flashblock" bot** on the tokens where the top 3 take < 50% (XDP, STONKEX, fBOMB,
  CTR, cbBTC, DRV, SYND, AERO, TEVA, TIBBIR; $200–550/day for all bots together): the reachable part is about $5–60/day
  before its own failed attempts, which can cost as much as it earns. **Roughly −$15 to +$60/day, most likely near zero**,
  before any server or RPC cost. Not worth building as specified. Details in the session answer.

## 7. Known gaps and limitations

- **Uniswap V4: hookless pools at the 4 standard fee/tick pairs only.** Hooked pools (most V4 memecoin/Zora/Clanker
  liquidity) and custom-fee pools aren't scanned. Uniswap V2 isn't covered either (KEYCAT's main pool).
- **Size search never goes below $100 or above $5,000.**
- **Only scans when run.** No history of "near misses" or of how spreads behave over time yet.
- **Public RPC** throttles. Keep 1–2 workers; more needs a private RPC (`BASE_RPC_URL`).
- **Safety blind spots** (documented in `CLAUDE.md`): risky functions with unusual names; role-based admins
  (AccessControl, `minter()`); timelock roles for weETH; wallet-size/cooldown rules not tested.
- **No execution contract, flash loan, or private transaction submission.** By design, not built yet.
- **Speed:** a 30-token cycle takes ~15 min; the test suite ~12 min.

---

## 8. Open questions for Ken (good planning topics)

0. **Atomic DEX-to-DEX arbitrage is closed** (see "Outcome" near the top). Next question under study: the
   exchange-vs-Base market (Coinbase price vs Base pools), measured read-only.
1. **Is there money here at all?** Decide what evidence would justify building the trading contract: e.g. "a
   fork-confirmed hit > $1 at least N times a week over two weeks of continuous scanning".
2. **Which direction first?** (a) watch longer and gather statistics, (b) widen coverage (Uniswap V4, more/newer
   tokens), (c) smarter sizing, or (d) start the execution contract on the fork.
3. **Budget:** a private RPC. Free tiers may cover occasional cycles, but continuous scanning with forks is RPC-heavy and
   billed per request or compute unit. Measure usage before choosing a plan: roughly $0–50/month for light use, more for
   24/7 multi-worker scanning. Plus Claude usage (adversarial reviews are token-heavy).
4. **Risk appetite:** how much capital, and whether to ever leave fork-only mode. Any real-money step needs Ken's
   explicit go-ahead.
5. Add **cbXRP** to the allowlist? Re-pin cadence for the 5 issuers?

---

## 9. Recommended next steps, with ready-to-paste prompts for Claude Code

Ordered by value / risk. Each prompt is self-contained. Paste one at a time into Claude Code inside `~/defi-arb-bot`.

### Step 1: DONE (2026-10-19): git repo, first commit, no remote
```
Initialize a git repository in ~/defi-arb-bot. Before the first commit, scan every file for private keys, API
keys or RPC URLs with keys in them, and confirm .gitignore excludes .venv, logs/, blacklist*.json, .env and
tests/contracts/out. Make the first commit. Do not create any remote or push anywhere.
```

### Step 2: DONE (2026-10-17): block-by-block gap monitor (`gap_monitor.py`)
Built and run instead of the per-cycle statistics report: the pipeline samples each token once every ~15 min, which is far
too coarse for gaps that last a block or two. See §6 for the result. To repeat it (e.g. on a private RPC, or at a volatile
time): `python gap_monitor.py --minutes 120`, then `python gap_monitor.py --report`.

### Step 3: DONE (2026-10-19): `--size-search` (result in §6)
```
Add an optional size search, wired through both scanner.py and pipeline.py (--size-search flag in both). For the top 3 routes of each token, evaluate net profit at $100, $250, $500, $1,000, $2,500 and $5,000, then refine around the best with a few more points. Record the best size and its net in results.jsonl. If the best-size net clears --min-profit, run confirm_route_on_fork at that size, and only a fork-confirmed result gets the banner. Default behaviour without the flag stays unchanged. Add regression tests, run pytest -v tests/, then run one pipeline cycle with --size-search and compare it with logs/20261016-100048/results.jsonl.
```

### Step 4: DONE (2026-10-19): Uniswap V4 hookless pools (see CLAUDE.md; result in §6)
```
Research and add Uniswap V4 as a fourth pool kind (`univ4`) in scanner.py. Scope: hookless pools only (hooks == address(0)). Hooked pools are SKIPPED at discovery and logged as 'skipped: hooked'. They are not a safety failure, never fail the token and never cause a blacklist entry. Discover pools by computing PoolIds for the standard fee/tickSpacing combos against both WETH and native ETH (treat native ETH as the WETH base and say how you handle wrap/unwrap) and reading StateView, not by scanning logs. Quote with the V4 Quoter in next-block context like Slipstream, with the same QUOTE_GAS cap and partial-fill guard. Swap on the fork through the Universal Router plus Permit2 (or a minimal unlockCallback test contract). Extend simulate_all_pools, the owner probes and confirm_route_on_fork so V4 pools get the same buy/transfer/sell checks. Verify every address on-chain against Uniswap's official deployment list (two independent checks, never hand-typed), add the kind to the 'Authorized pool types' table in CLAUDE.md, add tests (quote == real swap, hooked pool skipped, native-ETH pool), run pytest -v tests/, then show which seed tokens gain qualifying pools. Tell me if any of this is wrong.
```

### Step 5: wiring DONE (2026-10-19); still needs Ken's private RPC URL in `.env` as `BASE_RPC_URL=...`
Benchmark on the public RPC is in §6. Re-run `python rpc_bench.py --workers 2` once the key is in, to check the quota.
```
Add a private Base RPC as the UPSTREAM that anvil forks copy from: BASE_RPC_URL in a gitignored .env, loaded with python-dotenv in pipeline.py, scanner.py and tests/conftest.py. Keep RPC_URL meaning 'the endpoint the scanner talks to' (usually the local fork) and do not merge the two. Never print or log the URL: redact it to scheme+host in pipeline.py's startup line (~line 550) and in scanner.py's 'Connected to' line (~1185), and make sure error strings written to results.jsonl and the per-token logs can't contain it. Add a test that runs one cycle with a fake BASE_RPC_URL containing 'SECRETKEY' and asserts that string never shows up in stdout, results.jsonl or logs. Document it in CLAUDE.md and confirm .env is gitignored. Then benchmark one cycle with 2 vs 4 workers and report the RPC request count/compute units used, so I can check it against my plan's quota.
```

### Step 6 (only after the gap monitor shows real, repeated gaps worth > $1): Execution contract on the fork
```
Phase 2: write a Solidity arbitrage executor with Foundry in ~/defi-arb-bot/contracts. It takes a route (pools + directions + pool kinds) and an amount, does every swap in one transaction, and reverts unless the final balance >= start balance + minProfit. Only the owner can call execute() and withdraw(). Add tests showing that a non-owner call reverts and a route that would lose money reverts. Funds are held by the contract (no flash loan yet). Follow CLAUDE.md: run everything only against a local anvil fork (127.0.0.1). No private keys, no `forge script --broadcast`, no `cast send` to any non-local RPC, no testnet or mainnet deployment. Write Foundry tests that replay the best routes recorded in logs/*/results.jsonl at their recorded block (tell me if that needs an archive RPC), plus one synthetic mispriced pool to show the profitable path works. Show gas used vs the GasModel (60k + 120k per swap).
```

### Later options
- **Allowlist cbXRP:** "Verify and pin cbXRP in TRUSTED_PROXIES with the same two-researcher process used for
  the other five; add it to the tests."
- **Results dashboard:** "Build a dashboard page from logs/*/results.jsonl and the opportunities DB."
- **The learning loop (much later):** only once the rule-based bot has real fork-confirmed data. Let an agent tune
  X, trade size and pool selection against recorded history, and never touch safety rules.

---

## 10. How to write prompts for Claude Code on this project (what worked)

- **State the outcome and the constraints, not the code.** "Add X; keep Y; add tests; run the suite" worked well.
- **Always ask for verification:** "run `pytest -v tests/` and show the result", "re-run one pipeline cycle".
- **Name the safety rules you care about:** fork-only, no keys, no real transactions, net-after-costs.
- **Ask for addresses to be verified on-chain and never hand-typed.** Every contract address so far came from two
  independent checks. Copycat tokens and wrong addresses were real risks.
- **Expect pushback and corrections:** Claude Code corrected several premises (file name, a wrong selector, double-
  counted fees). That's useful; keep prompts open to it ("tell me if any of this is wrong").
- **"Ultracode" mode** (`/effort ultracode`) makes Claude Code use multi-agent workflows: research by two independent
  methods, then adversarial reviewers that try to break the result. It caught most of the serious bugs listed in §5,
  but it's slow and token-heavy. Use it for safety-critical or research-heavy steps, not small edits.
- **Keep one phase per prompt.** Each of the 8 phases above was one prompt, and that rhythm worked.
- Claude Code keeps notes between sessions in its own memory, and always reads `CLAUDE.md`. If a rule matters
  permanently, ask for it to be added to `CLAUDE.md`.
