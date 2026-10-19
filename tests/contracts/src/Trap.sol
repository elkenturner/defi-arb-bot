// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// Test-only ERC20 for the safety regression suite. Never deploy outside a local fork.
/// - taxBps: tax on every transfer (owner exempt)
/// - honeypot: transfers into the pool (sells) revert for everyone but the owner
/// - powers: bitmask of owner functions that actually work (the functions always exist in bytecode)
contract Trap {
    uint8 public constant MUTABLE_TAX = 1;
    uint8 public constant PAUSABLE = 2;
    uint8 public constant BLACKLIST = 4;
    uint8 public constant MINTABLE = 8;

    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    address public owner;
    address public pool;
    uint256 public taxBps;
    bool public honeypot;
    uint8 public powers;
    bool public paused;
    mapping(address => bool) public blacklisted;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }

    constructor(string memory s, uint256 _taxBps, bool _honeypot, uint8 _powers) {
        symbol = s; taxBps = _taxBps; honeypot = _honeypot; powers = _powers; owner = msg.sender;
        totalSupply = 1e27; balanceOf[msg.sender] = 1e27;
    }

    function setPool(address p) external onlyOwner { pool = p; }
    function renounceOwnership() external onlyOwner { owner = address(0); }
    function setTax(uint256 bps) external onlyOwner { require(powers & MUTABLE_TAX != 0, "locked"); require(bps <= 10000); taxBps = bps; }
    function pause() external onlyOwner { require(powers & PAUSABLE != 0, "locked"); paused = true; }
    function blacklist(address a, bool b) external onlyOwner { require(powers & BLACKLIST != 0, "locked"); blacklisted[a] = b; }
    function mint(address to, uint256 a) external onlyOwner { require(powers & MINTABLE != 0, "locked"); totalSupply += a; balanceOf[to] += a; }

    function approve(address s, uint256 a) external returns (bool) { allowance[msg.sender][s] = a; return true; }
    function transfer(address to, uint256 a) external returns (bool) { _move(msg.sender, to, a); return true; }
    function transferFrom(address f, address to, uint256 a) external returns (bool) {
        if (allowance[f][msg.sender] != type(uint256).max) allowance[f][msg.sender] -= a;
        _move(f, to, a); return true;
    }

    function _move(address f, address to, uint256 a) internal {
        require(!paused || f == owner, "paused");
        require(!blacklisted[f] && !blacklisted[to], "blacklisted");
        if (honeypot && to == pool && f != owner) revert("no selling");
        balanceOf[f] -= a;
        uint256 tax = (f == owner || to == owner) ? 0 : a * taxBps / 10000;
        balanceOf[to] += a - tax; balanceOf[owner] += tax;
    }
}
