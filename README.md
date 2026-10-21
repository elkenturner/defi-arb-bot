# DeFi Arb Bot (Base L2)

Research and tooling for finding price gaps between DEX pools on Base (an Ethereum L2), with a focus on long-tail
token pairs. Phase 1 is a defensive, quote-based calculator plus a safety gate: nothing is traded on mainnet, and all
test transactions run on a local anvil fork and are rolled back.

The one decision rule: **trade only if net profit after all costs is above a threshold.** "All costs" means swap fees and
price impact (from real on-chain quoters), L2 execution gas and the L1 data fee. A raw price spread is never treated as profit.

## What's here

| File | What it does |
|---|---|
| `scanner.py` | Discovers pools for a token across Uniswap V3, Uniswap V4 (hookless), Aerodrome v2 and Aerodrome Slipstream (3 factory generations), quotes every 2–4 swap cycle through USDC and WETH, and nets out fees, impact, gas and L1 data cost. |
| `scanner.py` safety gate | Per-pool liquidity floor, a buy → transfer → sell simulation on a fork against every pool, bytecode scan for risky owner functions, proxy/controller detection, and owner-impersonation probes (e.g. "if the owner set the fee to 99%, could we still sell?"). Upgradeable tokens pass only if their exact controller chain matches a pinned fingerprint. |
| `pipeline.py` | Runs the scanner over a token list in cycles, one isolated anvil fork per worker, with a blacklist that only caches token-level failures (never RPC errors). Profitable quotes are re-run as real swaps on the fork before they're reported. |
| `gap_monitor.py` | Watches every block: one Multicall batch reads all pool prices and live dynamic fees, then fully quotes the most promising routes within a ~1.6s budget. Coverage accounting is explicit: every block is either done or counted as missed. |
| `arb_history.py` | Measures what existing arb bots actually earn: pulls every Swap event for a window, finds loop-shaped transactions, and computes profit from Transfer logs at the venue edge, minus gas. Validated against on-chain balance changes of real arbs. |
| `flashblock_recorder.py` | Records Base's ~200ms Flashblocks stream to tell whether arbs react to the stream or were already queued. |
| `rpc_bench.py` | Counts upstream RPC calls per method for one pipeline cycle (providers bill per method). |

## Findings so far

One day of Base history (2026-10-19): about 24k atomic arbs by 248 bot contracts made **~$4.9k net after gas**. The
profit is lumpy (one stablecoin dislocation was 41% of the day), most arbs earn cents, and for most tokens the top 3 bots
take 90–100% of the profit. Most of it lands inside blocks, right behind the swap that opens the gap, so a block-end
scanner sees only a small share. That finding is why the project moved from block-end scanning to history and Flashblocks timing.

## Tests

128 tests (`pytest -v tests/`) run against a local anvil fork of Base. They deploy trap tokens (honeypot, transfer tax,
mutable tax, pause, blacklist, mint), check quotes against real swaps on every venue, and pin the known contract
deployments so a protocol change fails loudly. Needs Foundry and internet; takes about 23 minutes.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
anvil --fork-url https://mainnet.base.org            # local fork
RPC_URL=http://127.0.0.1:8545 python scanner.py --amount 500
python pipeline.py --workers 1
python gap_monitor.py --minutes 60 && python gap_monitor.py --report
```

Provider URLs with API keys go in `.env` as `BASE_RPC_URL` (gitignored). They are redacted from all output and logs.

`CLAUDE.md` holds the detailed design rules and `HANDOFF.md` the full results log.

## Tech

Python 3, web3.py, Solidity + Foundry (anvil forks, test contracts), Multicall3, SQLite.
