"""BondedLink against a fake bleak client, with a length-prefixed framing.

Many of these come from grundfos-ble's and mira-ble's transport tests; the policy tests cover
what each of those devices needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import patch

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError

from ble_bond import link as link_module
from ble_bond.exceptions import (
    ConnectionLost,
    LinkError,
    PairingRequired,
    PairingUnsupported,
    RequestTimeout,
)
from ble_bond.link import BondedLink, LinkPolicy, SubscribeRetry
from ble_bond.testing import FakeClient, patched_establish

NOTIFY = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE = "0000ffe2-0000-1000-8000-00805f9b34fb"
PROXY = "00:11:22:33:44:55"


def frame(payload: bytes) -> bytes:
    """A test frame: one length byte (the whole frame) then the payload."""
    return bytes((len(payload) + 1,)) + payload


class LengthAssembler:
    """Frames are a length byte (counting itself) and that many bytes in all."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        self.buffer += data
        frames = []
        while self.buffer and len(self.buffer) >= self.buffer[0] > 0:
            size = self.buffer[0]
            frames.append(bytes(self.buffer[:size]))
            del self.buffer[:size]
        return frames

    def reset(self) -> None:
        self.buffer.clear()


REQUEST = frame(b"\x01get")
REPLY = frame(b"\x01answer")
LONG_REQUEST = frame(bytes(range(40)))


def _device(details: dict[str, Any] | None = None) -> BLEDevice:
    return BLEDevice("AA:BB:CC:DD:EE:FF", "Test device", details or {"source": PROXY})


def _link(**kwargs: Any) -> BondedLink:
    kwargs.setdefault("assembler", LengthAssembler())
    return BondedLink(_device(kwargs.pop("details", None)), notify_uuid=NOTIFY, **kwargs)


@contextlib.asynccontextmanager
async def _no_agent(_device: BLEDevice, **_kwargs: Any) -> AsyncIterator[None]:
    yield


@pytest.fixture(autouse=True)
def quick() -> Any:
    with patch.object(link_module, "RETRY_DELAY", 0):
        yield


QUICK = LinkPolicy(settle_after_subscribe=0, subscribe_retry_delay=0)


@pytest.fixture
def client() -> Any:
    client = FakeClient({REQUEST: REPLY})
    with patched_establish(client) as calls:
        client.calls = calls
        yield client


async def _pending(link: BondedLink, data: bytes, **kwargs: Any) -> asyncio.Task[bytes]:
    task = asyncio.create_task(link.request(data, **kwargs))
    await asyncio.sleep(0)
    return task


def _establish_sequence(*outcomes: Any) -> tuple[Callable[..., Any], list[dict[str, Any]]]:
    remaining = list(outcomes)
    calls: list[dict[str, Any]] = []

    async def establish(_cls: Any, _device: BLEDevice, _name: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, FakeClient):
            outcome.disconnected_callback = kwargs["disconnected_callback"]
        return outcome

    return establish, calls


# --- Requests -----------------------------------------------------------------------------------


async def test_roundtrip(client: FakeClient) -> None:
    link = _link(policy=QUICK)
    assert not link.is_connected
    await link.connect()
    await link.connect()  # idempotent
    assert await link.request(REQUEST) == REPLY
    assert link.reply_seen
    assert link.last_frames[0] == ("tx", REQUEST)
    assert link.last_frames[-1] == ("rx", REPLY)
    assert client.calls[0]["pair"] is True
    await link.disconnect()
    await link.disconnect()  # idempotent
    assert not link.is_connected
    assert client.unsubscribed


async def test_async_with(client: FakeClient) -> None:
    async with _link(policy=QUICK) as link:
        assert link.is_connected
    assert not link.is_connected


async def test_writes_go_in_20_byte_pieces_to_the_write_characteristic(client: FakeClient) -> None:
    client.auto_reply = False
    sent: list[str] = []
    original = client.write_gatt_char

    async def write(char: str, data: bytes, response: bool | None = None) -> None:
        sent.append(char)
        await original(char, data, response)

    client.write_gatt_char = write  # type: ignore[method-assign]
    link = _link(policy=QUICK, write_uuid=WRITE)
    await link.connect()
    await link.send(LONG_REQUEST)
    assert client.writes == [LONG_REQUEST[:20], LONG_REQUEST[20:40], LONG_REQUEST[40:]]
    assert set(sent) == {WRITE}


async def test_a_reply_the_check_refuses_is_not_taken(client: FakeClient) -> None:
    client.auto_reply = False
    link = _link(policy=QUICK, request_timeout=1)
    await link.connect()
    task = await _pending(link, REQUEST, accept=lambda reply: reply[1] == 0x01)
    client.notify(frame(b"\x02stray"))
    assert not task.done()
    client.notify(REPLY)
    assert await task == REPLY
    assert link.unsolicited == 1


async def test_a_check_that_raises_does_not_accept(client: FakeClient) -> None:
    client.auto_reply = False
    link = _link(policy=QUICK, request_timeout=0.05)
    await link.connect()

    def broken(_reply: bytes) -> bool:
        raise IndexError

    task = await _pending(link, REQUEST, accept=broken)
    client.notify(REPLY)
    with pytest.raises(RequestTimeout, match=r"1 frame\(s\) did not answer the request$"):
        await task


async def test_a_frame_with_no_request_waiting_is_unsolicited(client: FakeClient) -> None:
    link = _link(policy=QUICK)
    await link.connect()
    client.notify(REPLY)
    assert link.unsolicited == 1
    assert link.last_frames[-1] == ("rx", REPLY)


async def test_timeout_quotes_the_pieces_and_closes_the_link(client: FakeClient) -> None:
    """Lost notifications and a silent device look alike unless the pieces are kept."""
    client.auto_reply = False
    link = _link(policy=QUICK, request_timeout=0.05)
    await link.connect()
    task = await _pending(link, REQUEST)
    client.notify_cb(None, bytearray(b"\x20abc"))  # type: ignore[misc]
    with pytest.raises(RequestTimeout, match=r"1 piece\(s\) received: 20 61 62 63$"):
        await task
    assert not link.is_connected
    with pytest.raises(ConnectionLost):
        await link.request(REQUEST)


async def test_timeout_with_nothing_received(client: FakeClient) -> None:
    client.auto_reply = False
    link = _link(policy=QUICK)
    await link.connect()
    with pytest.raises(RequestTimeout, match=r"nothing received$"):
        await link.request(REQUEST, timeout=0)


async def test_quoted_pieces_are_bounded(client: FakeClient) -> None:
    client.auto_reply = False
    link = _link(policy=QUICK, request_timeout=0.05)
    await link.connect()
    task = await _pending(link, REQUEST)
    for _ in range(link_module.MAX_PIECES_REPORTED + 1):
        client.notify_cb(None, bytearray(b"\x7f"))  # type: ignore[misc]
    with pytest.raises(RequestTimeout, match=r"9 piece\(s\) received: (7f \| ){8}\.\.\.$"):
        await task


async def test_kept_pieces_are_bounded(client: FakeClient) -> None:
    link = _link(policy=QUICK)
    await link.connect()
    for _ in range(link_module.MAX_PIECES_KEPT * 2):
        client.notify_cb(None, bytearray(b"\x7f"))  # type: ignore[misc]
    assert len(link._pieces) == link_module.MAX_PIECES_KEPT


async def test_a_cancelled_request_closes_the_link(client: FakeClient) -> None:
    client.auto_reply = False
    link = _link(policy=QUICK)
    await link.connect()
    task = await _pending(link, REQUEST)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not link.is_connected
    assert client.unsubscribed


async def test_a_failed_write_closes_the_link_and_is_no_pairing_evidence(
    client: FakeClient,
) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> None:
        raise BleakError("[org.bluez.Error.AuthenticationFailed] write failed")

    client.write_gatt_char = failing  # type: ignore[method-assign]
    link = _link(policy=QUICK)
    await link.connect()
    with pytest.raises(LinkError, match="write failed") as caught:
        await link.request(REQUEST)
    assert not isinstance(caught.value, PairingRequired)
    assert not link.is_connected
    client.is_connected = True
    await link.connect()
    with pytest.raises(LinkError, match="write failed"):
        await link.send(REQUEST)
    assert not link.is_connected


async def test_a_dropped_link_fails_the_request_and_calls_back(client: FakeClient) -> None:
    client.auto_reply = False
    dropped: list[bool] = []
    link = _link(policy=QUICK, disconnected_callback=lambda: dropped.append(True))
    await link.connect()
    task = await _pending(link, REQUEST)
    client.drop()
    with pytest.raises(ConnectionLost):
        await task
    assert dropped == [True]
    assert not link.is_connected


async def test_our_own_disconnect_does_not_call_back(client: FakeClient) -> None:
    dropped: list[bool] = []
    link = _link(policy=QUICK, disconnected_callback=lambda: dropped.append(True))
    await link.connect()
    await link.disconnect()
    assert client.disconnected_callback is not None
    client.disconnected_callback(client)
    link._on_disconnect(FakeClient())  # type: ignore[arg-type]
    assert dropped == []


async def test_not_connected() -> None:
    with pytest.raises(ConnectionLost):
        await _link().request(REQUEST)


# --- Connecting ---------------------------------------------------------------------------------


async def test_concurrent_connects_share_one_connection() -> None:
    calls = 0

    async def establish(_cls: Any, _device: BLEDevice, _name: str, **kwargs: Any) -> FakeClient:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        client = FakeClient()
        client.disconnected_callback = kwargs["disconnected_callback"]
        return client

    link = _link(policy=QUICK)
    with patch.object(link_module, "establish_connection", establish):
        await asyncio.gather(link.connect(), link.connect())
    assert calls == 1


async def test_a_refusal_ends_the_retries_at_once() -> None:
    establish, calls = _establish_sequence(
        BleakError("Failed to connect after 1 attempt(s): Pairing failed due to error: 82"),
        FakeClient(),
    )
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(PairingRequired) as caught,
    ):
        await _link(policy=QUICK, max_attempts=3).connect()
    assert caught.value.error_code == 82
    assert caught.value.refused
    assert len(calls) == 1
    assert calls[0]["max_attempts"] == 1


async def test_bluez_authentication_failed_is_a_refusal() -> None:
    establish, _ = _establish_sequence(
        BleakError("[org.bluez.Error.AuthenticationFailed] Authentication Failed")
    )
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(PairingRequired),
    ):
        await _link(policy=QUICK, max_attempts=1).connect()


@pytest.mark.parametrize("code", [99, 102, 96, 92, -1])
async def test_transient_smp_reasons_are_not_refusals(code: int) -> None:
    establish, _ = _establish_sequence(BleakError(f"Pairing failed due to error: {code}"))
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(LinkError) as caught,
    ):
        await _link(policy=QUICK, max_attempts=1).connect()
    assert not isinstance(caught.value, PairingRequired | PairingUnsupported)
    assert caught.value.error_code == code


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("Pairing failed due to error: 820", False),
        ("pairing failed, 82 devices nearby", False),
    ],
    ids=["other-code", "loose-text"],
)
async def test_only_bleak_esphome_refusal_text_counts(text: str, refused: bool) -> None:
    establish, _ = _establish_sequence(BleakError(text))
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(LinkError) as caught,
    ):
        await _link(policy=QUICK, max_attempts=1).connect()
    assert isinstance(caught.value, PairingRequired) is refused


@pytest.mark.parametrize(
    "error",
    [
        NotImplementedError("Pairing is not available in this version ESPHome; upgrade it."),
        BleakError("Pairing failed due to error: 129"),
    ],
    ids=["bleak-esphome", "reason-129"],
)
async def test_a_proxy_that_cannot_pair_is_reported_at_once(error: Exception) -> None:
    establish, calls = _establish_sequence(error, FakeClient())
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(PairingUnsupported),
    ):
        await _link(policy=QUICK, max_attempts=2).connect()
    assert len(calls) == 1


async def test_a_proxy_that_cannot_pair_may_be_tried_unpaired() -> None:
    """mira-ble: a bond the proxy already holds may still encrypt the link."""
    client = FakeClient()
    establish, calls = _establish_sequence(NotImplementedError("Pairing is not available"), client)
    policy = LinkPolicy(settle_after_subscribe=0, retry_unpaired=True)
    with patch.object(link_module, "establish_connection", establish):
        link = _link(policy=policy, max_attempts=1)
        await link.connect()
    assert link.is_connected
    assert [call["pair"] for call in calls] == [True, False]


async def test_an_ordinary_failure_is_retried() -> None:
    establish, calls = _establish_sequence(BleakError("no slot"), FakeClient())
    with patch.object(link_module, "establish_connection", establish):
        link = _link(policy=QUICK, max_attempts=2)
        await link.connect()
    assert link.is_connected
    assert len(calls) == 2


async def test_a_failed_attempt_does_not_call_back() -> None:
    dropped: list[bool] = []

    async def establish(_cls: Any, _device: BLEDevice, _name: str, **kwargs: Any) -> FakeClient:
        kwargs["disconnected_callback"](FakeClient())
        raise BleakError("Disconnected during connect")

    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(LinkError),
    ):
        await _link(
            policy=QUICK, max_attempts=1, disconnected_callback=lambda: dropped.append(True)
        ).connect()
    assert dropped == []


async def test_the_connect_timeout_is_enforced() -> None:
    async def establish(*_args: Any, **_kwargs: Any) -> FakeClient:
        await asyncio.sleep(30)
        raise AssertionError

    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(LinkError, match="timed out"),
    ):
        await _link(policy=QUICK, max_attempts=1, connect_timeout=0.01).connect()


@pytest.mark.parametrize("error", [EOFError("x"), BrokenPipeError("x"), AttributeError("x")])
async def test_foreign_link_errors_are_translated(error: Exception) -> None:
    establish, _ = _establish_sequence(error)
    with (
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(LinkError),
    ):
        await _link(policy=QUICK, max_attempts=1).connect()


async def test_each_attempt_asks_for_the_current_device() -> None:
    first = _device()
    second = BLEDevice("AA:BB:CC:DD:EE:FF", "Test device", {"source": "66:77:88:99:AA:BB"})
    devices = iter([first, None, second])
    seen: list[BLEDevice] = []

    async def establish(_cls: Any, device: BLEDevice, _name: str, **_kwargs: Any) -> FakeClient:
        seen.append(device)
        if len(seen) < 3:
            raise BleakError("no slot")
        return FakeClient()

    with patch.object(link_module, "establish_connection", establish):
        link = BondedLink(
            first,
            notify_uuid=NOTIFY,
            assembler=LengthAssembler(),
            policy=QUICK,
            max_attempts=3,
            device_callback=lambda: next(devices),
        )
        await link.connect()
    assert seen == [first, first, second]
    assert link.device is second
    assert link.last_source == "66:77:88:99:AA:BB"


@pytest.mark.parametrize(
    ("details", "cached"),
    [({"source": PROXY}, True), ({"path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"}, False)],
    ids=["proxy", "local-bluez"],
)
async def test_a_local_adapter_waits_for_resolved_services(
    details: dict[str, Any], cached: bool
) -> None:
    client = FakeClient()
    with (
        patched_establish(client) as calls,
        patch.object(link_module, "bluez_pairing_agent", _no_agent),
    ):
        await _link(policy=QUICK, details=details).connect()
    assert calls[0]["use_services_cache"] is cached


async def test_a_missing_characteristic_clears_the_service_cache(client: FakeClient) -> None:
    cleared: list[bool] = []

    async def missing(*_args: Any) -> None:
        raise BleakCharacteristicNotFoundError(NOTIFY)

    async def clear_cache() -> bool:
        cleared.append(True)
        return True

    client.start_notify = missing  # type: ignore[method-assign]
    client.clear_cache = clear_cache  # type: ignore[attr-defined]
    link = _link(policy=QUICK)
    with pytest.raises(LinkError, match="not found"):
        await link.connect()
    assert cleared == [True]
    assert not link.is_connected


# --- Policies -----------------------------------------------------------------------------------


async def test_pauses_happen_where_the_policy_puts_them(client: FakeClient) -> None:
    events: list[str] = []

    async def sleep(delay: float) -> None:
        events.append(f"sleep {delay}")

    original = client.start_notify

    async def start_notify(char: str, callback: Callable[[Any, bytearray], None]) -> None:
        events.append("subscribe")
        await original(char, callback)

    client.start_notify = start_notify  # type: ignore[method-assign]
    policy = LinkPolicy(settle_after_pair=0.5, settle_after_subscribe=0.25)
    with patch.object(link_module.asyncio, "sleep", sleep):
        await _link(policy=policy).connect()
    assert events == ["sleep 0.5", "subscribe", "sleep 0.25"]


async def test_the_default_pauses_only_after_subscribing(client: FakeClient) -> None:
    """esphome#22651: requests within 200 ms of subscribing were lost."""
    events: list[str] = []

    async def sleep(delay: float) -> None:
        events.append(f"sleep {delay}")

    with patch.object(link_module.asyncio, "sleep", sleep):
        await _link().connect()
    assert events == ["sleep 0.5"]


async def test_subscribing_is_retried_once_after_any_link_error(client: FakeClient) -> None:
    attempts: list[int] = []
    original = client.start_notify

    async def flaky(char: str, callback: Callable[[Any, bytearray], None]) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise BleakError("GATT error")
        await original(char, callback)

    client.start_notify = flaky  # type: ignore[method-assign]
    link = _link(policy=QUICK)
    await link.connect()
    assert link.is_connected
    assert len(attempts) == 2


async def test_a_subscription_failing_twice_is_never_pairing_evidence_by_default(
    client: FakeClient,
) -> None:
    async def refused(*_args: Any) -> None:
        raise BleakError("Insufficient authentication")

    client.start_notify = refused  # type: ignore[method-assign]
    with pytest.raises(LinkError) as caught:
        await _link(policy=QUICK).connect()
    assert not isinstance(caught.value, PairingRequired)


async def test_insufficient_authentication_can_be_weak_evidence(client: FakeClient) -> None:
    """mira-ble: a link that stays unencrypted says something, but not much, about the bond."""

    async def refused(*_args: Any) -> None:
        raise BleakError("ATT error: insufficient authentication (5)")

    client.start_notify = refused  # type: ignore[method-assign]
    policy = LinkPolicy(
        settle_after_subscribe=0,
        subscribe_retry=SubscribeRetry.INSUFFICIENT_AUTHENTICATION,
        subscribe_retry_delay=0,
        insufficient_authentication_is_evidence=True,
    )
    with pytest.raises(PairingRequired) as caught:
        await _link(policy=policy).connect()
    assert not caught.value.refused
    assert caught.value.error_code == link_module.INSUFFICIENT_AUTHENTICATION_CODE


async def test_retry_only_on_insufficient_authentication(client: FakeClient) -> None:
    attempts: list[int] = []

    async def failing(*_args: Any) -> None:
        attempts.append(1)
        raise BleakError("GATT error 0x0e")

    client.start_notify = failing  # type: ignore[method-assign]
    policy = LinkPolicy(
        settle_after_subscribe=0, subscribe_retry=SubscribeRetry.INSUFFICIENT_AUTHENTICATION
    )
    with pytest.raises(LinkError):
        await _link(policy=policy).connect()
    assert len(attempts) == 1


async def test_writes_can_follow_the_mtu(client: FakeClient) -> None:
    client.auto_reply = False
    client.mtu_size = 65
    await (link := _link(policy=LinkPolicy(settle_after_subscribe=0, write_chunk=None))).connect()
    await link.send(LONG_REQUEST)
    assert client.writes == [LONG_REQUEST]


async def test_an_unknown_mtu_means_20_byte_writes(client: FakeClient) -> None:
    client.auto_reply = False
    del client.mtu_size
    await (link := _link(policy=LinkPolicy(settle_after_subscribe=0, write_chunk=None))).connect()
    await link.send(LONG_REQUEST)
    assert len(client.writes[0]) == 20


async def test_connection_parameters_are_requested_best_effort(client: FakeClient) -> None:
    policy = LinkPolicy(settle_after_subscribe=0, connection_params=(24, 60, 0, 2000))
    await _link(policy=policy).connect()
    assert client.connection_params == (24, 60, 0, 2000)

    async def refuse(*_params: int) -> None:
        raise BleakError("not supported")

    client.set_connection_params = refuse  # type: ignore[method-assign]
    client.is_connected = True
    link = _link(policy=policy)
    await link.connect()
    assert link.is_connected


# --- Which path an attempt used -----------------------------------------------------------------


class _Scanner:
    def __init__(self, source: str) -> None:
        self.source = source


class _HaLikeClient:
    """Stands in for habluetooth's client: picks its backend in a private method."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def _async_get_best_available_backend_and_device(self, _manager: Any) -> Any:
        return type("Backend", (), {"scanner": _Scanner("proxy-b")})()


async def test_the_source_home_assistant_chose_is_recorded_for_a_refusal() -> None:
    async def establish(cls: Any, _device: BLEDevice, _name: str, **_kwargs: Any) -> Any:
        cls()._async_get_best_available_backend_and_device(None)
        raise BleakError("Pairing failed due to error: 82")

    link = _link(policy=QUICK, max_attempts=1)
    with (
        patch.object(
            link_module.bleak_retry_connector, "BleakClientWithServiceCache", _HaLikeClient
        ),
        patch.object(link_module, "establish_connection", establish),
        pytest.raises(PairingRequired),
    ):
        await link.connect()
    assert link.last_source == "proxy-b"


async def test_without_home_assistant_the_source_comes_from_the_device(client: FakeClient) -> None:
    link = _link(policy=QUICK)
    assert link.last_source is None
    await link.connect()
    assert link.last_source == PROXY


def test_plain_bleak_clients_are_left_alone() -> None:
    assert link_module._source_recording(FakeClient) is FakeClient


def test_the_default_policy_keeps_replies_above_the_supervision_timeout() -> None:
    """A stalled link that recovers within 8 s must still answer (ha-grundfos-ble#3)."""
    assert LinkPolicy().request_timeout > 8.0


# --- Logging ------------------------------------------------------------------------------------


async def test_frames_stay_out_of_the_links_debug_log(
    client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging for ble_bond shows the link; the frames need ble_bond.frames."""
    caplog.set_level(logging.DEBUG, logger="ble_bond")
    link = _link(policy=QUICK)
    await link.connect()
    await link.request(REQUEST)
    messages = [r.getMessage() for r in caplog.records]
    assert any("connected and subscribed" in m for m in messages)
    assert not any(": tx " in m or ": rx " in m for m in messages)

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="ble_bond.frames")
    await link.request(REQUEST)
    frames = [r for r in caplog.records if r.name == "ble_bond.frames"]
    assert [r.getMessage().split(": ")[1][:2] for r in frames] == ["tx", "rx"]


def test_a_configured_frames_level_is_kept() -> None:
    assert logging.getLogger("ble_bond.frames").level in (logging.INFO, logging.DEBUG)
