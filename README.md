# ble-bond

[![CI](https://github.com/83noit/ble-bond/actions/workflows/ci.yml/badge.svg)](https://github.com/83noit/ble-bond/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ble-bond)](https://pypi.org/project/ble-bond/)
[![License: MIT](https://img.shields.io/github/license/83noit/ble-bond)](https://github.com/83noit/ble-bond/blob/main/LICENSE)

Bonded Bluetooth LE sessions for devices that pair in a button window and answer one request at
a time.

Some devices accept a new bond only for a few seconds after a button is pressed, drop any link
that is not encrypted, and then answer requests with no request id, so a late or stray reply can
be taken for the wrong request. Reaching them through a Home Assistant Bluetooth proxy adds its
own traps: the bond lives on one proxy, Home Assistant picks the proxy for each attempt, and a
refusal through the wrong proxy says nothing about the bond. ble-bond handles that part, built
on [bleak](https://github.com/hbldh/bleak) and
[bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector), and leaves
framing and the device's protocol to the device library.

Used by [grundfos-ble](https://github.com/83noit/grundfos-ble) (Grundfos ALPHA2 GO pumps) and
[mira-ble](https://github.com/83noit/mira-ble) (Mira digital showers).

## What it does

- Pairs on every connection: on an ESPHome proxy a quick re-encrypt with the stored bond, real
  pairing otherwise. A refusal (ESP-IDF reason 82, BlueZ `AuthenticationFailed`) raises
  `PairingRequired`; a proxy that cannot pair at all raises `PairingUnsupported`.
- Records which adapter or proxy each attempt went through (`last_source`), refused attempts
  included, so a refusal can be pinned on the path that saw it.
- On a local Linux adapter, registers a BlueZ pairing agent, one at a time, only while a new
  bond is expected; `remove_bluez_bond` makes BlueZ forget a bond the device dropped.
- Runs one request at a time. A frame is taken as the reply only when the caller's check
  accepts it; a request that does not finish (timeout, cancellation, failed write) closes the
  link, so its late reply cannot answer the next request.
- What differs between devices (pauses, retries, how weak evidence about the bond is reported,
  write size, connection parameters) is one `LinkPolicy`.

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

Debug logging for `ble_bond` shows the link's events: connecting, pairing evidence, retries,
frames no request was waiting for. Every frame sent and received goes to `ble_bond.frames`,
which stays at INFO unless you set it to debug.

`ble_bond.testing` has a fake bleak client and a patch for the connection call, so a device
library can test its transport without hardware.

## Compatibility

Every 1.x release is backward compatible: several device libraries may be installed in one Home
Assistant, which then holds a single copy of this package. Depend on `ble-bond>=1.0,<2`.

## Acknowledgements

The BlueZ pairing agent is adapted from [alpha-hwr](https://github.com/eman/alpha-hwr) (MIT);
its notice is in [THIRD_PARTY_NOTICES.md](https://github.com/83noit/ble-bond/blob/main/THIRD_PARTY_NOTICES.md).
The pause after subscribing follows ESPHome's measurement in
[esphome/esphome#22651](https://github.com/esphome/esphome/pull/22651).

## Licence

MIT
