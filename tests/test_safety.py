"""Regression suite for is_safe_token(). Starts its own anvil fork of Base (needs Foundry + internet).

Run:  pytest -v tests/
"""
from scanner import WETH

MUTABLE_TAX, PAUSABLE, BLACKLIST, MINTABLE = 1, 2, 4, 8


def test_weth_is_safe(check):
    ctx = check(WETH)
    assert ctx.verdict == "SAFE"
    assert ctx.controllers == []


def test_clean_renounced_token_is_safe(make_trap, check):
    ctx = check(make_trap("CLEAN", renounce=True))
    assert ctx.verdict == "SAFE"
    assert ctx.controllers == []


def test_owner_with_locked_powers_is_safe(make_trap, check):
    # Functions exist in bytecode (warnings), but every probe reverts: no false positive.
    ctx = check(make_trap("LOCKED", powers=0))
    assert ctx.verdict == "SAFE"
    assert ctx.controllers and ctx.findings == []
    assert any("setTax" in w for w in ctx.warnings)


def test_static_tax_is_unsafe(make_trap, check):
    ctx = check(make_trap("TAX5", tax_bps=500))
    assert ctx.verdict == "UNSAFE"
    assert abs(ctx.transfer_tax - 0.05) < 1e-6


def test_honeypot_is_unsafe(make_trap, check):
    ctx = check(make_trap("HONEY", honeypot=True))
    assert ctx.verdict == "UNSAFE"


def test_mutable_tax_is_dynamic_unsafe(make_trap, check):
    ctx = check(make_trap("MUTTAX", powers=MUTABLE_TAX))
    assert ctx.verdict == "DYNAMIC_UNSAFE"
    assert ctx.findings[0][1] == "setTax(uint256)"


def test_pausable_is_dynamic_unsafe(make_trap, check):
    ctx = check(make_trap("PAUSE", powers=PAUSABLE))
    assert ctx.verdict == "DYNAMIC_UNSAFE"
    assert ctx.findings[0][1] == "pause()"


def test_blacklist_is_dynamic_unsafe(make_trap, check):
    ctx = check(make_trap("BLIST", powers=BLACKLIST))
    assert ctx.verdict == "DYNAMIC_UNSAFE"
    assert ctx.findings[0][1] == "blacklist(address,bool)"


def test_mint_is_warning_not_block(make_trap, check):
    ctx = check(make_trap("MINT", powers=MINTABLE))
    assert ctx.verdict == "SAFE"
    assert any(w.startswith("MINT_POWER") for w in ctx.warnings)


def test_real_unlisted_upgradeable_proxy_is_dynamic_unsafe(check):
    # DRV (Derive): a real upgradeable proxy with a live admin that is NOT on the trusted issuer allowlist.
    ctx = check("0x9d0E8f5b25384C7310CB8C6aE32C8fbeb645d083")
    assert ctx.verdict == "DYNAMIC_UNSAFE" and ctx.reason_code == "PROXY"
    assert ctx.findings[0][0] == "upgrade"
