"""Quote-based arbitrage scanner for Base, with long-tail safety checks.

For a token, it finds every Uniswap V3, Aerodrome v2 (legacy) and Aerodrome Slipstream (concentrated liquidity)
pool paired against a base asset (USDC or WETH),
drops any pool that is individually too thin, then for each ordered pool pair (A, B) it simulates a cycle:
    same base:   base --(A)--> token --(B)--> base                         (2 swaps)
    mixed base:  baseA --(A)--> token --(B)--> baseB --(anchor)--> baseA   (3 swaps, triangular)
The anchor is the deep Uniswap V3 WETH/USDC 0.05% pool. With --settle usdc every cycle starts and ends in
USDC, so WETH legs get an anchor hop on each side (up to 4 swaps).
Every swap uses the pool's own quote function, so swap fees and price impact are already baked in.
It then subtracts estimated gas (L2 execution + L1 data fee, scaled by swap count) to get the net result.

Safety: against a local anvil fork, is_safe_token() runs a real buy -> transfer -> sell on the fork
(inside an evm_snapshot that is reverted afterwards). Against a live RPC it can't send anything,
so it falls back to a short verified-token list.

Against a live RPC nothing is ever signed or sent. On a fork, only local test transactions are sent.
"""
import argparse
import os
from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from eth_abi import encode
from web3 import Web3
from web3.exceptions import ContractLogicError

# RPC_URL is the endpoint this scanner talks to (usually a local anvil fork). BASE_RPC_URL (pipeline.py, tests) is the
# upstream that forks copy state from. Either may come from the gitignored .env and may carry an API key, so they are
# only ever shown through redact_url() / scrub().
load_dotenv(Path(__file__).resolve().parent / ".env")
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")


def redact_url(url):
    """Scheme + host only: provider URLs carry the API key in the path or query."""
    p = urlparse(str(url))
    return f"{p.scheme}://{p.hostname}" if p.hostname else "<rpc>"


def scrub(text, *urls):
    """`text` with every given URL (and its key-bearing path/query on its own) replaced by its redacted form."""
    text = str(text)
    for url in urls:
        if not url:
            continue
        p = urlparse(url)
        text = text.replace(url.rstrip("/"), redact_url(url))
        # the key on its own too (it can show up without the host): every long path segment and query value
        pieces = p.path.split("/") + [kv.split("=", 1)[-1] for kv in p.query.split("&")] + [p.password or ""]
        for piece in sorted(pieces, key=len, reverse=True):
            if len(piece) >= 8:
                text = text.replace(piece, "<redacted>")
    return text


def cs(addr):
    return Web3.to_checksum_address(addr)


WETH = cs("0x4200000000000000000000000000000000000006")
USDC = cs("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
UNI_V3_FACTORY = cs("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
UNI_V3_QUOTER = cs("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
UNI_V3_ROUTER = cs("0x2626664c2603336E57B271c5C0b26F421741e481")  # SwapRouter02
AERO_FACTORY = cs("0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
AERO_ROUTER = cs("0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")
GAS_ORACLE = cs("0x420000000000000000000000000000000000000F")

# Aerodrome Slipstream (concentrated liquidity): the three live CL factories listed by Aerodrome's FactoryRegistry
# (0x5C3F18F06CC09CA1910767A34a20F771039E37C0, poolFactories()), each with its own QuoterV2 and SwapRouter.
# A quoter derives pool addresses from ITS OWN factory: asked about a tick spacing that several factories share,
# it silently prices its own factory's pool (verified: 1 WETH @ ts50 = $2,060 on CL1 vs $2,429 on CL3).
# So every pool is keyed (factory, tickSpacing) and always uses its own factory's quoter and router.
SLIPSTREAM_DEPLOYMENTS = [
    {"name": "CL1", "factory": cs("0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"),     # initial (2024)
     "quoter": cs("0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0"),
     "router": cs("0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5")},
    {"name": "CL2", "factory": cs("0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a"),     # gauge caps (2025)
     "quoter": cs("0x3d4C22254F86f64B7eC90ab8F7aeC1FBFD271c6C"),
     "router": cs("0xcbBb8035cAc7D4B3Ca7aBb74cF7BdF900215Ce0D")},
    {"name": "CL3", "factory": cs("0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef"),     # gauges v3 (2026)
     "quoter": cs("0x514c8B5f54112481E28028F1166Bd78501089259"),
     "router": cs("0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F")},
]

# Gas ceiling for a single quote eth_call. Concentrated-liquidity quotes walk ticks and get expensive in thin or
# wide pools; a quote that would exceed this is treated as "no quote" (0), never a crash.
QUOTE_GAS = 10_000_000

# Concentrated-liquidity price bounds (TickMath MIN_SQRT_RATIO / MAX_SQRT_RATIO). If a quote's final price lands on
# a bound, the swap ran out of liquidity and only part of amount_in was used: the quote is a partial fill, so reject it.
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

# Slipstream fee modules add a same-block surcharge after a pool is traded (e.g. 0.015% -> 0.06%), which is gone by
# the next block. Our transaction can only land in the next block, so CL quotes run in next-block context.
NEXT_BLOCK_SECONDS = 2   # Base block time
ZERO = "0x0000000000000000000000000000000000000000"

UNI_FEE_TIERS = [100, 500, 3000, 10000]  # in hundredths of a bip: 0.01%, 0.05%, 0.3%, 1%

# Why a token got its verdict. MALICIOUS_CODES are properties of the token itself (safe to cache as "bad");
# the rest are about the environment or market right now and must be re-checked next time.
REASON_CODES = {
    "OK": "passed every check",
    "LIQUIDITY": "no pool clears the per-pool liquidity floor (market condition, re-check later)",
    "LIVE_RPC": "live RPC can't run the fork checks (environment, not the token)",
    "INFRA": "test setup failed, e.g. funding the test wallet (environment, not the token)",
    "HONEYPOT": "a buy, transfer or sell reverted or paid out nothing",
    "TAX": "a buy, transfer or sell tax above the limit",
    "PROXY": "upgradeable proxy with a live admin",
    "PROXY_PIN_MISMATCH": "allowlisted issuer proxy whose controller chain no longer matches its pin (blocked, "
                          "re-checked every cycle so a human re-pin takes effect at once)",
    "OWNER_POWER": "a controller can switch on a rule that breaks trading",
}
MALICIOUS_CODES = {"HONEYPOT", "TAX", "PROXY", "OWNER_POWER"}

# Trusted upgradeable proxies: an upgradeable token normally gets DYNAMIC_UNSAFE (its admin can swap in any code).
# These institutional issuers are allowed through with a PROXY_WARNING instead, because a same-block (atomic) arb
# can only be hurt by an upgrade that lands in our block ahead of us. Each entry pins the TOKEN address together with
# its full controller chain (scanner's controllers + each controller's own owner), read on-chain and attributed to the
# issuer. A controller address alone is never trusted: anyone can deploy a proxy that names Coinbase's admin.
# If the live chain differs from the pin in any way (admin rotated, owner changed, new controller), the token falls
# back to DYNAMIC_UNSAFE until a human re-verifies and updates the pin.
TRUSTED_PROXIES = {
    cs("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"): {  # cbBTC
        "issuer": "Coinbase", "symbol": "cbBTC", "confidence": "high", "verified_block": 52689443,
        "evidence": (
            "Same owner/admin EOAs as Coinbase's documented L1 cbBTC; basescan 'Coinbase: cbBTC Token'. "
            "Both controllers are single-key EOAs: instant upgrades."),
        "controllers": {
            "0xbbc9b6001cf0a4FCcfFAB7bDda1ab16cafb9dB4F": "owner()",
            "0x42D90b3faF2287475cdc863706Bd7cc3E938C2Af": "admin()",
        },
    },
    cs("0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22"): {  # cbETH
        "issuer": "Coinbase", "symbol": "cbETH", "confidence": "medium", "verified_block": 52689443,
        "evidence": (
            "Deployed by basescan-labelled 'Coinbase: Deployer', admin handed to 3/6 Safe that also "
            "administers USDbC. No official doc names the Safe. No timelock."),
        "controllers": {
            "0xd94E416cf2c7167608B2515B7e4102B41efff94f": "admin()",
            "0xf9e320f3dA12E68af219d9E2A490Dd649f6B177c": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0xB011a32ED8b4F70D9943A2199F539bbeCd7b62F7": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0x3Dad2200849925Bb46d9bF05aFa5f7F213F4c18E": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0x5FbEFA105bbd53b43bf537Cbc5cD30804Dd0c993": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0x3cd692eCE8b6573A2220ae00d0dEb98f0DfFA9a1": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0x6CD3850756b7894774Ab715D136F9dD02837De50": "signer of Safe 0xd94E416cf2c7167608B2515B7e4102B41efff94f",
            "0xd94E416cf2c7167608B2515B7e4102B41efff94f#threshold=3": "Safe threshold",
            "0xd94E416cf2c7167608B2515B7e4102B41efff94f#modules=none": "Safe modules (can bypass the threshold)",
            "0xd94E416cf2c7167608B2515B7e4102B41efff94f#guard=none": "Safe guard",
        },
    },
    cs("0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452"): {  # wstETH
        "issuer": "Lido", "symbol": "wstETH", "confidence": "high", "verified_block": 52689443,
        "evidence": (
            "docs.lido.fi/deployed-contracts (Base): wstETH proxy + Base Governance Bridge Executor; "
            "executor answers to Lido Aragon Agent on L1. L2 delay 0."),
        "controllers": {
            "0x0E37599436974a25dDeEdF795C848d30Af46eaCF": "proxy admin slot",
            "0x0E37599436974a25dDeEdF795C848d30Af46eaCF#delay=0": "getDelay() of 0x0E37599436974a25dDeEdF795C848d30Af46eaCF",
            "0x3e40D73EB977Dc6a537aF587D48316feE66E9C8c": "getEthereumGovernanceExecutor() of 0x0E37599436974a25dDeEdF7",
        },
    },
    cs("0x04C0599Ae5A44757c0af6F9eC3b93da8976c150A"): {  # weETH
        "issuer": "ether.fi", "symbol": "weETH", "confidence": "medium", "verified_block": 52689443,
        "evidence": (
            "etherfi docs list the token; deployer 'ether.fi: Deployer 1' handed ProxyAdmin + ownership "
            "to EtherFiTimelock (3-day). Controller addresses not named in docs."),
        "controllers": {
            "0x851Dd540f4D2Ec78120De0a0cc87B21EdE5Df5C6": "owner()",
            "0x851Dd540f4D2Ec78120De0a0cc87B21EdE5Df5C6#minDelay=259200": "getMinDelay() of 0x851Dd540f4D2Ec78120De0a0cc87B21EdE5Df5C6",
            "0x2F6f3cc4a275C7951FB79199F01eD82421eDFb68": "proxy admin slot",
        },
    },
    cs("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842"): {  # MORPHO
        "issuer": "Morpho", "symbol": "MORPHO", "confidence": "high", "verified_block": 52689443,
        "evidence": (
            "docs.morpho.org/learn/governance/organization: morpho.eth 5/9 multisig on Base = owner(). No"
            " timelock."),
        "controllers": {
            "0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa": "owner()",
            "0x69FcEFDe2B48503d675181448B3D4272128bca9c": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0x30E7c016fC702cDe9A50720a469d418490b7b652": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0xe0aeb6811d33Df42A09066857CDaFca16b506086": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0x84D3E4EE550DD5F99e76a548aC59a6BE1C8dCf79": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0xC100c251bdD297A66795112f04356E6BA5f89D80": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0x264c86DBbD2E4165FbBf0C35b0ddf0e00AEc6b31": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0x8f02b4a44Eacd9b8eE7739aa0BA58833DD45d002": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0xCF263cEe139763114fAaFC5F52865135412F50Ec": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0x13cA8756E9470b71B8e998352c8741706217f963": "signer of Safe 0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa",
            "0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa#threshold=5": "Safe threshold",
            "0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa#modules=none": "Safe modules (can bypass the threshold)",
            "0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa#guard=none": "Safe guard",
        },
    },
}

# Used only on a live RPC, where the fork simulation can't run.
VERIFIED_TOKENS = {WETH, USDC}

# Base routing assets: every token pool must be paired against one of these. address -> (symbol, decimals)
BASES = {USDC: ("USDC", 6), WETH: ("WETH", 18)}
ANCHOR_FEE_TIER = 500           # the trusted WETH/USDC pool used to convert between bases (Uniswap V3 0.05%)

# Gas model for an arb through a custom contract: fixed overhead (tx, flash loan, checks) + a cost per swap.
# 2 swaps = 300k (same as the old flat estimate), 3 swaps = 420k, 4 swaps = 540k.
GAS_OVERHEAD = 60_000
GAS_PER_SWAP = 120_000
# Signed tx size (for the L1 data fee): base calldata + one encoded hop per swap. 2 swaps = 400 bytes.
TX_BYTES_BASE = 250
TX_BYTES_PER_SWAP = 75

# Fork simulation sizes.
SIM_WRAP_ETH = 10**17          # 0.1 ETH wrapped to WETH (and swapped to USDC if the test pool is USDC-based)
SIM_BUY_USD = 20               # $20 test buy, in the test pool's base asset


def fn(name, inputs, outputs, mutability="view"):
    return {"name": name, "type": "function", "stateMutability": mutability,
            "inputs": [{"type": t, "name": f"a{i}"} for i, t in enumerate(inputs)],
            "outputs": [{"type": t, "name": f"r{i}"} for i, t in enumerate(outputs)]}


ERC20_ABI = [fn("decimals", [], ["uint8"]), fn("symbol", [], ["string"]), fn("totalSupply", [], ["uint256"]),
             fn("balanceOf", ["address"], ["uint256"]),
             fn("approve", ["address", "uint256"], ["bool"], "nonpayable"),
             fn("transfer", ["address", "uint256"], ["bool"], "nonpayable"),
             fn("deposit", [], [], "payable")]
UNI_FACTORY_ABI = [fn("getPool", ["address", "address", "uint24"], ["address"])]
UNI_POOL_ABI = [fn("liquidity", [], ["uint128"]), fn("token0", [], ["address"]),
                fn("slot0", [], ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"])]
UNI_QUOTER_ABI = [{
    "name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}],
}]
UNI_ROUTER_ABI = [{
    "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
AERO_FACTORY_ABI = [fn("getPool", ["address", "address", "bool"], ["address"]),
                    fn("getFee", ["address", "bool"], ["uint256"])]
AERO_POOL_ABI = [fn("getAmountOut", ["uint256", "address"], ["uint256"])]
AERO_ROUTER_ABI = [{
    "name": "swapExactTokensForTokensSupportingFeeOnTransferTokens", "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
               {"name": "routes", "type": "tuple[]", "components": [
                   {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                   {"name": "stable", "type": "bool"}, {"name": "factory", "type": "address"}]},
               {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
    "outputs": [],
}]
GAS_ORACLE_ABI = [fn("getL1FeeUpperBound", ["uint256"], ["uint256"])]
CL_FACTORY_ABI = [fn("getPool", ["address", "address", "int24"], ["address"]), fn("tickSpacings", [], ["int24[]"])]
CL_POOL_ABI = [fn("liquidity", [], ["uint128"]), fn("fee", [], ["uint24"]), fn("factory", [], ["address"])]
CL_QUOTER_ABI = [{
    "name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "tickSpacing", "type": "int24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}],
}]
CL_ROUTER_ABI = [{
    "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "tickSpacing", "type": "int24"}, {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"}, {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"}, {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]


@dataclass
class Pool:
    label: str
    kind: str          # "univ3", "aero" (v2 legacy) or "cl" (Slipstream)
    address: str
    fee: float         # swap fee as a fraction, e.g. 0.003 = 0.3%
    uni_fee_tier: int = 0
    stable: bool = False
    liquidity_usd: float = 0.0
    base: str = USDC   # the base asset this pool pairs the token against (USDC or WETH)
    tick_spacing: int = 0          # Slipstream only
    quoter: str = ""               # Slipstream only: the quoter of this pool's factory generation
    router: str = ""               # Slipstream only: the router of this pool's factory generation


@dataclass
class Context:
    w3: Web3
    token: str              # the long-tail / volatile token
    token_dec: int
    token_sym: str
    gas_cost_usdc: float    # estimated gas for a 2-swap trade, in USD (used if no GasModel is attached)
    rpc_url: str = RPC_URL
    pools: list = field(default_factory=list)
    max_tax: float = 0.005  # reject anything that loses more than 0.5% on buy, transfer or sell
    # Filled in by the fork simulation (fractions, e.g. 0.05 = 5%). None = not measured.
    buy_tax: Optional[float] = None
    transfer_tax: Optional[float] = None
    sell_tax: Optional[float] = None
    # Filled in by is_safe_token: "SAFE", "PROXY_WARNING", "UNSAFE" or "DYNAMIC_UNSAFE", plus human-readable notes.
    verdict: Optional[str] = None
    reason: str = ""           # human-readable why
    reason_code: str = ""      # machine-readable: see REASON_CODES
    proxy_warning: str = ""    # issuer name when an allowlisted upgradeable proxy was let through
    controllers: list = field(default_factory=list)   # owner / admin addresses that can still act
    warnings: list = field(default_factory=list)      # non-blocking flags
    findings: list = field(default_factory=list)      # blocking owner powers proven on the fork
    eth_usd: float = 0.0    # anchor mid price at the current block; 0 = look it up when needed
    gas: Optional["GasModel"] = None   # per-swap-count gas costs; None = use gas_cost_usdc for every route


def is_local_rpc(url):
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------- pool discovery

def base_symbol(base):
    return BASES[base][0]


def anchor_pool(w3):
    """The trusted deep WETH/USDC pool (Uniswap V3 0.05%) used for base conversions and the ETH price."""
    addr = w3.eth.contract(address=UNI_V3_FACTORY, abi=UNI_FACTORY_ABI).functions.getPool(
        WETH, USDC, ANCHOR_FEE_TIER).call()
    return Pool("anchor UniV3 0.05%", "univ3", addr, ANCHOR_FEE_TIER / 1_000_000,
                uni_fee_tier=ANCHOR_FEE_TIER, base=USDC)


def eth_usd_mid(w3):
    """USDC per WETH at the anchor's current price (slot0), with no fee or price impact applied."""
    anchor = w3.eth.contract(address=anchor_pool(w3).address, abi=UNI_POOL_ABI)
    sqrt_p = anchor.functions.slot0().call()[0]
    raw = (sqrt_p / 2**96) ** 2                      # token1 per token0, smallest units
    if anchor.functions.token0().call() == WETH:      # token0 = WETH, token1 = USDC
        return raw * 10**(18 - 6)
    return 1 / (raw * 10**(6 - 18))


def usd_price(base, eth_usd):
    return 1.0 if base == USDC else eth_usd


def usd_to_base_units(amount_usd, base, eth_usd):
    """Convert a USD amount to the base asset's smallest units at the anchor mid price."""
    sym, dec = BASES[base]
    return int(amount_usd / usd_price(base, eth_usd) * 10**dec)


def find_pools(w3, token):
    """Every Uniswap V3 (all fee tiers) and Aerodrome v2 pool pairing the token with USDC or WETH."""
    pools = []
    uni_factory = w3.eth.contract(address=UNI_V3_FACTORY, abi=UNI_FACTORY_ABI)
    aero_factory = w3.eth.contract(address=AERO_FACTORY, abi=AERO_FACTORY_ABI)
    for base in BASES:
        if base == token:
            continue
        bsym = base_symbol(base)
        for tier in UNI_FEE_TIERS:
            addr = uni_factory.functions.getPool(token, base, tier).call()
            if addr == ZERO:
                continue
            if w3.eth.contract(address=addr, abi=UNI_POOL_ABI).functions.liquidity().call() == 0:
                continue
            pools.append(Pool(f"UniV3 {tier / 10000:.2f}% /{bsym}", "univ3", addr, tier / 1_000_000,
                              uni_fee_tier=tier, base=base))
        for stable in (False, True):
            addr = aero_factory.functions.getPool(token, base, stable).call()
            if addr == ZERO:
                continue
            try:
                fee = aero_factory.functions.getFee(addr, stable).call() / 10_000
            except Exception:
                fee = 0.0005 if stable else 0.003
            pools.append(Pool(f"Aero {'sAMM' if stable else 'vAMM'} {fee * 100:.2f}% /{bsym}", "aero", addr, fee,
                              stable=stable, base=base))
        pools += find_slipstream_pools(w3, token, base)
    return pools


def find_slipstream_pools(w3, token, base):
    """Every Slipstream pool for token/base across every factory generation and every enabled tick spacing."""
    pools = []
    for dep in SLIPSTREAM_DEPLOYMENTS:
        factory = w3.eth.contract(address=dep["factory"], abi=CL_FACTORY_ABI)
        for ts in factory.functions.tickSpacings().call():
            addr = factory.functions.getPool(token, base, ts).call()
            if addr == ZERO:
                continue
            pool = w3.eth.contract(address=addr, abi=CL_POOL_ABI)
            if pool.functions.liquidity().call() == 0:
                continue
            fee = pool.functions.fee().call() / 1_000_000      # current fee; Slipstream fees can be dynamic
            pools.append(Pool(f"Aero {dep['name']}-ts{ts} {fee * 100:.2f}% /{base_symbol(base)}", "cl", addr, fee,
                              base=base, tick_spacing=ts, quoter=dep["quoter"], router=dep["router"]))
    return pools


def pool_liquidity_usd(w3, pool, token, eth_usd):
    """USD depth of ONE pool: 2 x the smaller side, so a pool drained on either side reads as thin.

    Base side = base balance x its USD price (USDC = $1, WETH = anchor mid price).
    Token side is priced with a $1-of-base probe quote on this same pool. For Uniswap V3 this counts all
    tokens held by the pool (including out-of-range liquidity), so treat it as an upper bound.
    """
    sym, dec = BASES[pool.base]
    base_bal = erc20(w3, pool.base).functions.balanceOf(pool.address).call()
    base_side = base_bal / 10**dec * usd_price(pool.base, eth_usd)
    token_bal = erc20(w3, token).functions.balanceOf(pool.address).call()
    tokens_per_usd = quote(w3, pool, pool.base, token, usd_to_base_units(1, pool.base, eth_usd))
    token_side = token_bal / tokens_per_usd if tokens_per_usd else 0.0
    return 2 * min(base_side, token_side)


def filter_pools_by_liquidity(w3, pools, token, min_liquidity_usd, eth_usd=None):
    """Per-pool rule: every pool in a route must clear the floor on its own, whatever its base asset."""
    eth_usd = eth_usd or eth_usd_mid(w3)
    kept = []
    for p in pools:
        p.liquidity_usd = pool_liquidity_usd(w3, p, token, eth_usd)
        ok = p.liquidity_usd >= min_liquidity_usd
        print(f"  {p.label:<30} ~${p.liquidity_usd:>16,.0f}  {'ok' if ok else 'DROPPED (too thin)'}")
        if ok:
            kept.append(p)
    return kept


# ---------------------------------------------------------------- quoting

def quote_request(w3, pool, token_in, token_out, amount_in):
    """(target, calldata) of the eth_call that quotes this swap. The one place quote encoding lives: quote() sends it
    alone, gap_monitor.py batches many of them through Multicall3."""
    if pool.kind == "univ3":
        fn_call = w3.eth.contract(address=UNI_V3_QUOTER, abi=UNI_QUOTER_ABI).functions.quoteExactInputSingle(
            (token_in, token_out, amount_in, pool.uni_fee_tier, 0))
    elif pool.kind == "cl":
        fn_call = w3.eth.contract(address=pool.quoter, abi=CL_QUOTER_ABI).functions.quoteExactInputSingle(
            (token_in, token_out, amount_in, pool.tick_spacing, 0))
    else:
        fn_call = w3.eth.contract(address=pool.address, abi=AERO_POOL_ABI).functions.getAmountOut(amount_in, token_in)
    return fn_call.address, fn_call._encode_transaction_data()


def decode_quote(w3, pool, raw):
    """Amount out from a quote's raw return data; 0 for a partial fill (the pool ran dry before taking all input)."""
    if pool.kind in ("univ3", "cl"):
        amount_out, sqrt_after = w3.codec.decode(_QUOTE_OUT_TYPES, bytes(raw))[:2]
        if sqrt_after <= MIN_SQRT_RATIO + 1 or sqrt_after >= MAX_SQRT_RATIO - 1:
            return 0
        return amount_out
    return w3.codec.decode(["uint256"], bytes(raw))[0]


def quote(w3, pool, token_in, token_out, amount_in):
    """Exact output (smallest units) for swapping amount_in through one pool. Includes fee + price impact."""
    if amount_in <= 0:
        return 0
    try:
        target, data = quote_request(w3, pool, token_in, token_out, amount_in)
        if pool.kind == "cl":
            raw = call_next_block(w3, target, data)
        elif pool.kind == "univ3":
            raw = w3.eth.call({"to": target, "data": data, "gas": QUOTE_GAS})
        else:
            raw = w3.eth.call({"to": target, "data": data})
        return decode_quote(w3, pool, raw)
    except Exception:
        return 0  # quote reverted or ran out of QUOTE_GAS, e.g. not enough liquidity for this size


_QUOTE_OUT_TYPES = ["uint256", "uint160", "uint32", "uint256"]
_block_overrides_ok = {}   # per provider: does this RPC accept eth_call block overrides?


def next_block_overrides(block):
    """eth_call block overrides for 'the block after `block`' (number + 1, time + one Base block)."""
    return {"number": hex(block.number + 1), "time": hex(block.timestamp + NEXT_BLOCK_SECONDS)}


def call_next_block(w3, target, data, gas=QUOTE_GAS):
    """Raw eth_call as if in the next block, so per-block dynamic fees match what a transaction sent now would pay.
    Falls back to a plain 'latest' call if the RPC lacks block overrides."""
    key = id(w3.provider)
    if _block_overrides_ok.get(key, True):
        tx = {"to": target, "data": data, "gas": hex(gas)}
        resp = w3.provider.make_request("eth_call", [tx, "latest", {}, next_block_overrides(w3.eth.get_block("latest"))])
        if "result" in resp:
            return bytes.fromhex(resp["result"][2:])
        msg = str(resp.get("error", {}).get("message", "")).lower()
        if "revert" in msg or "out of gas" in msg or "gas required" in msg:
            raise RuntimeError(msg)            # a real quote failure -> caller returns 0
        _block_overrides_ok[key] = False       # RPC doesn't understand block overrides: stop trying
        print("  (warning: RPC has no eth_call block overrides; Slipstream quotes use 'latest' fees)")
    return w3.eth.call({"to": target, "data": data, "gas": gas})


def price_impact(w3, pool, token_in, token_out, amount_in, amount_out):
    """How much worse the real fill is than a tiny 'probe' trade scaled up (fees cancel out)."""
    probe_in = max(amount_in // 1000, 1)
    probe_out = quote(w3, pool, token_in, token_out, probe_in)
    if probe_out == 0:
        return None
    ideal_out = probe_out * amount_in / probe_in
    return 1 - amount_out / ideal_out


def build_legs(pool_a, pool_b, token, anchor, settle="native"):
    """The swap sequence for one cycle. Returns (start/end asset, [(pool, token_in, token_out), ...]).

    settle="native": start in pool A's base, so same-base cycles are 2 swaps and mixed-base cycles close
    through the anchor (3 swaps). settle="usdc": always start and end in USDC (WETH legs add anchor hops).
    """
    start = pool_a.base if settle == "native" else USDC
    legs = []
    if start != pool_a.base:
        legs.append((anchor, start, pool_a.base))
    legs.append((pool_a, pool_a.base, token))
    legs.append((pool_b, token, pool_b.base))
    if pool_b.base != start:
        legs.append((anchor, pool_b.base, start))
    return start, legs


def after_tax(amount_out, token_in, token_out, token, buy_tax, sell_tax):
    """Apply the token's measured buy/sell tax (from the fork sim) to one leg's quoted output. Pool quotes never run
    the token's transfer code, so without this a taxed token would look more profitable than it is."""
    if token_out == token:
        return int(amount_out * (1 - max(buy_tax or 0, 0)))
    if token_in == token:
        return int(amount_out * (1 - max(sell_tax or 0, 0)))
    return amount_out


def calculate_net_out(amount_in, pool_a, pool_b, ctx, settle="native"):
    """Simulate one full cycle through pool_a then pool_b (plus anchor hops when bases differ).

    amount_in is in USD (e.g. 500). It is converted to the start asset at the anchor mid price at the
    current block (exact WETH equivalent, no fee). Each swap's exact output feeds the next swap, so fees
    and price impact are inside the quotes. Gas for this route's swap count is subtracted at the end.
    """
    w3 = ctx.w3
    eth_usd = ctx.eth_usd or eth_usd_mid(w3)
    anchor = anchor_pool(w3)
    start, legs = build_legs(pool_a, pool_b, ctx.token, anchor, settle)
    start_sym, start_dec = BASES[start]
    start_units = usd_to_base_units(amount_in, start, eth_usd)

    amount, impacts, path = start_units, [], [start_sym]
    for pool, t_in, t_out in legs:
        out = quote(w3, pool, t_in, t_out, amount)
        impacts.append(price_impact(w3, pool, t_in, t_out, amount, out) if out else None)
        out = after_tax(out, t_in, t_out, ctx.token, ctx.buy_tax, ctx.sell_tax)
        path.append(ctx.token_sym if t_out == ctx.token else base_symbol(t_out))
        amount = out
        if out == 0:
            break
    end_units = amount
    ok = end_units > 0

    price = usd_price(start, eth_usd)
    gross_out = end_units / 10**start_dec * price                  # USD value of what came back
    hops = len(legs)
    gas_usd = ctx.gas.cost_usd(hops) if ctx.gas else ctx.gas_cost_usdc
    known = [i for i in impacts if i is not None]
    return {
        "route": f"{pool_a.label} -> {pool_b.label}",
        "path": " > ".join(path),
        "hops": hops,
        "start": start_sym,
        "start_asset": start,
        "legs": legs,           # [(Pool, token_in, token_out)], for re-running the route on a fork
        "pool_a": pool_a,
        "pool_b": pool_b,
        "amount_in": amount_in,
        "start_units": start_units,
        "end_units": end_units,
        "gross_out": gross_out,
        "swap_fees_usdc": amount_in * sum(p.fee for p, _, _ in legs),  # info only, already in quotes
        "impacts": impacts,
        "max_impact": max(known) if known else None,
        "gas_usdc": gas_usd,
        "net_out": gross_out - gas_usd,
        "net_profit": (end_units - start_units) / 10**start_dec * price - gas_usd,
        "ok": ok,
    }


# ---------------------------------------------------------------- trade size search

SEARCH_SIZES = (100, 250, 500, 1000, 2500, 5000)   # USD, coarse grid tried for every searched route
SEARCH_TOP_ROUTES = 3                               # how many of a token's best routes (at --amount) get searched
SEARCH_REFINE_ROUNDS = 2                            # each round tries the geometric midpoints next to the best size


def search_best_size(pool_a, pool_b, ctx, settle="native", sizes=SEARCH_SIZES, rounds=SEARCH_REFINE_ROUNDS):
    """Net profit across trade sizes for one route: the coarse grid, then refined around the best size.

    Small sizes are dominated by gas, big ones by price impact; the best is usually in between. Refinement only looks
    between already-tried neighbours (never beyond the grid). Returns {"best": calculate_net_out result at the best
    size (None if every size failed), "curve": {usd_size: net_profit or None}}.
    """
    tried = {}

    def run(size):
        size = round(size, 2)
        if size not in tried:
            tried[size] = calculate_net_out(size, pool_a, pool_b, ctx, settle)
        return tried[size]

    for size in sizes:
        run(size)
    for _ in range(rounds):
        ok = [x for x in sorted(tried) if tried[x]["ok"]]
        if not ok:
            break
        best = max(ok, key=lambda x: tried[x]["net_profit"])
        ordered = sorted(tried)
        i = ordered.index(best)
        for j in (i - 1, i + 1):
            if 0 <= j < len(ordered):
                run((best * ordered[j]) ** 0.5)
    ok = [r for r in tried.values() if r["ok"]]
    best = max(ok, key=lambda r: r["net_profit"]) if ok else None
    return {"best": best, "curve": {x: (tried[x]["net_profit"] if tried[x]["ok"] else None) for x in sorted(tried)}}


# ---------------------------------------------------------------- gas

@dataclass
class GasModel:
    """Gas cost per swap count: (overhead + per_swap x swaps) x L2 gas price + L1 data fee for that tx size."""
    w3: Web3
    l2_price_wei: int        # block base fee + our tip
    eth_usd: float
    overhead: int = GAS_OVERHEAD
    per_swap: int = GAS_PER_SWAP
    _l1_cache: dict = field(default_factory=dict)

    def units(self, swaps):
        return self.overhead + self.per_swap * swaps

    def l1_fee_wei(self, swaps):
        if swaps not in self._l1_cache:
            size = TX_BYTES_BASE + TX_BYTES_PER_SWAP * swaps
            try:
                self._l1_cache[swaps] = self.w3.eth.contract(
                    address=GAS_ORACLE, abi=GAS_ORACLE_ABI).functions.getL1FeeUpperBound(size).call()
            except Exception:
                print("  (warning: could not read L1 data fee; gas may be underestimated)")
                self._l1_cache[swaps] = 0
        return self._l1_cache[swaps]

    def cost_wei(self, swaps):
        return self.units(swaps) * self.l2_price_wei + self.l1_fee_wei(swaps)

    def cost_usd(self, swaps):
        return self.cost_wei(swaps) / 10**18 * self.eth_usd


def build_gas_model(w3, priority_gwei, eth_usd, overhead=GAS_OVERHEAD, per_swap=GAS_PER_SWAP):
    # Block base fee + our own tip. (eth_gasPrice on anvil adds a 1 gwei tip, ~100x what Base needs.)
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0)
    return GasModel(w3, base_fee + int(priority_gwei * 10**9), eth_usd, overhead, per_swap)


# ---------------------------------------------------------------- fork simulation helpers

def erc20(w3, addr):
    return w3.eth.contract(address=addr, abi=ERC20_ABI)


def send(w3, call, sender, value=0):
    """Send a local test transaction on the fork. Raises if it reverts."""
    tx_hash = call.transact({"from": sender, "value": value})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status != 1:
        raise RuntimeError("transaction reverted")
    return receipt


def swap_on_fork(w3, pool, token_in, token_out, amount_in, sender):
    """Real swap through the pool's router on the fork. Aerodrome uses its fee-on-transfer-safe path."""
    if pool.kind == "univ3":
        send(w3, erc20(w3, token_in).functions.approve(UNI_V3_ROUTER, amount_in), sender)
        router = w3.eth.contract(address=UNI_V3_ROUTER, abi=UNI_ROUTER_ABI)
        send(w3, router.functions.exactInputSingle(
            (token_in, token_out, pool.uni_fee_tier, sender, amount_in, 0, 0)), sender)
    elif pool.kind == "cl":
        send(w3, erc20(w3, token_in).functions.approve(pool.router, amount_in), sender)
        router = w3.eth.contract(address=pool.router, abi=CL_ROUTER_ABI)
        deadline = w3.eth.get_block("latest").timestamp + 600
        send(w3, router.functions.exactInputSingle(
            (token_in, token_out, pool.tick_spacing, sender, deadline, amount_in, 0, 0)), sender)
    else:
        send(w3, erc20(w3, token_in).functions.approve(AERO_ROUTER, amount_in), sender)
        router = w3.eth.contract(address=AERO_ROUTER, abi=AERO_ROUTER_ABI)
        deadline = w3.eth.get_block("latest").timestamp + 600
        send(w3, router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_in, 0, [(token_in, token_out, pool.stable, AERO_FACTORY)], sender, deadline), sender)


def is_onchain_revert(err):
    """True only for evidence from the chain itself: a reverted receipt or an execution revert from the node.
    Timeouts, HTTP 429s, dropped connections etc. say nothing about the token and must not become HONEYPOT."""
    if isinstance(err, ContractLogicError):
        return True
    text = str(err).lower()
    return "transaction reverted" in text or "execution reverted" in text or ("revert" in text and "429" not in text)


def _sim_failure(prefix, err):
    if not is_onchain_revert(err):
        raise err            # infra problem: let the caller report ERROR (never blacklisted)
    return False, f"{prefix} ({str(err)[:80]})"


def shortfall(expected, received):
    """Fraction lost versus what we expected, e.g. 0.05 = 5% taken. Negative means we got extra."""
    return 1 - received / expected if expected else None


def simulate_all_pools(token, ctx, quiet=False):
    """Run buy -> transfer -> sell on EVERY qualifying pool, deepest first. Any pool failing fails the token.

    Routes can sell into any pool in ctx.pools, and a token can trap just one of them (e.g. block sells into its
    USDC pair only). Quotes never run the token's transfer code, so only a real swap per pool can catch that.
    ctx.buy_tax / transfer_tax / sell_tax end up as the worst value seen across pools.
    """
    worst = {"buy_tax": None, "transfer_tax": None, "sell_tax": None}
    for pool in sorted(ctx.pools, key=lambda p: p.liquidity_usd, reverse=True):
        passed, reason = run_fork_simulation(token, ctx, quiet=quiet, test_pool=pool)
        for k in worst:
            v = getattr(ctx, k)
            if v is not None and (worst[k] is None or v > worst[k]):
                worst[k] = v
        if not passed:
            return False, f"{reason} [on {pool.label}]"
    for k, v in worst.items():
        setattr(ctx, k, v)
    n = len(ctx.pools)
    return True, f"buy, transfer and sell passed on all {n} pool{'s' if n != 1 else ''} with tax under the limit"


def run_fork_simulation(token, ctx, quiet=False, test_pool=None):
    """Buy -> transfer -> sell on one pool (default: the deepest). Fills ctx.buy_tax / transfer_tax / sell_tax.

    Returns (passed, reason). All state changes are rolled back with evm_snapshot / evm_revert.
    """
    log = (lambda *a, **k: None) if quiet else print
    w3 = ctx.w3
    ctx.buy_tax = ctx.transfer_tax = ctx.sell_tax = None
    test_pool = test_pool or max(ctx.pools, key=lambda p: p.liquidity_usd)
    alice, bob = w3.eth.accounts[0], w3.eth.accounts[1]
    base = test_pool.base
    bsym, bdec = BASES[base]
    tok, base_tok = erc20(w3, token), erc20(w3, base)
    buy_units = usd_to_base_units(SIM_BUY_USD, base, ctx.eth_usd or eth_usd_mid(w3))
    log(f"  Fork simulation on {test_pool.label} (wallets {alice[:8]}…, {bob[:8]}…)")

    snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        # Fund alice with the test pool's base: wrap ETH -> WETH (-> USDC through the anchor if needed).
        w3.provider.make_request("anvil_setBalance", [alice, hex(10**20)])
        w3.provider.make_request("anvil_setBalance", [bob, hex(10**20)])
        send(w3, erc20(w3, WETH).functions.deposit(), alice, value=SIM_WRAP_ETH)
        if base == USDC:
            swap_on_fork(w3, anchor_pool(w3), WETH, USDC, SIM_WRAP_ETH, alice)
        if base_tok.functions.balanceOf(alice).call() < buy_units:
            return False, f"could not fund the test wallet with {bsym}"

        # a) Buy test
        expected = quote(w3, test_pool, base, token, buy_units)
        before = tok.functions.balanceOf(alice).call()
        try:
            swap_on_fork(w3, test_pool, base, token, buy_units, alice)
        except Exception as e:
            return _sim_failure("HONEYPOT: buy reverted", e)
        bought = tok.functions.balanceOf(alice).call() - before
        ctx.buy_tax = shortfall(expected, bought)
        log(f"    a) buy      ok  got {bought / 10**ctx.token_dec:,.6f} {ctx.token_sym}  "
              f"(buy tax {pct(ctx.buy_tax)})")
        if bought == 0:
            return False, "HONEYPOT: buy succeeded but delivered 0 tokens"

        # b) Transfer test: alice -> bob, compare what left vs what arrived
        a0, b0 = tok.functions.balanceOf(alice).call(), tok.functions.balanceOf(bob).call()
        try:
            send(w3, tok.functions.transfer(bob, bought), alice)
        except Exception as e:
            return _sim_failure("HONEYPOT: wallet-to-wallet transfer reverted", e)
        sent = a0 - tok.functions.balanceOf(alice).call()
        received = tok.functions.balanceOf(bob).call() - b0
        ctx.transfer_tax = shortfall(sent, received)
        log(f"    b) transfer ok  sent {sent / 10**ctx.token_dec:,.6f}, received "
              f"{received / 10**ctx.token_dec:,.6f}  (transfer tax {pct(ctx.transfer_tax)})")

        # c) Sell test: bob sells everything he received back to the base asset
        expected = quote(w3, test_pool, token, base, received)
        u0 = base_tok.functions.balanceOf(bob).call()
        try:
            swap_on_fork(w3, test_pool, token, base, received, bob)
        except Exception as e:
            note = (" Concentrated-liquidity pools (Uniswap V3, Slipstream) also revert on any transfer-tax token."
                    if test_pool.kind in ("univ3", "cl") else "")
            failed = _sim_failure("HONEYPOT: sell reverted", e)
            return failed[0], failed[1] + note
        sold_for = base_tok.functions.balanceOf(bob).call() - u0
        ctx.sell_tax = shortfall(expected, sold_for)
        log(f"    c) sell     ok  got {sold_for / 10**bdec:,.6f} {bsym}  (sell tax {pct(ctx.sell_tax)})")
        if sold_for == 0:
            return False, f"HONEYPOT: sell succeeded but paid out 0 {bsym}"

        for name, tax in (("buy", ctx.buy_tax), ("transfer", ctx.transfer_tax), ("sell", ctx.sell_tax)):
            if tax is not None and tax > ctx.max_tax:
                return False, f"{name} tax {tax * 100:.3f}% is above the {ctx.max_tax * 100:.2f}% limit"
        return True, "buy, transfer and sell all passed with tax under the limit"
    finally:
        w3.provider.make_request("evm_revert", [snapshot])


# ---------------------------------------------------------------- contract structure (owner powers)

def selector(sig):
    """4-byte function selector, computed from the signature so we never hard-code a wrong hex value."""
    return Web3.keccak(text=sig)[:4].hex().removeprefix("0x")


def arg_types(sig):
    inner = sig[sig.index("(") + 1:-1]
    return [t for t in inner.split(",") if t]


FEE_VALUES = (9900, 99, 25)  # 99% in basis points, 99 as a plain percent, then 25 in case big values are capped

# (signature, kind, args to try). Args are a function of (victim wallet, controller) -> list of tuples.
# kind "mint" is a warning only (see is_safe_token); every other kind blocks the token if it works.
TAX_SIGNATURES = [
    "setTax(uint256)", "setFee(uint256)", "setFees(uint256,uint256)", "setTaxes(uint256,uint256)",
    "updateFees(uint256,uint256)", "updateFees(uint256,uint256,uint256)", "setBuyFee(uint256)",
    "setSellFee(uint256)", "setBuyTax(uint256)", "setSellTax(uint256)", "setTaxFeePercent(uint256)",
    "updateBuyFees(uint256,uint256,uint256)", "updateSellFees(uint256,uint256,uint256)",
]
PROBES = [
    ("pause()", "pause", lambda v, o: [()]),
    ("setPaused(bool)", "pause", lambda v, o: [(True,)]),
    ("blacklist(address)", "blacklist", lambda v, o: [(v,)]),
    ("blacklist(address,bool)", "blacklist", lambda v, o: [(v, True)]),
    ("setBlacklist(address,bool)", "blacklist", lambda v, o: [(v, True)]),
    ("addToBlacklist(address)", "blacklist", lambda v, o: [(v,)]),
    ("setBot(address,bool)", "blacklist", lambda v, o: [(v, True)]),
    ("setTradingEnabled(bool)", "trading", lambda v, o: [(False,)]),
    ("setMaxTxAmount(uint256)", "limits", lambda v, o: [(1,)]),
    ("setMaxWalletSize(uint256)", "limits", lambda v, o: [(1,)]),
    ("mint(address,uint256)", "mint", lambda v, o: [(o, 10**30)]),
    ("mint(uint256)", "mint", lambda v, o: [(10**30,)]),
] + [(sig, "tax", lambda v, o, n=len(arg_types(sig)): [(x,) * n for x in FEE_VALUES]) for sig in TAX_SIGNATURES]

# Selectors we only look for (never call).
SCAN_ONLY = [("owner()", "ownership"), ("renounceOwnership()", "ownership"), ("transferOwnership(address)", "ownership"),
             ("unpause()", "pause"), ("upgradeTo(address)", "upgrade"), ("upgradeToAndCall(address,bytes)", "upgrade")]

# Proxy storage slots: EIP-1967 (modern) and the older ZeppelinOS layout (used by USDC-style FiatToken proxies).
IMPL_SLOTS = ["0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
              "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"]
ADMIN_SLOTS = ["0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103",
               "0x10d6a54a4754c8869d6886b5f5d7fbfa5b4522237ea5c60d11bc4e7a1ff9390b"]


def push_values(code):
    """Every constant pushed by PUSH1..PUSH4, left-padded to 4 bytes. Solidity's function dispatcher
    compares calldata against these, so a selector in this set means the function exists.
    (Walking opcodes skips PUSH data, which avoids false matches from random bytes.)"""
    found, i = set(), 0
    while i < len(code):
        op = code[i]
        if 0x60 <= op <= 0x7f:                    # PUSH1..PUSH32
            n = op - 0x5f
            if n <= 4:
                found.add(code[i + 1:i + 1 + n].rjust(4, b"\0").hex())
            i += n
        i += 1
    return found


def slot_address(w3, addr, slot):
    raw = w3.eth.get_storage_at(addr, slot)
    a = "0x" + raw[-20:].hex().removeprefix("0x")
    return None if int(a, 16) == 0 else cs(a)


def scan_bytecode(w3, token):
    """Read-only. Returns (present signatures, proxy implementation or None)."""
    code = bytes(w3.eth.get_code(token))
    impl = next((a for a in (slot_address(w3, token, sl) for sl in IMPL_SLOTS) if a), None)
    if impl:
        code += bytes(w3.eth.get_code(impl))       # the real logic lives in the implementation
    pushed = push_values(code)
    sigs = [(s, k) for s, k, _ in PROBES] + SCAN_ONLY
    present = {s: k for s, k in sigs if selector(s) in pushed}
    return present, impl


def find_controllers(w3, token):
    """Addresses that can still change the contract: owner()/getOwner()/admin() and the EIP-1967 proxy admin."""
    found = []
    for sig in ("owner()", "getOwner()", "admin()"):
        addr = _as_address(_raw_call(w3, token, sig))     # revert -> no such controller; RPC failure -> raises
        if addr:
            found.append((sig, addr))
    for sl in ADMIN_SLOTS:
        admin = slot_address(w3, token, sl)
        if admin:
            found.append(("proxy admin slot", admin))
    unique = {}
    for how, addr in found:
        unique.setdefault(addr, how)
    return [(how, addr) for addr, how in unique.items()]


SAFE_GUARD_SLOT = "0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8"
SAFE_SENTINEL = "0x0000000000000000000000000000000000000001"
# Getters that point from a controller to whoever controls IT (ProxyAdmin owner, Lido executor's L1 agent, ...).
CHAIN_NEXT_GETTERS = ("owner()", "getOwner()", "getEthereumGovernanceExecutor()")


def _raw_call(w3, addr, sig, args_types=(), args=()):
    """eth_call that returns None only when the call REVERTED (the getter doesn't exist / doesn't apply).
    Transport failures (timeouts, 429s, dropped connections) are raised: a fingerprint with silently missing
    keys would look like a control change, and a missing module read would look like "no modules"."""
    data = "0x" + selector(sig) + (encode(list(args_types), list(args)).hex().removeprefix("0x") if args_types else "")
    try:
        return w3.eth.call({"to": addr, "data": data})
    except Exception as e:
        if is_onchain_revert(e):
            return None
        raise


def _as_address(raw):
    if raw is None or len(raw) < 32 or int.from_bytes(raw[:12], "big") != 0 or int.from_bytes(raw[:32], "big") == 0:
        return None
    return cs("0x" + raw[12:32].hex().removeprefix("0x"))


def _as_uint(raw):
    return int.from_bytes(raw[:32], "big") if raw is not None and len(raw) >= 32 else None


def controller_chain(w3, controllers, depth=2):
    """Fingerprint of everything that can act on the token, as {key: how it was found}.

    Keys are addresses (the scanner's controllers, whoever controls each of them via CHAIN_NEXT_GETTERS, and every
    Safe signer) plus settings that change who can act without changing an address: a Safe's threshold, modules and
    guard; a timelock's minimum delay; a governance executor's delay. Any change to any of these changes the set.
    """
    chain = {}
    frontier = [(addr, how) for how, addr in controllers]
    for _ in range(depth + 1):
        nxt = []
        for addr, how in frontier:
            if addr in chain:
                continue
            chain[addr] = how
            if len(w3.eth.get_code(addr)) == 0:
                continue                                         # EOA: nothing further to read
            owners_raw = _raw_call(w3, addr, "getOwners()")
            threshold = _as_uint(_raw_call(w3, addr, "getThreshold()"))
            if owners_raw is not None and threshold is not None:   # Gnosis Safe
                for signer in w3.codec.decode(["address[]"], owners_raw)[0]:
                    chain.setdefault(cs(signer), f"signer of Safe {addr}")
                chain[f"{addr}#threshold={threshold}"] = "Safe threshold"
                mods_raw = _raw_call(w3, addr, "getModulesPaginated(address,uint256)", ("address", "uint256"),
                                     (SAFE_SENTINEL, 50))
                if mods_raw is None:
                    mods_key = "UNREADABLE"            # never matches a pin: modules can bypass the threshold
                else:
                    page, cursor = w3.codec.decode(["address[]", "address"], mods_raw)
                    if cs(cursor) not in (cs(SAFE_SENTINEL), cs(ZERO)):
                        mods_key = "TRUNCATED"         # more than one page: can't prove the full list
                    else:
                        mods_key = ",".join(sorted(cs(m) for m in page)) or "none"
                chain[f"{addr}#modules={mods_key}"] = "Safe modules (can bypass the threshold)"
                guard = _as_address(w3.eth.get_storage_at(addr, SAFE_GUARD_SLOT).rjust(32, b"\0")) or "none"
                chain[f"{addr}#guard={guard}"] = "Safe guard"
            for sig, label in (("getMinDelay()", "minDelay"), ("getDelay()", "delay")):
                value = _as_uint(_raw_call(w3, addr, sig))
                if value is not None:
                    chain[f"{addr}#{label}={value}"] = f"{sig} of {addr}"
            for sig in CHAIN_NEXT_GETTERS:
                nxt_addr = _as_address(_raw_call(w3, addr, sig))
                if nxt_addr:
                    nxt.append((nxt_addr, f"{sig} of {addr}"))
        frontier = nxt
    return chain


def check_trusted_proxy(w3, token, controllers):
    """(issuer, detail) if this exact token is allowlisted AND its live controller chain equals the pin, else
    (None, why not)."""
    entry = TRUSTED_PROXIES.get(cs(token))
    if entry is None:
        return None, "not on the trusted proxy allowlist"
    live = set(controller_chain(w3, controllers))
    pinned = set(entry["controllers"])
    if live != pinned:
        added = ", ".join(sorted(live - pinned)) or "none"
        missing = ", ".join(sorted(pinned - live)) or "none"
        return None, (f"allowlisted for {entry['issuer']} but the controller chain changed "
                      f"(new: {added}; gone: {missing}). Re-verify before trusting it again")
    return entry["issuer"], f"controller chain matches the {entry['issuer']} pin ({len(pinned)} entries)"


def probe_owner_powers(token, ctx, present):
    """On the fork, impersonate each controller and call every present config function with extreme values.

    Each attempt runs inside its own evm_snapshot. If the call succeeds, we measure the effect:
    mint -> total supply change; everything else -> re-run buy/transfer/sell and see if it now fails.
    Returns a list of (kind, signature, args, controller, effect).
    """
    w3 = ctx.w3
    victim = w3.eth.accounts[0]     # the wallet the buy test uses
    tok = erc20(w3, token)
    results = []
    for sig, kind, make_args in PROBES:
        if sig not in present:
            continue
        for how, controller in ctx.controllers:
            hit = None
            for args in make_args(victim, controller):
                snapshot = w3.provider.make_request("evm_snapshot", [])["result"]
                try:
                    w3.provider.make_request("anvil_impersonateAccount", [controller])
                    w3.provider.make_request("anvil_setBalance", [controller, hex(10**20)])
                    supply_before = tok.functions.totalSupply().call()
                    data = "0x" + selector(sig) + encode(arg_types(sig), list(args)).hex().removeprefix("0x")
                    try:
                        tx = w3.eth.send_transaction({"from": controller, "to": token, "data": data, "gas": 3_000_000})
                        ok = w3.eth.wait_for_transaction_receipt(tx, timeout=60).status == 1
                    except Exception:
                        ok = False
                    w3.provider.make_request("anvil_stopImpersonatingAccount", [controller])
                    if not ok:
                        continue
                    if kind == "mint":
                        grew = tok.functions.totalSupply().call() - supply_before
                        effect = f"total supply +{grew / 10**ctx.token_dec:,.0f}" if grew > 0 else None
                    else:
                        saved = (ctx.buy_tax, ctx.transfer_tax, ctx.sell_tax)
                        passed, reason = simulate_all_pools(token, ctx, quiet=True)
                        if not passed and sim_reason_code(reason) == "INFRA":
                            raise RuntimeError(f"owner probe re-sim could not run: {reason}")
                        ctx.buy_tax, ctx.transfer_tax, ctx.sell_tax = saved
                        effect = None if passed else f"buy/transfer/sell now fails: {reason}"
                    if effect:
                        hit = (kind, sig, args, controller, effect)
                    else:
                        ctx.warnings.append(f"{sig} by {controller[:10]}… succeeded but had no visible effect")
                finally:
                    w3.provider.make_request("evm_revert", [snapshot])
                if hit:
                    break
            if hit:
                results.append(hit)
                break
    return results


# ---------------------------------------------------------------- safety

def sim_reason_code(reason):
    if reason.startswith("could not fund"):
        return "INFRA"
    if "tax" in reason and "above" in reason:
        return "TAX"
    return "HONEYPOT"


def is_safe_token(token_address, ctx):
    """Defensive gate. Returns False unless the token clears every check we can run.

    1. Bytecode scan (read-only, any RPC): which risky functions exist, is it an upgradeable proxy.
    2. Controllers: owner()/getOwner()/admin()/proxy admin still set?
    3. Fork only: buy -> transfer -> sell simulation on every qualifying pool (honeypot + taxes).
    4. Fork only: impersonate each controller and try to switch on each risky power with extreme values.
       Any that breaks trading = DYNAMIC_UNSAFE.
    Per-pool liquidity is enforced separately by filter_pools_by_liquidity().
    Sets ctx.verdict to "SAFE", "PROXY_WARNING" (allowlisted upgradeable proxy), "UNSAFE" or "DYNAMIC_UNSAFE".
    """
    token_address = cs(token_address)
    w3 = ctx.w3

    def fail(verdict, reason, code):
        ctx.verdict, ctx.reason, ctx.reason_code = verdict, reason, code
        print(f"  {verdict}: {reason}")
        return False

    if not ctx.pools:
        return fail("UNSAFE", "no pool clears the per-pool liquidity floor", "LIQUIDITY")

    # 1 + 2: structure (read-only)
    present, impl = scan_bytecode(w3, token_address)
    ctx.controllers = find_controllers(w3, token_address)
    risky = {s: k for s, k in present.items() if k != "ownership"}
    print(f"  Bytecode: {', '.join(f'{s} [{k}]' for s, k in risky.items()) or 'no risky functions found'}")
    if impl:
        print(f"  Upgradeable proxy -> implementation {impl}")
    if ctx.controllers:
        for how, addr in ctx.controllers:
            print(f"  Controller: {addr} (via {how})")
    else:
        renounced = "renounceOwnership()" in present
        print(f"  Controller: none{' (ownership renounced)' if renounced else ''}")
    for s, k in risky.items():
        ctx.warnings.append(f"has {s} [{k}]")

    upgradeable = impl or any(k == "upgrade" for k in present.values())
    if upgradeable and ctx.controllers:
        admin = next((a for how, a in ctx.controllers if "admin" in how), ctx.controllers[0][1])
        issuer, detail = check_trusted_proxy(w3, token_address, ctx.controllers)
        if issuer is None:
            ctx.findings.append(("upgrade", "proxy", (), admin, "admin can replace all contract code"))
            code = "PROXY_PIN_MISMATCH" if cs(token_address) in TRUSTED_PROXIES else "PROXY"
            return fail("DYNAMIC_UNSAFE", f"upgradeable proxy with a live admin ({admin}): "
                                          f"the rules can be rewritten after you buy; {detail}", code)
        ctx.proxy_warning = issuer
        ctx.warnings.append(f"PROXY_WARNING: upgradeable by {issuer}; {detail}")
        print(f"  PROXY_WARNING: upgradeable proxy, trusted issuer {issuer} ({detail}). Continuing checks.")

    if not is_local_rpc(ctx.rpc_url):
        if token_address in VERIFIED_TOKENS:
            ctx.verdict, ctx.reason, ctx.reason_code = "SAFE", "verified list (live RPC)", "OK"
            # (VERIFIED_TOKENS are WETH/USDC; neither reaches here as an untrusted proxy)
            print("  SAFE (verified list). Run against an anvil fork for the full simulation.")
            return True
        return fail("UNSAFE", "live RPC can't run the fork simulation or owner probes and token isn't verified. "
                              "Re-run against an anvil fork.", "LIVE_RPC")

    # 3: buy -> transfer -> sell
    passed, reason = simulate_all_pools(token_address, ctx)
    if not passed:
        return fail("UNSAFE", reason, sim_reason_code(reason))

    # 4: owner impersonation probes
    if ctx.controllers and risky:
        print(f"  Probing {len(risky)} function(s) as the controller, with extreme values…")
        ctx.findings = probe_owner_powers(token_address, ctx, present)
        for kind, sig, args, who, effect in ctx.findings:
            print(f"    {sig} {args} by {who[:10]}… -> {effect}")
        blocking = [f for f in ctx.findings if f[0] != "mint"]
        for f in ctx.findings:
            if f[0] == "mint":
                ctx.warnings.append(f"MINT_POWER: {f[1]} works ({f[4]})")
                print("    (mint is a warning only: it dilutes holders but doesn't break a same-block arb)")
        if blocking:
            return fail("DYNAMIC_UNSAFE", f"controller can change the rules after purchase ({blocking[0][1]})",
                        "OWNER_POWER")
        if not ctx.findings:
            print("    no probe changed anything")

    if ctx.proxy_warning:
        ctx.verdict, ctx.reason, ctx.reason_code = "PROXY_WARNING", f"{reason}; upgradeable by {ctx.proxy_warning}", "OK"
    else:
        ctx.verdict, ctx.reason, ctx.reason_code = "SAFE", reason, "OK"
    print(f"  {ctx.verdict}: {ctx.reason}")
    return True


# ---------------------------------------------------------------- main

def pct(x):
    return "   n/a" if x is None else f"{x * 100:6.3f}%"


@dataclass
class ScanSettings:
    amount: float = 500
    settle: str = "native"
    min_profit: float = 1.0
    min_liquidity: float = 50_000
    max_tax: float = 0.5            # percent
    gas_overhead: int = GAS_OVERHEAD
    gas_per_swap: int = GAS_PER_SWAP
    priority_gwei: float = 0.001
    size_search: bool = False       # also search the best trade size for the top routes (search_best_size)


def evaluate_token(w3, token, settings, rpc_url=RPC_URL):
    """Full pipeline for one token: pools -> per-pool floor -> safety -> every route. Prints progress to stdout.

    Returns a report dict: status is the safety verdict (SAFE / PROXY_WARNING / UNSAFE / DYNAMIC_UNSAFE), or "THIN"
    when the token passes but has fewer than two qualifying pools. Exceptions (bad address, RPC failure) propagate to the caller.
    """
    token = cs(token)
    if token == USDC:
        raise ValueError("USDC is a base routing asset, not a scan target")
    erc = erc20(w3, token)
    try:
        sym = erc.functions.symbol().call()
    except Exception:
        sym = token[:8]
    dec = erc.functions.decimals().call()
    block = w3.eth.block_number

    eth_usd = eth_usd_mid(w3)
    print(f"Anchor (UniV3 0.05% WETH/USDC) mid price: 1 WETH = ${eth_usd:,.2f}")

    all_pools = find_pools(w3, token)
    bases = "/".join(base_symbol(b) for b in BASES if b != token)
    print(f"\n{sym} pools vs {bases}, per-pool liquidity (floor ${settings.min_liquidity:,.0f} each):")
    pools = filter_pools_by_liquidity(w3, all_pools, token, settings.min_liquidity, eth_usd)
    venues = {"univ3": "Uniswap V3", "aero": "Aero legacy", "cl": "Aero Slipstream"}
    print("  Qualifying pools by venue: " + ", ".join(
        f"{name} {sum(p.kind == k for p in pools)}/{sum(p.kind == k for p in all_pools)}" for k, name in venues.items()))

    gas = build_gas_model(w3, settings.priority_gwei, eth_usd, settings.gas_overhead, settings.gas_per_swap)
    ctx = Context(w3, token, dec, sym, gas.cost_usd(2), rpc_url=rpc_url, pools=pools,
                  max_tax=settings.max_tax / 100, eth_usd=eth_usd, gas=gas)
    report = {"token": token, "symbol": sym, "decimals": dec, "block": block, "eth_usd": eth_usd,
              "pools_found": len(all_pools), "pools_qualified": len(pools), "ctx": ctx, "gas": gas,
              "routes": [], "failed_routes": 0}

    print("\nSafety check:")
    if not is_safe_token(token, ctx):
        report["status"] = ctx.verdict
        return report
    if len(pools) < 2:
        ctx.reason, ctx.reason_code = "fewer than two pools above the liquidity floor", "LIQUIDITY"
        report["status"] = "THIN"
        return report

    results = [calculate_net_out(settings.amount, a, b, ctx, settings.settle) for a, b in permutations(pools, 2)]
    report["failed_routes"] = sum(not r["ok"] for r in results)
    report["routes"] = sorted((r for r in results if r["ok"]), key=lambda r: r["net_profit"], reverse=True)
    report["status"] = ctx.verdict     # SAFE or PROXY_WARNING
    if settings.size_search:
        report["size_search"] = []
        for r in report["routes"][:SEARCH_TOP_ROUTES]:
            found = search_best_size(r["pool_a"], r["pool_b"], ctx, settings.settle)
            found["route"], found["path"] = r["route"], r["path"]
            report["size_search"].append(found)
    return report


def main():
    ap = argparse.ArgumentParser(description="Quote-based net-profit arb scanner for Base (USDC + WETH bases)")
    ap.add_argument("--amount", type=float, default=500, help="trade size in USD (default 500)")
    ap.add_argument("--token", default=WETH, help="token to route through, paired with USDC or WETH (default WETH)")
    ap.add_argument("--settle", choices=["native", "usdc"], default="native",
                    help="native: start/end in pool A's base (fewest swaps). usdc: always start/end in USDC")
    ap.add_argument("--min-profit", type=float, default=1.0, help="X in 'trade if net profit > X' (USD)")
    ap.add_argument("--min-liquidity", type=float, default=50_000, help="per-pool liquidity floor in USD")
    ap.add_argument("--max-tax", type=float, default=0.5, help="max buy/transfer/sell tax in percent")
    ap.add_argument("--gas-overhead", type=int, default=GAS_OVERHEAD, help="fixed gas units per arb tx")
    ap.add_argument("--gas-per-swap", type=int, default=GAS_PER_SWAP, help="gas units added per swap")
    ap.add_argument("--priority-gwei", type=float, default=0.001, help="tip per gas on top of the base fee")
    ap.add_argument("--size-search", action="store_true",
                    help=f"also search trade sizes ${', $'.join(map(str, SEARCH_SIZES))} (+ refinement) "
                         f"for the top {SEARCH_TOP_ROUTES} routes")
    args = ap.parse_args()
    settings = ScanSettings(args.amount, args.settle, args.min_profit, args.min_liquidity, args.max_tax,
                            args.gas_overhead, args.gas_per_swap, args.priority_gwei, args.size_search)

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit(f"Could not connect to {redact_url(RPC_URL)}")
    mode = "LOCAL FORK" if is_local_rpc(RPC_URL) else "LIVE (read-only)"
    shown = RPC_URL if is_local_rpc(RPC_URL) else redact_url(RPC_URL)
    print(f"Connected to {shown} [{mode}] | chain {w3.eth.chain_id} | block {w3.eth.block_number}")

    try:
        report = evaluate_token(w3, args.token, settings, RPC_URL)
    except ValueError as e:
        raise SystemExit(str(e))
    if report["status"] in ("UNSAFE", "DYNAMIC_UNSAFE"):
        raise SystemExit("Token failed safety check, skipping.")
    if report["status"] == "THIN":
        raise SystemExit("Need at least two pools above the liquidity floor to compare.")

    gas = report["gas"]
    print(f"\nGas model: {gas.overhead:,} + {gas.per_swap:,}/swap units at "
          f"{gas.l2_price_wei / 1e9:.4f} gwei + L1 data fee:")
    for n in (2, 3, 4):
        print(f"  {n} swaps: {gas.units(n):,} units = ${gas.cost_usd(n):.4f}")

    print(f"\nCycles: ${args.amount:,.2f} in, settle={args.settle}. Path shows every swap.\n")
    print(f"{'route (pool A -> pool B)':<62}{'path':<36}{'swaps':>6}{'fees':>9}{'max imp':>9}"
          f"{'gas':>8}{'NET P/L':>12}  decision")
    for r in report["routes"]:
        decision = "TRADE" if r["net_profit"] > args.min_profit else "skip"
        print(f"{r['route']:<62}{r['path']:<36}{r['hops']:>6}{r['swap_fees_usdc']:>9.4f}"
              f"{pct(r['max_impact']):>9}{r['gas_usdc']:>8.4f}{r['net_profit']:>+12.4f}  {decision}")
    if report["failed_routes"]:
        print(f"\n({report['failed_routes']} route(s) skipped: a quote returned 0, e.g. not enough liquidity for this size)")
    for found in report.get("size_search", []):
        best = found["best"]
        curve = "  ".join(f"${x:,.0f}:{'fail' if v is None else f'{v:+.3f}'}" for x, v in found["curve"].items())
        head = "no size quoted" if best is None else f"best ${best['amount_in']:,.2f} -> net {best['net_profit']:+.4f}"
        print(f"\nSize search {found['route']}: {head}"
              f"{'  TRADE' if best and best['net_profit'] > args.min_profit else ''}\n  {curve}")
    print(f"\nRule: TRADE only if net profit > ${args.min_profit:.2f} (USD). "
          "Fees and price impact are inside the quotes; gas scales with swap count. No arb trade was sent.")


if __name__ == "__main__":
    main()
