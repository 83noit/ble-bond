# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): every 1.x release is backward compatible.

## [Unreleased]

## [1.0.0]

### Added

- `BondedLink`: a bonded BLE link that pairs on every connection, records the adapter or proxy
  each attempt used, runs one request at a time with a caller-supplied reply check, and closes
  the link on any request that does not finish. Taken from grundfos-ble 0.2.0 and mira-ble,
  where it ran on real devices.
- `LinkPolicy` and `SubscribeRetry` for what differs between devices.
- `PairingRequired` (with `refused`), `PairingUnsupported`, `ConnectionLost`,
  `RequestTimeout`, all `LinkError`s.
- The BlueZ pairing agent (`bluez_pairing_agent`), `remove_bluez_bond` and `bluez_device_path`.
- `ble_bond.testing`: `FakeClient` and `patched_establish`.

[Unreleased]: https://github.com/83noit/ble-bond/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/83noit/ble-bond/releases/tag/v1.0.0
