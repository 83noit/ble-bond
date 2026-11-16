"""Test helpers: a fake bleak client and a patch that hands it to :class:`BondedLink`.

``FakeClient`` answers a written frame from a table of replies, delivered as 20-byte
notifications, or stays silent. ``patched_establish`` replaces the connection call, so a test
needs neither an adapter nor a device::

    client = FakeClient({request: reply})
    with patched_establish(client) as calls:
        async with BondedLink(device, notify_uuid=UUID, assembler=...) as link:
            assert await link.request(request) == reply
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

from ble_bond import link as link_module

NOTIFICATION_SIZE = 20


class FakeClient:
    """Enough of ``BleakClient`` for the link: notifications, writes, drops and disconnects."""

    def __init__(self, replies: dict[bytes, bytes] | None = None, *, mtu_size: int = 23) -> None:
        self.replies = replies or {}
        self.writes: list[bytes] = []
        self.is_connected = True
        self.mtu_size = mtu_size
        self.notify_cb: Callable[[Any, bytearray], None] | None = None
        self.disconnected_callback: Callable[[Any], None] | None = None
        self.auto_reply = True
        self.unsubscribed = False
        self.connection_params: tuple[int, ...] | None = None

    async def start_notify(self, _char: str, callback: Callable[[Any, bytearray], None]) -> None:
        """Remember the notification callback."""
        self.notify_cb = callback

    async def stop_notify(self, _char: str) -> None:
        """Note the unsubscription."""
        self.unsubscribed = True

    async def write_gatt_char(self, _char: str, data: bytes, response: bool | None = None) -> None:
        """Record a write; answer once the written pieces make up a known request."""
        del response
        self.writes.append(bytes(data))
        if not self.auto_reply:
            return
        frame = b"".join(self.writes)
        if frame in self.replies:
            self.writes.clear()
            self.notify(self.replies[frame])

    def notify(self, data: bytes) -> None:
        """Deliver ``data`` as notifications of at most 20 bytes."""
        assert self.notify_cb is not None, "not subscribed"
        for start in range(0, len(data), NOTIFICATION_SIZE):
            self.notify_cb(None, bytearray(data[start : start + NOTIFICATION_SIZE]))

    async def set_connection_params(self, *params: int) -> None:
        """Record the requested connection parameters."""
        self.connection_params = params

    async def disconnect(self) -> None:
        """Close the link."""
        self.is_connected = False

    def drop(self) -> None:
        """Lose the link from the device's side."""
        self.is_connected = False
        assert self.disconnected_callback is not None
        self.disconnected_callback(self)


@contextlib.contextmanager
def patched_establish(client: Any) -> Iterator[list[dict[str, Any]]]:  # noqa: ANN401
    """Make every connection attempt return ``client``; yield the attempts' keyword arguments."""
    calls: list[dict[str, Any]] = []

    async def establish(_cls: Any, _device: Any, _name: str, **kwargs: Any) -> Any:  # noqa: ANN401
        calls.append(kwargs)
        if hasattr(client, "disconnected_callback"):
            client.disconnected_callback = kwargs["disconnected_callback"]
        return client

    with patch.object(link_module, "establish_connection", establish):
        yield calls
