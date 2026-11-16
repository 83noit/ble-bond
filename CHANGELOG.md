# Changelog

Notable changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/): every 1.x release stays backward compatible.

## [Unreleased]

### Changed

- The README is rewritten in plainer English, along with this changelog and the security
  policy.

## [1.1.0] - 2026-11-16

### Changed

- Every frame sent and received now goes to its own logger, `ble_bond.frames`, which starts at
  INFO. So debug logging for `ble_bond` (Home Assistant's "Enable debug logging") shows what the
  link is doing without a dozen frame lines per exchange. Set `ble_bond.frames` to debug to see
  the frames. If you set a level for it before the import, it's kept.

## [1.0.0] - 2026-11-16

### Added

- `BondedLink`: a bonded Bluetooth LE link. It pairs on every connection, notes the adapter
  or proxy each attempt used, runs one request at a time with a reply check you supply, and
  closes the link whenever a request doesn't finish. Taken from grundfos-ble 0.2.0 and
  mira-ble, where it already ran on real devices.
- `LinkPolicy` and `SubscribeRetry` for what differs between devices.
- `PairingRequired` (with `refused`), `PairingUnsupported`, `ConnectionLost` and
  `RequestTimeout`, all of them `LinkError`s.
- The BlueZ pairing agent (`bluez_pairing_agent`), `remove_bluez_bond` and `bluez_device_path`.
- `ble_bond.testing`: `FakeClient` and `patched_establish`.

[Unreleased]: https://github.com/83noit/ble-bond/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/83noit/ble-bond/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/83noit/ble-bond/releases/tag/v1.0.0
