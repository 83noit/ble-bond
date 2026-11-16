# ble-bond

[![CI](https://github.com/83noit/ble-bond/actions/workflows/ci.yml/badge.svg)](https://github.com/83noit/ble-bond/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ble-bond)](https://pypi.org/project/ble-bond/)
[![License: MIT](https://img.shields.io/github/license/83noit/ble-bond)](https://github.com/83noit/ble-bond/blob/main/LICENSE)

Bonded Bluetooth LE sessions for devices that pair in a button window and answer one request at
a time.

Some devices only accept a new bond (a saved pairing) for a few seconds after you press a
button. They drop any connection that isn't encrypted, and their replies don't say which
request they answer, so a late or stray reply can easily be mistaken for the wrong one. Going
through a Home Assistant Bluetooth proxy adds a few more catches: the bond lives on one proxy,
Home Assistant picks the proxy for each attempt, and a refusal through the wrong proxy tells you
nothing about the bond.

ble-bond deals with all that, on top of [bleak](https://github.com/hbldh/bleak) and
[bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector). Building
the messages and speaking the device's protocol is left to the device library.

Used by [grundfos-ble](https://github.com/83noit/grundfos-ble) (Grundfos ALPHA2 GO pumps) and
[mira-ble](https://github.com/83noit/mira-ble) (Mira digital showers).

## What it does

- Pairs on every connection. On an ESPHome proxy with a stored bond that's just a quick
  re-encrypt; otherwise it really pairs. If the device turns it down (ESP-IDF reason 82, BlueZ
  `AuthenticationFailed`), you get `PairingRequired`. A proxy that can't pair at all gives you
  `PairingUnsupported`.
- Notes which adapter or proxy each attempt went through (`last_source`), refused attempts
  too, so you can tell which path a refusal came from.
- On a local Linux adapter, it registers a BlueZ pairing agent (one at a time), but only while
  a new bond is expected. `remove_bluez_bond` makes BlueZ forget a bond the device has dropped.
- Runs one request at a time. A frame only counts as the reply if your check accepts it. If a
  request doesn't finish (timeout, cancellation, failed write), the link is closed, so its late
  reply can't answer the next request.
- Everything that differs between devices (pauses, retries, how shaky hints about the bond are
  reported, write size, connection parameters) goes in one `LinkPolicy`.

## Usage

```python
import asyncio

from bleak import BleakScanner
from ble_bond import BondedLink, LinkPolicy, PairingRequired

REPLY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


class LengthFramed:
    """The device's framing: here, a length byte that counts the whole frame."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        self.buffer += data
        frames = []
        while self.buffer and len(self.buffer) >= self.buffer[0] > 0:
            frames.append(bytes(self.buffer[: self.buffer[0]]))
            del self.buffer[: self.buffer[0]]
        return frames

    def reset(self) -> None:
        self.buffer.clear()


async def main() -> None:
    device = await BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
    policy = LinkPolicy(settle_after_subscribe=0.5)
    try:
        async with BondedLink(
            device, notify_uuid=REPLY_UUID, assembler=LengthFramed(), policy=policy
        ) as link:
            reply = await link.request(b"\x02\x01", accept=lambda frame: frame[1] == 0x01)
            print(reply.hex(" "), "via", link.last_source)
    except PairingRequired:
        print("Press the device's button, then connect with expect_new_bond=True.")


asyncio.run(main())
```

Debug logging for `ble_bond` shows what the link is doing: connecting, signs about pairing,
retries, and frames that turned up when no request was waiting. Every frame sent and received
goes to `ble_bond.frames`, which stays at INFO unless you set it to debug.

`ble_bond.testing` has a fake bleak client and a stand-in for the connection call, so a device
library can test its transport without any hardware.

## Compatibility

Every 1.x release stays backward compatible. Several device libraries can be installed in the
same Home Assistant, and they all share one copy of this package, so a break would hit all of
them. Depend on `ble-bond>=1.0,<2`.

## Acknowledgements

The BlueZ pairing agent is adapted from [alpha-hwr](https://github.com/eman/alpha-hwr) (MIT);
its notice is in [THIRD_PARTY_NOTICES.md](https://github.com/83noit/ble-bond/blob/main/THIRD_PARTY_NOTICES.md).
The pause after subscribing comes from ESPHome's findings in
[esphome/esphome#22651](https://github.com/esphome/esphome/pull/22651).

## Licence

MIT
