# Security policy

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/ble-bond/) gets fixes.

## Reporting a vulnerability

Please don't open a public issue. Tell me privately instead:
[Report a vulnerability](https://github.com/83noit/ble-bond/security/advisories/new).

These count as security bugs:

- anything that makes the library pair, or accept a pairing, when you didn't ask for a new
  bond (for example the BlueZ pairing agent saying yes to another device's request)
- anything that sends data to a device you didn't hand it, or sends a frame you didn't ask for

Connection failures, timeouts and adapters that don't work are ordinary bugs:
[open an issue here](https://github.com/83noit/ble-bond/issues).
