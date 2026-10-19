// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./Trap.sol";

/// Test-only "upgradeable" token: a plain Trap ERC20 that also records an admin in the EIP-1967 admin slot and
/// exposes upgradeTo(), so the scanner classifies it as an upgradeable proxy with a live controller.
/// The admin can be ANY address, e.g. a real issuer's admin, to test that copying an admin address doesn't earn trust.
contract ProxyTrap is Trap {
    bytes32 private constant ADMIN_SLOT = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103;

    constructor(string memory s, address admin_) Trap(s, 0, false, 0) {
        assembly { sstore(ADMIN_SLOT, admin_) }
    }

    function upgradeTo(address) external view {
        address a;
        assembly { a := sload(ADMIN_SLOT) }
        require(msg.sender == a, "not admin");
    }
}
