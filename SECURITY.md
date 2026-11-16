# Security policy

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/ble-bond/) gets fixes.

## Reporting a vulnerability

Please do not open a public issue.
[Report a vulnerability](https://github.com/83noit/ble-bond/security/advisories/new) privately
instead.

These count as security issues:

- anything that makes the library pair, or accept pairing, when the caller did not ask for a
  new bond (for example the BlueZ pairing agent accepting another device's request);
- anything that sends data to a device the caller did not hand it, or sends a frame the caller
  did not ask for.

Connection failures, timeouts and unsupported adapters are ordinary issues:
[open one here](https://github.com/83noit/ble-bond/issues).
