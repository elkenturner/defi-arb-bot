# DeFi Arb Bot — Project Brain

## Goal
Find and (eventually) capture price gaps between DEX pools on Base (an Ethereum L2),
focusing on long-tail token pairs where big mainnet MEV searchers are less active.

## Strategy: strict rules first, learning later
- Phase 1 is a **defensive, quote-based calculator**. The only decision rule is:
  **"if net profit after ALL costs > X, trade."**
- "All costs" = AMM swap fees + price impact (both come from real on-chain quotes) + L2 execution gas + L1 data fee.
- A raw price spread is never treated as profit.
- **The RL / agentic learning loop is postponed.** It only starts once the rule-based version runs on a local
  fork and the numbers are trusted. The loop can then tune X, trade size and pool selection.

## Authorized pool types
| Kind | Venue | Discovery | Quote | Fork swap |
|---|---|---|---|---|
| `univ3` | Uniswap V3 | factory `getPool(a,b,fee)` for fees 0.01/0.05/0.3/1% | QuoterV2 (fee tier) | SwapRouter02 |
| `aero` | Aerodrome v2 "legacy" (vAMM/sAMM) | PoolFactory `getPool(a,b,stable)` | pool `getAmountOut` | Router (fee-on-transfer-safe) |
| `cl` | Aerodrome **Slipstream** (concentrated liquidity) | each CL factory's `tickSpacings()` → `getPool(a,b,tickSpacing)` | **that factory's** QuoterV2 (tickSpacing) | **that factory's** SwapRouter |
| `univ4` | Uniswap V4, **hookless pools only** | computed PoolIds for (fee, tickSpacing) = (100,1) (500,10) (3000,60) (10000,200), read via StateView | V4Quoter (next-block context) | Universal Router 2.0 + Permit2 |

**Uniswap V4 rules** (addresses from Uniswap's v4 deployment list, cross-checked on-chain; `test_v4_deployment_is_linked`):
| Contract | Address |
|---|---|
| PoolManager | `0x498581fF718922c3f8e6A244956aF099B2652b2b` |
| StateView | `0xA3c0c9b65baD0b08107Aa264b0f3dB444b867A71` |
| V4Quoter | `0x0d5e0F971ED27FBfF6c2837bf31316121532048D` |
| Universal Router **2.0** | `0x6fF5693b99212Da76ad316178A184AB56D299b43` |
| Permit2 | `0x000000000022D473030F116dDEE9F6B43aC78BA3` |
- A V4 pool has no contract address: `Pool.address` holds its **PoolId** = `keccak256(abi.encode(currency0, currency1, fee, tickSpacing, hooks))`.
- **Hooked pools are out of scope.** A hook runs arbitrary code on every swap. Discovery only computes hookless keys, so
  hooked pools are never seen. A hooked key passed to `v4_pool_from_key` is logged `skipped: hooked`: never a safety
  failure, never blacklisted. Custom fee / tick-spacing pools aren't found either (V4 allows any). Most Base memecoin and
  Zora/Clanker V4 liquidity is hooked, so V4 coverage today mostly adds blue chips (cbBTC: 5 pools ≥ $50k on 2026-10-19).
- **Native ETH is the WETH base.** For the WETH base both `WETH` and native ETH (`address(0)`) pools are looked up
  (`Pool.native`). Routes and P/L only ever see WETH. The fork swap unwraps (`WETH.withdraw`) before paying native ETH,
  and for native output TAKEs into the router and `WRAP_ETH`s it to the sender. A real executor pays the same ~wrap gas.
- **Fees:** Base V4 pools charge a **protocol fee** on top of the LP fee (12 bits per direction in `slot0.protocolFee`).
  `Pool.fee` = protocol + LP − protocol·LP/1e6 (worst direction). Quotes already include both. Read once at discovery.
- **Router struct:** UR 2.0's `ExactInputSingleParams` = `(PoolKey, zeroForOne, uint128 amountIn, uint128 amountOutMinimum, bytes hookData)`.
  UR 2.1.x adds `uint256 minHopPriceX36`: don't swap routers without changing `V4_SWAP_PARAMS`.
- **Quotes:** `quoteExactInputSingle((PoolKey, zeroForOne, uint128 exactAmount, bytes hookData))` → `(amountOut, gasEstimate)`,
  in next-block context, capped at `QUOTE_GAS`. The quoter itself reverts with `NotEnoughLiquidity` on a partial fill,
  so a returned quote is a full fill (no sqrtPrice to check). Amounts ≥ 2¹²⁸ → 0.
- **Depth (Rule 1) for V4:** the PoolManager holds every pool's tokens, so there are no per-pool balances. `v4_band_walk`
  walks initialized ticks (StateView bitmap + liquidityNet) and counts only liquidity within 2× / 0.5× of the price.
  That's stricter than V3's raw balances. `test_band_walk_matches_the_quoter` checks the math against real swaps.

**Slipstream has three live factory generations** (`SLIPSTREAM_DEPLOYMENTS`), confirmed by Aerodrome's FactoryRegistry
`0x5C3F18F06CC09CA1910767A34a20F771039E37C0` → `poolFactories()`:
| Name | Factory | QuoterV2 | SwapRouter |
|---|---|---|---|
| CL1 (2024) | `0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A` | `0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0` | `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5` |
| CL2 (2025) | `0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a` | `0x3d4C22254F86f64B7eC90ab8F7aeC1FBFD271c6C` | `0xcbBb8035cAc7D4B3Ca7aBb74cF7BdF900215Ce0D` |
| CL3 (2026) | `0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef` | `0x514c8B5f54112481E28028F1166Bd78501089259` | `0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F` |
Slipstream rules:
- **A pool is keyed (factory, tickSpacing) and must use its own factory's quoter and router.** A quoter only knows its own
  factory. Asked about a tick spacing that several factories share, it silently prices *its own* pool. Verified:
  1 WETH at ts50 = $2,060 through CL1's quoter vs $2,429 through CL3's.
- **Never hard-code tick spacings.** Read `tickSpacings()` from each factory (CL3 adds 80 and 150; CL2/CL3 add 500).
- Fees are dynamic: read `pool.fee()`, not `tickSpacingToFee()`. Slipstream `slot0()` has 6 fields, not V3's 7.
- Quoter struct: `(tokenIn, tokenOut, amountIn, int24 tickSpacing, sqrtPriceLimitX96)`. Router struct:
  `(tokenIn, tokenOut, int24 tickSpacing, recipient, deadline, amountIn, amountOutMinimum, sqrtPriceLimitX96)`.
  Note that tickSpacing sits in a different position in the two.
- Ignore the multi-factory "MixedQuoter" contracts (encoding unverified) and the unregistered empty CL factories.
- **If the registry adds a factory, `test_every_cl_factory_is_registered_and_linked` fails on purpose.** Add the new one.

**Quote safety (Uniswap V3, V4 and Slipstream):**
- Every quote `eth_call` is capped at `QUOTE_GAS` = 10M. Thin or wide pools can need ~1M+. A quote that runs out of gas or
  reverts returns 0 ("no quote") and the route is skipped. It never crashes the scan.
- **Partial-fill guard:** if a quote's final `sqrtPriceX96After` lands on `MIN_SQRT_RATIO`/`MAX_SQRT_RATIO`, the pool ran dry
  before taking all of `amount_in`, and the quoter silently returns a partial amount. Such quotes are rejected (0).
- Concentrated-liquidity pools (V3, V4, Slipstream) can't handle transfer-tax tokens (swaps revert), so a tax token there fails the fork sim.
- **Slipstream quotes run in next-block context** (`call_next_block`: eth_call block override number+1, time+2s).
  Fee modules add a same-block surcharge after a pool trades (CL1-ts100 WETH/USDC: ~0.06% at `latest` vs 0.015% next block),
  and our tx can only land next block. Quoting at `latest` under-priced that pool by ~0.06% per leg. If an RPC rejects block
  overrides, it falls back to `latest` with a warning. Display fees (`pool.fee()`) are read once at discovery and may be stale.

## Multi-base asset routing (USDC + WETH)
- **Base assets:** USDC and WETH (`BASES` in `scanner.py`). A token's pools are discovered against **both**: every
  Uniswap V3 fee tier and Aerodrome v2 stable/volatile. Long-tail Base tokens mostly trade against WETH (DEGEN, BRETT).
- **Anchor pool:** Uniswap V3 WETH/USDC 0.05% (`anchor_pool()`). It's the only pool trusted to convert between bases,
  and its `slot0` mid price (`eth_usd_mid()`) is the single ETH price for sizing, liquidity valuation, P/L and gas.
- **Sizing:** `--amount` is in USD. For a WETH-start cycle it's converted to the exact WETH equivalent at the anchor
  mid price for the current block (no fee charged for that conversion: we assume WETH inventory or a WETH flash loan).
- **Cycles** (`build_legs`), for every ordered pair of qualifying pools (A, B):
  | A base | B base | `--settle native` (default) | swaps | `--settle usdc` | swaps |
  |---|---|---|---|---|---|
  | USDC | USDC | USDC → T → USDC | 2 | same | 2 |
  | WETH | WETH | WETH → T → WETH | 2 | USDC → WETH → T → WETH → USDC | 4 |
  | USDC | WETH | USDC → T → WETH → USDC (anchor) | 3 | same | 3 |
  | WETH | USDC | WETH → T → USDC → WETH (anchor) | 3 | USDC → WETH → T → USDC | 3 |
  Native is the cheapest, since it never pays the anchor fee just to get in and out. Use `usdc` when the bot must hold only USDC.
- **P/L** is measured in the start asset (end − start) and converted to USD at the anchor mid price. Then gas is subtracted.
- **Gas apportionment:** `GasModel`: units = 60k overhead + 120k per swap (2 = 300k, 3 = 420k, 4 = 540k). The L1 data fee
  is read for a tx of 250 + 75 bytes per swap. Tune with `--gas-overhead` / `--gas-per-swap`.
- The per-pool $50k floor applies to every token pool whatever its base. A WETH pool's base side is valued at the anchor price.
- The fork simulation runs on **every** qualifying pool, each in its own base (a WETH pool is bought and sold in WETH).

## Long-tail safety: traps to track and avoid
Every token must pass `is_safe_token(token, ctx)` before any route that uses it gets quoted.
Three layers: per-pool liquidity, 3-step fork simulation, contract structure (owner powers).

### Rule 1: per-pool liquidity floor (default $50k, `--min-liquidity`)
- **Every pool in a route must clear the floor on its own.** Never sum liquidity across pools; one deep pool
  must not vouch for a thin one.
- A pool's depth = 2 × the smaller of its two sides (base balance in USD vs token balance priced with a $1-of-base probe
  quote on that same pool). So a pool drained on either side reads as thin.
- Pools below the floor are dropped before any quoting. Uniswap V3 balances include out-of-range liquidity,
  so treat a V3 number as an upper bound.

### Rule 2: 3-step fork simulation (only runs against a local anvil fork)
Runs on **every qualifying pool** (`simulate_all_pools`, deepest first), each inside `evm_snapshot` / `evm_revert` so
the fork is left untouched. **Any pool failing fails the whole token.** Routes can sell into any pool, and quotes never
run the token's transfer code, so a trap on one pool (e.g. sells blocked into the USDC pair only) is only caught by a
real swap there. A token that treats its pools differently is a trap by design. Recorded taxes are the worst seen across pools.
Wallet A = `w3.eth.accounts[0]`, wallet B = `w3.eth.accounts[1]`. A is funded by wrapping 0.1 ETH (then swapping it to
USDC through the anchor if the test pool is USDC-based).
**On Base every default anvil account has an EIP-7702 delegation** (code `0xef0100…`, set on mainnet by whoever uses
the public test keys). `prepare_test_wallet` strips that code on the fork before any test wallet is funded, so the sim
runs on plain wallets and ETH sent to them doesn't run foreign code. Never call it on a real controller (may be a Safe).
- **a) Buy:** A swaps $20 of the base asset → token. Revert or 0 tokens received = HONEYPOT. Received vs quoted = `ctx.buy_tax`.
- **b) Transfer:** A sends everything to B. Revert = HONEYPOT. `1 - received/sent` = `ctx.transfer_tax`.
- **c) Sell:** B sells everything back to the base asset. Revert or 0 out = HONEYPOT. Received vs quoted = `ctx.sell_tax`.
- Pass only if all three legs succeed **and** every measured tax ≤ `--max-tax` (default 0.5%).
- Aerodrome swaps use the router's fee-on-transfer-safe function. Uniswap V3 can't handle tax tokens at all
  (sells revert), so a tax token on V3 shows up as a sell failure. Rejecting it is the correct outcome either way.

### Rule 3: contract structure — can the rules change after we buy?
Order inside `is_safe_token`: liquidity → bytecode scan → controllers → proxy check → 3-step sim → owner probes.
Verdicts land in `ctx.verdict`: `SAFE`, `PROXY_WARNING` (passes; upgradeable by an allowlisted issuer), `UNSAFE`
(fails today) or `DYNAMIC_UNSAFE` (an owner could make it fail later).

**a) Bytecode scan (read-only, works on any RPC).** `get_code` → walk the opcodes and collect PUSH1–PUSH4 constants
(the function dispatcher compares against these) → match against selectors **computed from signatures**
(`selector("pause()")`). Never hard-code selector hex. (`renounceOwnership()` = `0x715018a6`; `0x84563224` is wrong.)
Families: tax setters (`setTax`, `setFee(s)`, `updateFees`, `setBuy/SellFee/Tax`…), `pause`, `blacklist`/`setBot`,
`setTradingEnabled`, `setMaxTxAmount`/`setMaxWalletSize`, `mint`, `upgradeTo`. Matches are logged as warnings only.
If the token is a proxy, the implementation's code is scanned too. Both EIP-1967 slots and the older ZeppelinOS slots
(USDC/cbBTC-style FiatToken proxies) are checked.

**b) Controllers.** `owner()`, `getOwner()`, `admin()`, plus proxy admin storage slots. Zero address = renounced.

**c) Upgradeable proxy + live controller → `DYNAMIC_UNSAFE`, unless it's on the Trusted Proxy Allowlist.** The admin
can swap in any code, so no probe can prove an unknown proxy safe.

**Trusted Proxy Allowlist (`TRUSTED_PROXIES` in `scanner.py`).** Institutional issuers' upgradeable tokens pass this
step with verdict **`PROXY_WARNING`** and then go through every remaining check (fork sim on every pool, owner probes)
and on to routing. Rationale: our trades are atomic (same block). An upgrade can only hurt us if it lands in our block
ahead of our tx, so a known issuer's upgrade key is an accepted risk, like mint.
- **Trust is keyed by token address + its exact controller chain, never by a controller address.** Anyone can deploy a
  proxy whose admin slot names Coinbase's admin. `controller_chain()` fingerprints:
  - the scanner's controllers (`owner()`, `getOwner()`, `admin()`, proxy admin slots);
  - whoever controls each of them (`owner()`, `getOwner()`, `getEthereumGovernanceExecutor()`), 2 levels deep;
  - for every Gnosis Safe: each signer, the threshold, the module list (a module can bypass the threshold) and the guard;
  - timelock `getMinDelay()` and governance executor `getDelay()`.
  The live fingerprint must **equal** the pinned one. Any rotation, new signer, lower threshold, new module or shorter
  delay → back to `DYNAMIC_UNSAFE` with code **`PROXY_PIN_MISMATCH`**. It's blocked but **not blacklisted**, so it's
  re-checked every cycle and passes again as soon as a human re-pins it. (Unlisted proxies keep code `PROXY`, which is cached.)
- **Fingerprint reads fail closed, never open:** a getter that *reverts* means "doesn't apply". A *transport* error
  (timeout, 429) raises, so the token becomes ERROR and is retried. It never silently drops keys, which would fake a
  control change. An unreadable or truncated Safe module list becomes `#modules=UNREADABLE`/`TRUNCATED`, which never
  matches. It must never default to "no modules", since a module can bypass the threshold.
- **Current entries** (pinned at Base block 52689443, 2026-10-16):
  | Token | Issuer | Upgrade control | Attribution |
  |---|---|---|---|
  | cbBTC | Coinbase | 2 single-key EOAs (owner + admin), same as Coinbase's L1 cbBTC. Instant | high |
  | cbETH | Coinbase | 3/6 Safe (also admin of USDbC), no timelock. Instant | **medium**: Safe not named in any doc |
  | wstETH | Lido | Base Governance Bridge Executor → Lido Aragon Agent (L1 DAO vote) | high (docs.lido.fi) |
  | weETH | ether.fi | EtherFiTimelock (3-day) + ProxyAdmin | **medium**: controllers not named in docs |
  | MORPHO | Morpho | morpho.eth 5/9 Safe, no timelock. Instant | high (docs.morpho.org) |
- **A pin means "the issuer is identified", not "upgrades are safe"**: three of five can upgrade instantly. Only add an
  entry after two independent checks (on-chain chain trace + official issuer docs) and record its confidence + evidence.
- Known gap: weETH's timelock grants proposer/executor rights by role (a 4/7 Safe), which the fingerprint doesn't read.
  Its 3-day `minDelay` is pinned.
- Re-pin procedure: re-run the controller trace, compare with the issuer's docs, regenerate the entry from
  `controller_chain()` output (never hand-type addresses), and update `verified_block`.

**d) Owner impersonation probes (fork only).** For every risky function that's present and every controller:
`evm_snapshot` → `anvil_impersonateAccount(controller)` → call it with extreme values
(fees: 9900, then 99, then 25; pause; blacklist our buy wallet; max tx = 1; trading off) → if the call succeeds,
re-run buy→transfer→sell quietly on every pool → if trading now fails anywhere, record it → `evm_revert`.
Any recorded finding (except mint) → `DYNAMIC_UNSAFE`.
- **Mint is a warning, not a block.** It dilutes holders but can't break a same-block arb. Revisit if we ever hold
  inventory across blocks.
- A call that succeeds but changes nothing is logged as a warning.

### Live RPC fallback
A live RPC can't send test transactions. The bytecode scan, controller lookup and proxy check still run there.
After that, only `VERIFIED_TOKENS` (WETH, USDC) pass. **Any long-tail token must be checked against an anvil fork.**

### Not covered yet
- Risky functions with custom names (e.g. `setLiquidityFeePercent`, `manualSwap`). Only listed signatures are found,
  and a scammer can rename anything. Unverified, obfuscated contracts deserve extra suspicion.
- Controllers that aren't `owner()/getOwner()/admin()` (e.g. AERO's `minter()`, role-based `AccessControl`).
- Timelocks: a timelocked owner shows up as a controller that can act, even though changes would be delayed.
- Wallet-specific rules (e.g. cooldowns between trades). The simulation only uses two fresh wallets and one small trade size.
- Liquidity for Uniswap V3 and Slipstream pools is valued from raw balances, which includes out-of-range liquidity.
  A pool can look deep while its active price range is thin. Price impact in the quotes still reflects the real depth.

## Multi-token pipeline standard (`pipeline.py`)
`scanner.py` judges one token. `pipeline.py` feeds it a list, cycle after cycle.
- **Seed list:** `SEED_TOKENS` = the core monitoring baseline of actively traded Base tokens. Every address was verified on-chain
  (code, `symbol()`, a live pool vs USDC/WETH on a venue we scan). **Copycat tokens reuse symbols: never add or edit
  an entry by symbol alone.** Override with `--tokens 0x..,0x..` or `--tokens-file list.json`.
- **One fork per worker, never shared.** The safety sim and owner probes use `evm_snapshot`/`evm_revert`. Two workers on one
  fork would revert each other's state mid-check and corrupt both verdicts. So every worker process starts its own
  anvil and pulls tokens from a shared queue. `run_cycle` refuses `workers > 1` with an existing `--rpc` fork. Guards:
  - ports are picked only if nothing is listening (`pick_free_ports` from `--base-port`), and `start_anvil` **refuses a
    busy port**: a leftover or manual anvil there would otherwise answer and be used silently;
  - each worker stamps a sentinel balance (`FORK_MARKER`) on its fork and checks it after every token;
  - an interrupted startup always kills its anvil child (no orphan holding the port).
- **Fresh state per token:** a worker resets its own fork to the latest block (`anvil_reset`) before each token, so tokens
  late in a cycle aren't judged on stale prices. (Not done with `--rpc`: that fork belongs to someone else.)
- **Coordinator never hangs:** workers send `ready` / `started` / result / `worker_failed` messages. Tokens are handed out
  only after every fork is up (barrier). The coordinator polls every 2s, notices dead workers immediately, marks their
  in-flight token ERROR, and caps any single token at `TOKEN_TIMEOUT` (900s).
- **Library calls, not copies:** workers call `scanner.evaluate_token()` (pools → floor → safety → every route). The
  scanner's verbose output for each token goes to its own file, `logs/<run>/cycle<N>/<SYMBOL>-<addr>.log`.
- **Honeypot means an on-chain revert, nothing else** (`scanner.is_onchain_revert`). A timeout, HTTP 429 or dropped
  connection during the buy/transfer/sell or an owner-probe re-sim propagates as an exception → token `ERROR` → retried
  next cycle. Before this rule, one public-RPC timeout could permanently blacklist a good token.
- **Blacklist (`blacklist.json`):** only the coordinator writes it (atomic tmp + rename). A token is cached as bad **only**
  for token-level reasons (`MALICIOUS_CODES`: `HONEYPOT`, `TAX`, `PROXY`, `OWNER_POWER`). `LIQUIDITY` (thin today),
  `INFRA` (our test setup failed), `LIVE_RPC` and `ERROR` (RPC/timeouts, not-a-token) are **never** blacklisted and get
  re-checked next cycle. `PROXY_PIN_MISMATCH` and `PROXY_WARNING` are never blacklisted either. Blacklisted tokens are skipped before any fork or RPC work. `--recheck` ignores the list for
  one run. Delete the entry (or the file) to re-admit a token. A corrupt file is moved to `blacklist.corrupt.json`.
- **Output:** unprofitable tokens are silent on screen (TTY: one self-overwriting progress line), plus a one-line cycle
  summary. Every result goes to `logs/<run>/results.jsonl`. **A route with net profit after all costs > `--min-profit`
  is re-run as real swaps on the fork** (`confirm_route_on_fork`, then rolled back). **Only a route whose fork re-run
  also clears `--min-profit` gets the loud red banner + terminal bell** (`is_confirmed_hit`). Quote-only "hits" are
  recorded in results.jsonl but stay silent.
- Quotes apply the token's measured buy/sell tax (from the fork sim), since pool quotes never run the token's transfer
  code. So the ranking and the fork re-run are on the same basis.
- **Rate limits:** every fork pulls state from `--fork-url` (env `BASE_RPC_URL`). The public RPC throttles under several
  workers. Use 1–2 workers on the public endpoint, more only with a private RPC.
- Commands: `python pipeline.py` (one cycle) · `--loop --interval 300` · `--workers 2` · `--recheck`.

## Trade size search (`--size-search`, scanner and pipeline)
- Off by default (default behaviour unchanged: one size, `--amount`). With the flag, the top `SEARCH_TOP_ROUTES` (3)
  routes per token (ranked at `--amount`) are re-quoted at `SEARCH_SIZES` = $100/$250/$500/$1k/$2.5k/$5k, then refined
  `SEARCH_REFINE_ROUNDS` (2) times at the geometric midpoints next to the best size. Never beyond the grid.
- `search_best_size()` returns the full route result at the best size plus the whole size→net curve.
  results.jsonl gets `size_search: [{route, path, best_size, best_net, curve}]` (null when off).
- A best-size net > `--min-profit` becomes a hit with `size_search: true` and is re-run on the fork **at that size**.
  Only a fork-confirmed one gets the banner, as before.

## Private RPC (`BASE_RPC_URL`)
- `BASE_RPC_URL` = the **upstream** that anvil forks copy from (pipeline, tests, gap monitor). `RPC_URL` = the endpoint the
  scanner talks to (usually the local fork). They are separate on purpose. Both may come from `.env` (gitignored),
  loaded by `import scanner`.
- Provider URLs carry the API key. **Never print or store them raw:** `scanner.redact_url()` (scheme + host) for display,
  `scanner.scrub()` / `pipeline.scrub_deep()` on every per-token log line, worker error and results.jsonl record.
  `test_upstream_key_never_reaches_output_results_or_logs` runs a cycle through an upstream whose URL holds a fake key.

## Gap monitor (`gap_monitor.py`): is there money, block by block?
The pipeline samples each token once per ~15-min cycle; gaps live for seconds. The monitor watches **every block**.
- **Inputs:** SAFE / PROXY_WARNING tokens from the latest `logs/*/results.jsonl` (no safety re-checks), with their
  qualifying pools (stored by the pipeline since 2026-10-16; older files get pools rediscovered without safety checks).
- **Each block:** one Multicall3 batch reads every pool's mid price (slot0 / reserves; stable pools via the
  x³y+xy³ marginal price) **and every Slipstream pool's live fee** (dynamic, read in next-block context). This gives the
  gross spread and per-block fee for all ~450 two-pool routes. Then routes are fully quoted **most promising first**
  (gross − fees) at $100/$250/$500/$1,000/$2,500 through `scanner.quote_request/decode_quote` (shared with
  `scanner.quote`), leg by leg in batched rounds, in next-block context, with measured tax and per-swap gas.
- **Budget:** `Budget` adapts how many routes get fully quoted so a block's work fits in ~1.6s. On the public RPC that's
  ~15–20% of routes. Unquoted routes still get their gross spread and fee recorded, and their nets stay NULL (never guessed).
- **Upper bound:** gross − fees ≥ net (impact, tax and gas only subtract). So an unquoted route-block with
  gross ≤ fee − 0.5 bps provably lost money. One with gross > fee is reported as **unknown**, not as a loss.
- **Coverage is honest by construction:** `runs` table with start/end head. Every block in that range is either done or
  missed (outages, slow blocks and RPC errors all count). Next-block overrides are probed once at startup. A
  per-call RPC error fails that block (counted missed); it never silently switches modes.
- **Report** (`python gap_monitor.py --report [--run N]`, latest run by default), per route: watched, quoted, unknown,
  blocks with best net > $0 / > $1, `run` (longest confirmed streak of consecutive profitable blocks), `span` (longest
  stretch between profitable blocks with no observed loss; unseen blocks don't break it), best net @ size, max gross−fee.
  It only says "coverage was complete" when nothing was missed, unknown or unreadable.
- **Interpretation:** a gap seen at block end is an **upper bound**. Base also streams ~200ms Flashblocks, so
  co-located bots act inside the block, and everyone sees the same end-of-block state.
- `BASE_RPC_URL` comes from `.env` (gitignored) and falls back to the public RPC. The URL is never printed or stored
  (only scheme + host).
- Commands: `python gap_monitor.py --minutes 120` · `python gap_monitor.py --report`.
- RPC budget: `python rpc_bench.py --workers N` runs one pipeline cycle through a local counting proxy and reports upstream
  calls per method (providers bill per method). The upstream URL is never shown.

## Arb history (`arb_history.py`): what do real arb bots make?
Gap scans read block-end state, but on Base arbs land *inside* blocks (right behind the swap that opens the gap). So this
measures the existing atomic-arb market from history, read-only (nothing signed; hooked V4 pools are read, never traded).
- **Logs:** every Swap event in the window (Uniswap V3 + Slipstream share one event; Aerodrome v2; Uniswap V2-style forks;
  V4 PoolManager incl. hooks), chunked `eth_getLogs`, range halves on provider errors, resumable (`logs/arb_history.db`).
  Pools are labelled by `factory()`: `univ3`/`cl1-3`/`aero`/`univ4` = venues we trade, the rest `other_*`.
  V4 PoolIds are mapped to keys via PositionManager `poolKeys` (verified by re-hashing). Ids it doesn't know
  (hook-managed liquidity: 4,298 across 2026-10-17..19) come from the **PoolManager's own `Initialize` event**:
  `locate_v4_inits` bisects each pool's creation block to a 100-block window, sharing one Multicall per interval per
  round. The probe is `PoolManager.extsload(keccak(id, 6))` (slot0 sqrtPrice != 0 = exists; never un-initialized, so
  monotone). It's checked against StateView by `test_v4_existence_probe_reads_the_pool_managers_own_storage`, and works
  at any block since the deployment (archive `eth_call`: Blast, Tenderly, dRPC, mainnet.base.org). Then one 100-block
  `getLogs` per window gives the key, re-hashed against the id. Resumable (`windows` / `on_round` checkpoints); a
  window whose logs keep failing is skipped (rerun retries it) and keys are saved as they arrive (`on_found`). Ids still
  unresolved stay `unknown_pool`, kept out of the totals. Result (2026-10-20, all 4,298 resolved): the rough
  "≤ $14k/day gross" on those txs was **$30–36/day net** (all hooked pools; hookless $0). 2/3 weren't loops once the
  tokens were known, most of the rest were LP actions. **Never guess a V4 key from transfer amounts:** a hook fee
  broke the match and the "missing" side was assumed native ETH, which booked a fake 2.9M-ETH arb.
- **Candidate:** 2+ distinct pools and a loop (a token paid into an earlier swap comes out of a later one; holds in any
  log order, since nested callbacks emit inner swaps first). Plain A→B→C trades never qualify.
- **Arb + profit (receipt):** net flow per asset **at the venue edge**: ERC20 Transfers out of the swapped pools / the
  PoolManager (hooked pools' hook contracts count as inside, but only their transfers with tx.from/to) minus Transfers
  into them. Not the `{from, to}` balance: real bots flash-borrow from the PoolManager and pay
  profit to a third address (`0x8d48…` → `0x7190…`), which an actor-balance view misses. WETH = native ETH; native V4
  legs come from the Swap event. Arb = flow worth > 0 and its biggest gain is an asset the tx also paid in. Valued at
  the anchor (last anchor swap before the block; non-base tokens at the tx's own swap rates), minus L2 gas + `l1Fee`.
  Priority = gasUsed × (effectiveGasPrice − baseFee). Backrun = tx index − 1 swapped on one of the arb's pools.
- **Hooked V4 Swap events can lie** (a hook moves value with return-deltas): that's why profit comes from Transfer logs,
  and why a hooked pool with a native-ETH leg is `hooked_native` (not valued: its ETH movement has no log).
- **LP rebalances are not arbs:** a tx where a swapped pool / the PoolManager also emits Mint/Burn/Collect/
  ModifyLiquidity is `lp_action`. Tokens leave the pool with nothing paid in; before this rule the day's "top arbs"
  ($5k each) were all V3 Burn+Collect rebalances.
- **Validated** on 5 arbs incl. the day's 3 largest (`test_known_mainnet_arbs`): each recipient's balance diff N-1→N
  equals the measured profit exactly.
- **Attempts (`--attempts N`):** a losing attempt leaves no Swap log (it reverts, or the bot's contract checks the
  price and returns early, which succeeds and still burns gas), so every N-th block's receipts are read
  (`eth_getBlockReceipts`: Tenderly, dRPC, mainnet.base.org; Blast refuses). Every tx to a known bot contract or from a
  known bot EOA counts. Per bot: arb net (all blocks) − gas of its non-arb txs (sampled, scaled ×N) = true net.
  **Only `bot` contracts count** (sampled arbs + losing loops ≥ plain trades). The rest are `mixed`: ERC-4337
  EntryPoints, SwapRouter02, aggregators, bots running other strategies. Their users' trades are not failed arb attempts.
  Counting them made day 2 look −$3.3k/day; arb-only contracts are really +$2.7k/day ($3.9k arbs − $1.2k wasted gas,
  ~10 txs per arb).
- **Blind spots:** loops mostly through PancakeSwap V3 / Balancer / Curve are missed (their Swap events aren't pulled);
  attempts by bots that never landed an arb in the window aren't attributed to anyone.
- **RPC:** `mainnet.base.org` refuses `eth_getLogs` under any load. dRPC's free tier works: ~100-block ranges, batches > 1
  partly fail (use parallel single calls: `--max-batch 1 --threads 4`). A 24 h run ≈ 70k receipts.
- Commands: `python arb_history.py --hours 24 --rpc https://base.drpc.org` · `--to-block N` (window ends at N; a DB is
  locked to one range, so use `--db` for another window) · `--report` · `--rejudge` (re-classify from
  stored receipts) · `--tx 0x…` (explain one tx).

## Flashblock recorder (`flashblock_recorder.py`): react or pre-queued?
On 2026-10-19 only ~5% of real arb profit came from gaps already open at block start (`arb_history` `trigger_gap`
NULL), which is all a block-end scanner can see; ~64% landed right behind the swap that opened the gap. Base streams
each block as ~11 flashblocks (~200ms apart, index 0 = the system deposit) on `wss://mainnet.flashblocks.base.org/ws`
(public, brotli-compressed JSON, read-only). History keeps no flashblock boundaries, so this records them live.
- Stores per flashblock: block, index, the block tx-index range it added, arrival time (`logs/flashblocks.db`).
  Verified against the chain: per-block tx counts and tx positions (hash of the streamed raw tx at that index) match.
- A block is **untrusted** (table `gaps`, never used) if the stream joined mid-block, skipped an index or a block, or
  reconnected. `flashblock_of()` returns None there; never guess a range.
- `--timing <arb_history db>` (run arb_history over the recorded window with `--to-block`): each arb's flashblock minus
  its trigger swap's. 0 = same flashblock (the bot was already queued: spam / priority), ≥1 = it reacted to the stream,
  which a new bot could do too. "block start" = gap open before the block.
- **Result (2026-10-20, 3.1 h of trusted blocks, 3,073 arbs placed):** 98% of profit in the trigger's flashblock
  (incl. one $2,039 ZORA arb, also same flashblock). Excluding it, same flashblock
  $439 (avg $0.42/arb), +1 flashblock $37 (avg $0.02), +2 or more $1, open at block start $20. The money is taken by
  txs already queued when the gap opened; reacting to the stream wins cents. Flashblock spacing median 166 ms.
- Commands: `python flashblock_recorder.py --hours 6` · `--report` · `--timing logs/arb_history_fb.db`.

## Tests
`pytest -v tests/` starts its own anvil fork on port 8546, so it doesn't clash with a fork on 8545. It compiles
`tests/contracts/src/Trap.sol` with forge and deploys fake tokens with $60k Aerodrome pools
(`make_trap(..., bases=(USDC,))`, or `(WETH,)` / `(USDC, WETH)` for multi-base):
- `test_safety.py` (10): CLEAN (renounced), LOCKED (owner, powers disabled), TAX5, HONEY, MUTTAX, PAUSE, BLIST, MINT,
  real WETH, real cbBTC.
- `test_routing.py` (12; incl. size search finds the interior optimum): leg building for all base/settle combos, USD→WETH sizing, gas scaling, anchor mid vs quote,
  WETH-only safe and tax tokens, a USDC↔WETH triangle checked against an independent x·y=k recomputation, a honeypot
  on the *shallower* of two pools (must be UNSAFE), and a tax token on two bases.
- `test_pipeline.py` (25; incl. size-search hit re-run at its size, RPC key scrubbing, upstream-key leak cycle): blacklist persistence + corrupt-file recovery, which reason codes get blacklisted, silent vs
  loud output (incl. an unconfirmed hit staying silent), transport errors never counted as honeypot, bad addresses in the
  list, busy port refused, dead workers don't hang the coordinator (<60s), shared-fork worker refusal, a full
  single-worker cycle over 5 assets on the test fork (in order, exactly once each, blacklist written, mispriced ARB token
  = fork-confirmed hit, fork state unchanged, blacklisted token skipped next cycle), and 2 workers on 2 isolated forks
  of real Base (both used, sentinel verified, both forks shut down).
- `test_trusted_proxy.py` (12): each allowlisted token → `PROXY_WARNING` on the fork; unlisted test proxy blocked; a proxy
  copying Coinbase's admin address blocked; rotating cbBTC's admin on the fork → blocked, then trusted again after
  revert; lowering MORPHO's Safe threshold → pin broken; synthetic pin lets a test proxy through; injected RPC timeout raises (doesn't drop keys);
  reverted module read → `UNREADABLE` → no match. `test_safety.py` now
  uses DRV (real, unlisted proxy) for the "unlisted proxy stays blocked" case.
- `test_gap_monitor.py` (15): batched quotes == `scanner.quote`; mid prices vs tiny quotes; stable-curve marginal price;
  finds and stores the mispriced test token's gap (matches `calculate_net_out` within 5¢); budget quotes the best route
  first and leaves others NULL; live Slipstream fee per block; RPC errors fail the block without switching modes;
  streak/bridged-span math; report counts incl. missed tail and unknowns; complete-coverage claim only when true;
  runs reported separately; RPC key never printed or stored.
- `test_uniswap_v4.py` (10): deployment links, PoolId derivation, discovery for USDC + native ETH, hooked keys skipped,
  band walk == quoter, quote == real swap both ways (ERC20 and native ETH via WETH), oversize quote → 0, cbBTC fork sim on
  V4 pools + mixed routes, gap monitor V4 mid price.
- `test_arb_history.py` (31): swap decoding (V4 sign flip), loop detection in any log order, venue-edge profit incl.
  profit forwarded to a third address and PoolManager flash loans, user trades rejected, native V4 legs, unresolved
  V4 pools / hooked native legs / LP rebalances not valued, hook-contract transfers inside the venue, V4 key re-hash,
  report math (seed split, top-3 shares, medians), backrun lookup, range shrinking, URL scrubbing, 5 validated mainnet
  arbs + 1 real arb on an unresolvable pool flagged (receipts via the public RPC), seed bucket pinned to the seed list,
  V4 Initialize locator (bisection, resume, extsload probe vs StateView live, failing windows skipped + keys saved as
  found), attempt extraction + true-net math, public contracts (EntryPoint/router) not counted as bots.
- `test_flashblock_recorder.py` (4): tx ranges per flashblock, untrusted blocks (mid-block join, missing index, skipped
  block), arb timing relative to the trigger, per-token buckets, flashblock arrival spacing.
- `test_slipstream.py` (7): discovery for both bases plus pool→factory linkage, quote == real swap, oversize quote →
  0, CL routes mixed with legacy pools, registry ↔ deployments tripwire, shared-tick-spacing quoter routing,
  next-block quote context. The quote==swap test pins the swap's timestamp to the quote's (dynamic fees decay with time).
The test deployer gets USDC by minting through Circle's masterMinter (impersonated on the fork), **not** by swapping.
Swapping $600k through the anchor moved its price and broke later tests. Takes about 23 minutes (130 tests). Needs Foundry and internet. **Add a test whenever a new trap type or route shape is handled.**

## Tech stack
- **Python 3** + **web3.py**: scanning, quoting, simulation driver
- **Solidity**: on-chain execution contract (NOT written yet)
- **Foundry**: `forge` to build/test contracts, `anvil` for a local fork of Base
- **RPC**: `https://mainnet.base.org` for now; a private RPC for anything real

## Hard rules
1. **Always simulate transactions locally using an offline mainnet fork before writing any execution logic.**
   (`anvil --fork-url $BASE_RPC_URL`, then point scripts at `http://127.0.0.1:8545`.)
2. No private keys in code or git. Keys live in `.env` only (gitignored).
3. No real-money transactions without Ken's explicit go-ahead for that specific step.
4. Report net profit **after** fees, price impact and gas. Quotes from on-chain quoters already include the
   swap fee and price impact, so don't subtract the fee a second time.

## Current phase
Phase 1: quote scanner + safety gate (`scanner.py`). No arb contracts. On a live RPC nothing is sent;
on an anvil fork only local test transactions are sent, then rolled back.

## Gas on a fork
`eth_gasPrice` on anvil adds a 1 gwei tip, about 100x what Base needs. The scanner uses the block base fee
plus `--priority-gwei` (default 0.001) instead, so fork and live gas estimates match.

## Key Base addresses
- WETH: `0x4200000000000000000000000000000000000006`
- USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- Uniswap V4: see the V4 table above
- Uniswap V3 Factory: `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`
- Uniswap V3 QuoterV2: `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a`
- Uniswap V3 SwapRouter02: `0x2626664c2603336E57B271c5C0b26F421741e481`
- Aerodrome Slipstream: see the CL1/CL2/CL3 table above. Aerodrome FactoryRegistry: `0x5C3F18F06CC09CA1910767A34a20F771039E37C0`
- Aerodrome PoolFactory (v2 pools): `0x420DD381b31aEf6683db6B902084cB0FFECe40Da`
- Aerodrome Router: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
- OP-stack GasPriceOracle (L1 data fee): `0x420000000000000000000000000000000000000F`

## Commands
- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Scan live: `python scanner.py --amount 500` (`--amount` is USD, whatever the start asset)
- Other token: `python scanner.py --token 0x... --amount 500`
- Against a local fork: `anvil --fork-url https://mainnet.base.org`, then `RPC_URL=http://127.0.0.1:8545 python scanner.py`
- Tests: `pytest -v tests/`
- Pipeline: `python pipeline.py [--loop] [--workers N] [--tokens 0x..,0x..] [--recheck]`
- Arb market history: `python arb_history.py --hours 24 --rpc https://base.drpc.org`, then `--report`
- Safety knobs: `--min-liquidity 50000` (per pool, USD), `--max-tax 0.5` (percent), `--priority-gwei 0.001`
- Routing knobs: `--settle native|usdc`, `--gas-overhead 60000`, `--gas-per-swap 120000`, `--size-search`
