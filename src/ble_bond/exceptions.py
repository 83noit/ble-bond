"""Exceptions raised by ble-bond.

Device libraries usually translate these into their own exceptions, so that their callers see
one hierarchy; each keeps ``error_code`` and the original as ``__cause__``.
"""

from __future__ import annotations


class LinkError(Exception):
    """Connecting to or talking to the device failed at the Bluetooth level.

    ``error_code`` holds the adapter's numeric reason when it reported one.
    """

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class PairingRequired(LinkError):
    """The link could not be encrypted: the device wants a bond the adapter does not hold.

    Two cases of very different strength:

    * ``refused`` is True: the device turned pairing down outright (ESP-IDF SMP reason 82
      through an ESPHome proxy, BlueZ ``AuthenticationFailed`` on a local adapter). The adapter
      holds no bond the device accepts, and the device is not in its pairing window.
    * ``refused`` is False: the link came up but stayed unencrypted ("insufficient
      authentication"), and the policy counts that as weak evidence. It can be a lost bond, but
      also a proxy whose bond store is stuck, or an attempt through an adapter that never held
      the bond. On its own it is no reason to pair again.

    Only an attempt through the adapter that holds the bond says anything about the bond; see
    :attr:`~ble_bond.link.BondedLink.last_source`.
    """

    def __init__(
        self, message: str, error_code: int | None = None, *, refused: bool = True
    ) -> None:
        super().__init__(message, error_code)
        self.refused = refused


class PairingUnsupported(LinkError):
    """The Bluetooth path cannot pair at all.

    Raised when an ESPHome proxy's firmware has no pairing support (bleak-esphome's "Pairing is
    not available in this version ESPHome", or reason 129), unless the policy allows one more
    try without pairing. Pressing the device's button cannot help.
    """


class ConnectionLost(LinkError):
    """The link was not up, or dropped while a request was in flight."""


class RequestTimeout(LinkError, TimeoutError):
    """No accepted reply arrived within the request timeout."""
