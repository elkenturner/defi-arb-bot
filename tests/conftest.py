"""Shared fixtures: a throwaway anvil fork of Base, the compiled Trap token, and a token factory."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scanner  # noqa: E402
from scanner import AERO_FACTORY, AERO_FACTORY_ABI, AERO_ROUTER, USDC, WETH, Pool, erc20, send, swap_on_fork  # noqa: E402

FORK_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")   # .env already loaded by `import scanner`
PORT = int(os.getenv("TEST_ANVIL_PORT", "8546"))
LOCAL_URL = f"http://127.0.0.1:{PORT}"
CONTRACTS = Path(__file__).resolve().parent / "contracts"
POOL_USDC = 30_000 * 10**6          # each fake token gets a $60k-deep Aerodrome pool
MIN_LIQUIDITY = 50_000

AERO_ADD_LIQUIDITY_ABI = [{
    "name": "addLiquidity", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"type": t, "name": f"a{i}"} for i, t in enumerate(
        ["address", "address", "bool", "uint256", "uint256", "uint256", "uint256", "address", "uint256"])],
    "outputs": [{"type": "uint256", "name": f"r{i}"} for i in range(3)],
}]


def foundry_bin(name):
    found = shutil.which(name) or str(Path.home() / ".foundry" / "bin" / name)
    if not Path(found).exists():
        pytest.skip(f"{name} not installed (curl -L https://foundry.paradigm.xyz | bash && foundryup)")
    return found


@pytest.fixture(scope="session")
def trap_artifact():
    subprocess.run([foundry_bin("forge"), "build", "--root", str(CONTRACTS), "--contracts", "src", "-q"], check=True)
    return json.loads((CONTRACTS / "out" / "Trap.sol" / "Trap.json").read_text())


@pytest.fixture(scope="session")
def w3():
    from pipeline import ANVIL_FORK_FLAGS   # same polite throttling/retries as the pipeline's forks
    proc = subprocess.Popen([foundry_bin("anvil"), "--fork-url", FORK_URL, "--port", str(PORT), "--silent",
                             *ANVIL_FORK_FLAGS],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    w3 = Web3(Web3.HTTPProvider(LOCAL_URL, request_kwargs={"timeout": 60}))
    for _ in range(60):
        try:
            if w3.eth.chain_id == 8453:
                break
        except Exception:
            time.sleep(1)
    else:
        proc.terminate()
        pytest.fail("anvil fork did not start")
    yield w3
    proc.terminate()


@pytest.fixture(scope="session")
def deployer(w3):
    """accounts[2], funded with 1M USDC + 500 WETH WITHOUT trading, so no pool price on the fork moves.

    USDC is minted through Circle's masterMinter role (impersonated on the fork only). Swapping for it instead
    would shift the anchor pool's price for every later test.
    """
    owner = w3.eth.accounts[2]
    scanner.prepare_test_wallet(w3, owner, 10**22)
    send(w3, erc20(w3, WETH).functions.deposit(), owner, value=500 * 10**18)   # ~12 WETH per $30k WETH pool
    usdc = w3.eth.contract(address=USDC, abi=[
        scanner.fn("masterMinter", [], ["address"]),
        scanner.fn("configureMinter", ["address", "uint256"], ["bool"], "nonpayable"),
        scanner.fn("mint", ["address", "uint256"], ["bool"], "nonpayable")])
    master = usdc.functions.masterMinter().call()
    w3.provider.make_request("anvil_impersonateAccount", [master])
    w3.provider.make_request("anvil_setBalance", [master, hex(10**20)])
    send(w3, usdc.functions.configureMinter(owner, 10**13), master)
    w3.provider.make_request("anvil_stopImpersonatingAccount", [master])
    send(w3, usdc.functions.mint(owner, 1_000_000 * 10**6), owner)
    return owner


@pytest.fixture(scope="session")
def make_trap(w3, trap_artifact, deployer):
    """make_trap(symbol, tax_bps=0, honeypot=False, powers=0, renounce=False) -> token address with a pool."""
    Trap = w3.eth.contract(abi=trap_artifact["abi"], bytecode=trap_artifact["bytecode"]["object"])
    router = w3.eth.contract(address=AERO_ROUTER, abi=AERO_ADD_LIQUIDITY_ABI)
    factory = w3.eth.contract(address=AERO_FACTORY, abi=AERO_FACTORY_ABI)

    def _make(symbol, tax_bps=0, honeypot=False, powers=0, renounce=False, bases=(USDC,), side_usd=None,
              token_side=None):
        """side_usd: optional {base: USD per side} to make one pool deeper than another (default $30k).
        token_side: optional {base: whole tokens in that pool}. Default = side_usd (token at $1); fewer tokens
        than USD makes the token dearer in that pool, i.e. a deliberate cross-pool mispricing."""
        side_usd = side_usd or {}
        token_side = token_side or {}
        receipt = w3.eth.wait_for_transaction_receipt(
            Trap.constructor(symbol, tax_bps, honeypot, powers).transact({"from": deployer}))
        token = receipt.contractAddress
        trap = w3.eth.contract(address=token, abi=trap_artifact["abi"])
        send(w3, erc20(w3, token).functions.approve(AERO_ROUTER, 2**256 - 1), deployer)
        for base in bases:
            send(w3, erc20(w3, base).functions.approve(AERO_ROUTER, 2**256 - 1), deployer)
            # Each side worth `usd` ($30k default): token at $1, base at its USD price (WETH via the anchor)
            usd = side_usd.get(base, 30_000)
            base_amount = scanner.usd_to_base_units(usd, base, scanner.eth_usd_mid(w3))
            deadline = w3.eth.get_block("latest").timestamp + 600
            tokens = token_side.get(base, usd)
            send(w3, router.functions.addLiquidity(token, base, False, tokens * 10**18, base_amount, 0, 0,
                                                   deployer, deadline), deployer)
        # The honeypot blocks sells into the first base's pool only (Trap tracks one pool).
        send(w3, trap.functions.setPool(factory.functions.getPool(token, bases[0], False).call()), deployer)
        if renounce:
            send(w3, trap.functions.renounceOwnership(), deployer)
        return token

    return _make


@pytest.fixture(scope="session")
def make_proxy_trap(w3, deployer):
    """make_proxy_trap(symbol, admin) -> an 'upgradeable proxy' test token (ProxyTrap) whose EIP-1967 admin is `admin`,
    ownership renounced, with a $60k Aerodrome USDC pool."""
    art = json.loads((CONTRACTS / "out" / "ProxyTrap.sol" / "ProxyTrap.json").read_text())
    ProxyTrap = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    router = w3.eth.contract(address=AERO_ROUTER, abi=AERO_ADD_LIQUIDITY_ABI)
    factory = w3.eth.contract(address=AERO_FACTORY, abi=AERO_FACTORY_ABI)

    def _make(symbol, admin):
        token = w3.eth.wait_for_transaction_receipt(
            ProxyTrap.constructor(symbol, admin).transact({"from": deployer})).contractAddress
        trap = w3.eth.contract(address=token, abi=art["abi"])
        send(w3, erc20(w3, token).functions.approve(AERO_ROUTER, 2**256 - 1), deployer)
        send(w3, erc20(w3, USDC).functions.approve(AERO_ROUTER, 2**256 - 1), deployer)
        deadline = w3.eth.get_block("latest").timestamp + 600
        send(w3, router.functions.addLiquidity(token, USDC, False, 30_000 * 10**18, POOL_USDC, 0, 0,
                                               deployer, deadline), deployer)
        send(w3, trap.functions.setPool(factory.functions.getPool(token, USDC, False).call()), deployer)
        send(w3, trap.functions.renounceOwnership(), deployer)
        return token

    return _make


@pytest.fixture
def check(w3):
    """check(token) -> the Context after is_safe_token ran (ctx.verdict, ctx.findings, ...)."""
    def _check(token):
        sym = erc20(w3, token).functions.symbol().call()
        dec = erc20(w3, token).functions.decimals().call()
        pools = scanner.filter_pools_by_liquidity(w3, scanner.find_pools(w3, token), token, MIN_LIQUIDITY)
        ctx = scanner.Context(w3, token, dec, sym, 0.0, rpc_url=LOCAL_URL, pools=pools)
        scanner.is_safe_token(token, ctx)
        return ctx
    return _check
