"""The README's usage example, run against a fake client."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from ble_bond import link as link_module
from ble_bond.testing import FakeClient, patched_establish

README = Path(__file__).parent.parent / "README.md"


def test_usage_example_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    match = re.search(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    assert match is not None
    device = BLEDevice("AA:BB:CC:DD:EE:FF", "Test device", {"source": "00:11:22:33:44:55"})
    monkeypatch.setattr(BleakScanner, "find_device_by_address", AsyncMock(return_value=device))
    client = FakeClient({b"\x02\x01": b"\x03\x01\x2a"})
    with patched_establish(client), patch.object(link_module.asyncio, "sleep", AsyncMock()):
        exec(compile(match.group(1), str(README), "exec"), {"__name__": "readme"})
    assert capsys.readouterr().out == "03 01 2a via 00:11:22:33:44:55\n"
    assert not client.is_connected
