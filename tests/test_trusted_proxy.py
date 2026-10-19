"""Trusted proxy allowlist: allowlisted issuers pass with PROXY_WARNING; everything else stays DYNAMIC_UNSAFE.

Trust is per (token address + exact controller chain), never per controller address: anyone can deploy a proxy that
names a real issuer's admin, and an issuer's admin can be rotated to someone else.
"""
import pytest

import scanner

CBBTC = scanner.cs("0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf")
CBBTC_ADMIN = scanner.cs("0x42D90b3faF2287475cdc863706Bd7cc3E938C2Af")
RANDOM_ADMIN = scanner.cs("0x" + "ab" * 20)


@pytest.mark.parametrize("token", sorted(scanner.TRUSTED_PROXIES), ids=lambda t: scanner.TRUSTED_PROXIES[t]["symbol"])
def test_allowlisted_issuer_proxy_passes_with_warning(check, token):
    ctx = check(token)
    entry = scanner.TRUSTED_PROXIES[token]
    assert ctx.verdict == "PROXY_WARNING", ctx.reason
    assert ctx.proxy_warning == entry["issuer"] and ctx.reason_code == "OK"
    assert any(w.startswith("PROXY_WARNING") for w in ctx.warnings)
    assert not any(f[0] == "upgrade" for f in ctx.findings)


def test_unlisted_test_proxy_is_blocked(make_proxy_trap, check):
    ctx = check(make_proxy_trap("UPX", RANDOM_ADMIN))
    assert ctx.verdict == "DYNAMIC_UNSAFE" and ctx.reason_code == "PROXY"
    assert "not on the trusted proxy allowlist" in ctx.reason


def test_copying_an_issuers_admin_address_earns_no_trust(make_proxy_trap, check):
    # A scam proxy whose admin slot names Coinbase's real cbBTC admin: same controller address, different token.
    ctx = check(make_proxy_trap("FAKECB", CBBTC_ADMIN))
    assert [a for _, a in ctx.controllers] == [CBBTC_ADMIN]
    assert ctx.verdict == "DYNAMIC_UNSAFE" and ctx.reason_code == "PROXY"


def test_rotated_admin_on_allowlisted_token_falls_back_to_block(w3, check):
    # On the fork, the real cbBTC admin hands the proxy to a new key. The pin no longer matches -> hard block.
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        proxy = w3.eth.contract(address=CBBTC, abi=[scanner.fn("changeAdmin", ["address"], [], "nonpayable")])
        w3.provider.make_request("anvil_impersonateAccount", [CBBTC_ADMIN])
        w3.provider.make_request("anvil_setBalance", [CBBTC_ADMIN, hex(10**20)])
        scanner.send(w3, proxy.functions.changeAdmin(RANDOM_ADMIN), CBBTC_ADMIN)
        w3.provider.make_request("anvil_stopImpersonatingAccount", [CBBTC_ADMIN])
        ctx = check(CBBTC)
        assert ctx.verdict == "DYNAMIC_UNSAFE"
        # blocked, but NOT cached: a human re-pin must take effect on the next cycle
        assert ctx.reason_code == "PROXY_PIN_MISMATCH" and "PROXY_PIN_MISMATCH" not in scanner.MALICIOUS_CODES
        assert "controller chain changed" in ctx.reason and RANDOM_ADMIN in ctx.reason
    finally:
        w3.provider.make_request("evm_revert", [snap])
    assert check(CBBTC).verdict == "PROXY_WARNING"     # restored fork -> trusted again


def test_changed_safe_threshold_breaks_the_pin(w3):
    # Same Safe address, weaker threshold (MORPHO's 5/9 -> 1/9): must not match the pin.
    morpho = scanner.cs("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842")
    safe = scanner.cs("0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa")
    snap = w3.provider.make_request("evm_snapshot", [])["result"]
    try:
        # Safe stores threshold at storage slot 4 (Safe 1.3.0 layout). Overwrite it on the fork only.
        w3.provider.make_request("anvil_setStorageAt", [safe, hex(4), "0x" + "00" * 31 + "01"])
        issuer, why = scanner.check_trusted_proxy(w3, morpho, scanner.find_controllers(w3, morpho))
        assert issuer is None and f"{safe}#threshold=1" in why
    finally:
        w3.provider.make_request("evm_revert", [snap])
    issuer, _ = scanner.check_trusted_proxy(w3, morpho, scanner.find_controllers(w3, morpho))
    assert issuer == "Morpho"


def test_synthetic_allowlist_entry_lets_a_test_proxy_through(make_proxy_trap, check, w3, monkeypatch):
    # The positive path on a token we control: pin the test proxy's exact chain -> PROXY_WARNING.
    token = make_proxy_trap("OKPX", RANDOM_ADMIN)
    chain = scanner.controller_chain(w3, scanner.find_controllers(w3, token))
    monkeypatch.setitem(scanner.TRUSTED_PROXIES, token, {"issuer": "TestIssuer", "symbol": "OKPX",
                                                         "controllers": chain})
    ctx = check(token)
    assert ctx.verdict == "PROXY_WARNING" and ctx.proxy_warning == "TestIssuer"


MORPHO = scanner.cs("0xBAa5CC21fd487B8Fcc2F632f3F4E8D37262a0842")
MORPHO_SAFE = scanner.cs("0xcBa28b38103307Ec8dA98377ffF9816C164f9AFa")


def fail_selector(monkeypatch, w3, sig, exc):
    """Make every eth_call for one selector raise `exc` (everything else passes through)."""
    real = w3.eth.call
    sel = "0x" + scanner.selector(sig)

    def call(tx, *a, **k):
        if str(tx.get("data", "")).startswith(sel):
            raise exc
        return real(tx, *a, **k)
    monkeypatch.setattr(w3.eth, "call", call)


def test_rpc_error_in_fingerprint_raises_instead_of_dropping_keys(w3, monkeypatch):
    # A timeout on getThreshold() must not silently drop the whole Safe from the fingerprint (which would read as
    # "controller chain changed" and get a legit token blocked). It must surface as an error -> retried next cycle.
    import requests
    fail_selector(monkeypatch, w3, "getThreshold()", requests.exceptions.ReadTimeout("Read timed out"))
    with pytest.raises(requests.exceptions.ReadTimeout):
        scanner.check_trusted_proxy(w3, MORPHO, scanner.find_controllers(w3, MORPHO))


def test_unreadable_safe_modules_never_match(w3, monkeypatch):
    # If the module list can't be read (reverts), it must NOT default to "none": modules can bypass the threshold.
    from web3.exceptions import ContractLogicError
    fail_selector(monkeypatch, w3, "getModulesPaginated(address,uint256)", ContractLogicError("execution reverted"))
    chain = scanner.controller_chain(w3, scanner.find_controllers(w3, MORPHO))
    assert f"{MORPHO_SAFE}#modules=UNREADABLE" in chain
    issuer, why = scanner.check_trusted_proxy(w3, MORPHO, scanner.find_controllers(w3, MORPHO))
    assert issuer is None and "UNREADABLE" in why
