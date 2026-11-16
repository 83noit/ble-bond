"""ble-bond: bonded Bluetooth LE sessions for devices that pair in a button window.

For devices that accept a new bond only for a few seconds after a button press, and then answer
one request at a time with no request id, usually through a Home Assistant Bluetooth proxy.
The device library brings its framing (an :class:`Assembler`) and its :class:`LinkPolicy`;
ble-bond handles pairing, which adapter or proxy each attempt used, the BlueZ pairing agent, and
matching replies to requests.
"""

from __future__ import annotations

from ble_bond.bluez import bluez_device_path, bluez_pairing_agent, remove_bluez_bond
from ble_bond.exceptions import (
    ConnectionLost,
    LinkError,
    PairingRequired,
    PairingUnsupported,
    RequestTimeout,
)
from ble_bond.link import Accept, Assembler, BondedLink, LinkPolicy, SubscribeRetry

__version__ = "1.1.0"

__all__ = [
    "Accept",
    "Assembler",
    "BondedLink",
    "ConnectionLost",
    "LinkError",
    "LinkPolicy",
    "PairingRequired",
    "PairingUnsupported",
    "RequestTimeout",
    "SubscribeRetry",
    "__version__",
    "bluez_device_path",
    "bluez_pairing_agent",
    "remove_bluez_bond",
]
