"""A bonded Bluetooth LE link to a device that answers one request at a time.

The link takes a ``BLEDevice`` from the caller and never scans. On every connection it pairs
(on an ESPHome proxy this is "start encryption": a re-encrypt when the proxy already holds a
bond, SMP pairing otherwise), subscribes to the reply characteristic, lets the link settle, and
then runs one request at a time: write the frame, wait for a reply the caller accepts.

Pairing on every connection is what makes the same code work on a local adapter and on an
ESPHome proxy. A local BlueZ adapter applies a stored bond by itself; a proxy does not, and
only an explicit pair call re-arms encryption from its stored key. Devices of this kind accept a
new bond only for a few seconds after a button press; outside that window ESP-IDF reports
reason 82 and BlueZ ``AuthenticationFailed``, both of which become
:class:`~ble_bond.exceptions.PairingRequired`.

Replies carry no request id, so three rules follow:

* one request is in flight at a time;
* a frame the caller's ``accept`` check does not recognise is logged as unsolicited and the
  wait goes on, so a stray frame cannot shift every later reply by one;
* a request that does not complete (timeout, cancellation, write error, lost link) closes the
  link, so its late reply cannot be taken for the next request's answer.

What differs between devices (pauses, retries, how weak evidence about the bond is reported,
write size) is a :class:`LinkPolicy`; framing is the caller's :class:`Assembler`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, Self

import bleak_retry_connector
from bleak import BleakClient
from bleak.exc import BleakCharacteristicNotFoundError, BleakError
from bleak_retry_connector import establish_connection

from ble_bond.bluez import bluez_device_path, bluez_pairing_agent
from ble_bond.exceptions import (
    ConnectionLost,
    LinkError,
    PairingRequired,
    PairingUnsupported,
    RequestTimeout,
)

if TYPE_CHECKING:
    from types import TracebackType

    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

type Accept = Callable[[bytes], bool]
"""Says whether a complete frame is the reply the pending request waits for."""

DEFAULT_WRITE_CHUNK: Final = 20
"""Bytes one write carries when the negotiated MTU is not used or not known (ATT 23, less 3)."""
ATT_OVERHEAD: Final = 3
MAX_PIECES_REPORTED: Final = 8
"""Notification pieces of an unfinished reply quoted in a RequestTimeout."""
MAX_PIECES_KEPT: Final = 64
"""Notification pieces kept for that report; older ones are dropped."""
RETRY_DELAY: Final = 0.25
"""Seconds between connection attempts."""

PAIRING_REFUSED_CODES: Final = frozenset({82})
"""ESP-IDF SMP reasons that mean "no bond and the pairing window is closed".

Others seen in the same message (timeouts, busy, internal errors) are transient."""
PAIRING_UNSUPPORTED_CODES: Final = frozenset({129})
"""Reasons from proxies that cannot pair at all."""
INSUFFICIENT_AUTHENTICATION_CODE: Final = 5
"""The ATT error behind a subscription refused for want of encryption."""

_PAIRING_FAILED = re.compile(r"Pairing failed due to error: (-?\d+)")
"""bleak-esphome's report of a failed pairing; it survives bleak-retry-connector's wrapping."""
_PAIRING_UNAVAILABLE: Final = "Pairing is not available"
"""bleak-esphome's NotImplementedError when the proxy's ESPHome has no pairing support."""
_INSUFFICIENT_AUTHENTICATION = re.compile(r"insufficient (authentication|encryption)", re.I)
BLUEZ_AUTHENTICATION_FAILED: Final = "org.bluez.Error.AuthenticationFailed"
"""BlueZ's report of a refused pairing; with the pairing agent up it means a missed window."""
_LINK_ERRORS: Final = (BleakError, EOFError, BrokenPipeError, AttributeError, NotImplementedError)
"""What bleak backends raise for link trouble; bleak-retry-connector retries most of these.

The ``except`` clauses that use it wrap only bleak calls, so an ``AttributeError`` here comes
from a backend whose link went away under it, and a ``NotImplementedError`` from a proxy that
cannot pair (or CoreBluetooth, which pairs by itself).
"""


class SubscribeRetry(StrEnum):
    """When a failed subscription is tried once more on the same link."""

    ANY_LINK_ERROR = "any_link_error"
    """After any link error (encryption may still be settling)."""
    INSUFFICIENT_AUTHENTICATION = "insufficient_authentication"
    """Only after "insufficient authentication": the link is up but not yet encrypted."""
    NEVER = "never"


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkPolicy:
    """How one kind of device wants its link handled. The defaults suit most devices.

    Each value is a measurement or a choice made for a device, so a device library states them
    in one place, with where they come from.
    """

    settle_after_pair: float = 0.0
    """Seconds between the encrypted connect and subscribing.

    A pair call can return before link-layer encryption is up; a subscription landing in that
    gap is refused as "insufficient authentication"."""
    settle_after_subscribe: float = 0.5
    """Seconds between subscribing and the first write.

    ESPHome measured on a Grundfos ALPHA3 that requests sent 0-200 ms after notifications were
    enabled were lost in 4-5 of 10 connections, and none at 300-500 ms (esphome/esphome#22651)."""
    subscribe_retry: SubscribeRetry = SubscribeRetry.ANY_LINK_ERROR
    subscribe_retry_delay: float = 0.5
    insufficient_authentication_is_evidence: bool = False
    """Report a subscription refused twice for want of encryption as
    ``PairingRequired(refused=False)`` (weak evidence about the bond) rather than a plain
    :class:`~ble_bond.exceptions.LinkError`."""
    retry_unpaired: bool = False
    """When the adapter or proxy cannot pair at all, connect once more without pairing: a bond
    it already holds may still encrypt the link. Otherwise raise
    :class:`~ble_bond.exceptions.PairingUnsupported`. Only for devices that answer on an
    unencrypted link or encrypt by themselves."""
    write_chunk: int | None = DEFAULT_WRITE_CHUNK
    """Bytes per write; None writes as much as the negotiated MTU allows (at least 20)."""
    chunk_spacing: float = 0.025
    """Seconds between the writes of one frame."""
    connection_params: tuple[int, int, int, int] | None = None
    """(min interval, max interval, latency, supervision timeout) in BLE units, requested
    best-effort where the backend can (Home Assistant's client can, through a proxy)."""
    connect_timeout: float = 10.0
    """Seconds allowed for one connect-and-pair attempt."""
    request_timeout: float = 10.0
    """Seconds to wait for a whole reply, write included.

    Kept above the 8 s link supervision timeout Home Assistant and ESPHome proxies use, so a
    link that stalls and recovers answers late rather than being torn down."""


class Assembler(Protocol):
    """Puts notification pieces together into whole frames (the device library's framing)."""

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        """Add one notification and return every frame it completed."""
        ...

    def reset(self) -> None:
        """Forget any partial frame."""
        ...


# --- Which adapter or proxy an attempt used ---------------------------------------------------

_attempt_source: ContextVar[list[str | None] | None] = ContextVar(
    "ble_bond_attempt_source", default=None
)
"""Where the client class below records the Bluetooth source of the current attempt."""


@functools.cache
def _source_recording(base: Any) -> Any:  # noqa: ANN401 - a class only known at run time
    """Wrap Home Assistant's client class so that it records the scanner it connects through.

    Home Assistant (habluetooth) replaces bleak's client with one that picks a local adapter or
    a proxy at connect time, by signal and free slots, and keeps the choice private until the
    connection succeeds. A refused attempt must be pinned on the path that was asked, so this
    subclass reads the choice from that private method as it is made. With plain bleak the
    method does not exist and ``base`` is returned unchanged.
    """
    if not hasattr(base, "_async_get_best_available_backend_and_device"):
        return base

    class SourceRecordingClient(base):  # type: ignore[misc]
        def _async_get_best_available_backend_and_device(self, manager: Any) -> Any:  # noqa: ANN401
            backend = super()._async_get_best_available_backend_and_device(manager)
            if (slot := _attempt_source.get()) is not None:
                with contextlib.suppress(Exception):
                    slot[0] = backend.scanner.source
            return backend

    return SourceRecordingClient


def _details_source(device: BLEDevice) -> str | None:
    details = device.details
    if isinstance(details, dict) and isinstance(source := details.get("source"), str):
        return source
    return None


# --- The link -----------------------------------------------------------------------------------


class BondedLink:
    """One bonded BLE link to a device that answers one request at a time.

    ``notify_uuid`` is the characteristic replies arrive on; ``write_uuid`` the one requests are
    written to (the same one by default). ``assembler`` turns notification pieces into frames.

    ``device_callback``, when given, is asked for the device again before every connection
    attempt, so a retry goes through whichever adapter or proxy now hears the device best. It
    may return None to keep the last device.

    ``expect_new_bond`` says a new bond may be made on this link (the device's button was just
    pressed). Only then is a BlueZ pairing agent registered on a local adapter; without it an
    unbonded local adapter's pairing is refused, which is what a missed window looks like.

    ``disconnected_callback`` is called when the device or the adapter drops the live link, not
    when the link is closed on purpose.

    ``async with BondedLink(...) as link:`` connects on entry and disconnects on exit.
    """

    def __init__(
        self,
        device: BLEDevice,
        *,
        notify_uuid: str,
        assembler: Assembler,
        write_uuid: str | None = None,
        policy: LinkPolicy | None = None,
        name: str | None = None,
        max_attempts: int = 2,
        connect_timeout: float | None = None,
        request_timeout: float | None = None,
        disconnected_callback: Callable[[], None] | None = None,
        device_callback: Callable[[], BLEDevice | None] | None = None,
        expect_new_bond: bool = False,
    ) -> None:
        self._device = device
        self._notify_uuid = notify_uuid
        self._write_uuid = write_uuid or notify_uuid
        self._assembler = assembler
        self._policy = policy or LinkPolicy()
        self._name = name or device.name or device.address
        self._max_attempts = max(1, max_attempts)
        self._connect_timeout = (
            self._policy.connect_timeout if connect_timeout is None else connect_timeout
        )
        self._request_timeout = (
            self._policy.request_timeout if request_timeout is None else request_timeout
        )
        self._disconnected_callback = disconnected_callback
        self._device_callback = device_callback
        self._expect_new_bond = expect_new_bond
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._pending: asyncio.Future[bytes] | None = None
        self._accept: Accept | None = None
        self._pieces: deque[bytes] = deque(maxlen=MAX_PIECES_KEPT)
        self._unmatched = 0
        self._reply_seen = False
        self._last_source: str | None = None
        self.unsolicited = 0
        """Frames received that no request was waiting for, or that no check accepted."""
        self.last_frames: deque[tuple[str, bytes]] = deque(maxlen=48)
        """Recent traffic as ("tx" | "rx", frame), for diagnostics and fixture capture."""

    async def __aenter__(self) -> Self:
        """Connect, as :meth:`connect` does, and return the link."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Disconnect, whatever happened in the block."""
        await self.disconnect()

    @property
    def device(self) -> BLEDevice:
        """The device this link talks to (the latest one ``device_callback`` gave)."""
        return self._device

    @property
    def policy(self) -> LinkPolicy:
        """How this link is handled."""
        return self._policy

    @property
    def is_connected(self) -> bool:
        """True while the BLE link is up."""
        return self._client is not None and self._client.is_connected

    @property
    def reply_seen(self) -> bool:
        """True once the device has sent a frame on the current or last link."""
        return self._reply_seen

    @property
    def last_source(self) -> str | None:
        """The Bluetooth source (adapter or proxy) the latest connection attempt went through.

        Under Home Assistant this is the scanner habluetooth chose for the attempt, refused
        attempts included, read from a private method; elsewhere, or if that changes, it is the
        source the device was last heard through when the attempt started. None when neither
        is known. A refusal says something about the bond only when it came through the
        adapter or proxy that holds it.
        """
        return self._last_source

    # -- connecting --------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect through the device's path, pair (encrypt), subscribe and let the link settle.

        Retries run here, one connection attempt at a time, so that a refusal on any attempt is
        seen; bleak-retry-connector reports only the last attempt's error. Concurrent calls
        share one connection.
        """
        async with self._connect_lock:
            if self.is_connected:
                return
            self._assembler.reset()
            self._pieces.clear()
            self._reply_seen = False
            client = await self._connect_with_retries()
            self._client = client
            try:
                await self._set_connection_params(client)
                if self._policy.settle_after_pair:
                    await asyncio.sleep(self._policy.settle_after_pair)
                await self._subscribe(client)
            except BaseException:
                await self.disconnect()
                raise
            _LOGGER.debug("%s: connected and subscribed", self._name)
            if self._policy.settle_after_subscribe:
                await asyncio.sleep(self._policy.settle_after_subscribe)

    async def _connect_with_retries(self) -> BleakClient:
        last: LinkError | None = None
        for attempt in range(self._max_attempts):
            if attempt:
                await asyncio.sleep(RETRY_DELAY)
            if self._device_callback is not None:
                self._device = self._device_callback() or self._device
            slot: list[str | None] = [None]
            token = _attempt_source.set(slot)
            try:
                async with (
                    asyncio.timeout(self._connect_timeout),
                    bluez_pairing_agent(self._device, enabled=self._expect_new_bond),
                ):
                    return await self._establish_pairing()
            except _LINK_ERRORS as err:
                last = self._translate(err)
                last.__cause__ = err
                if isinstance(last, (PairingRequired, PairingUnsupported)):
                    raise last from err
            except TimeoutError as err:
                last = LinkError(f"timed out connecting to {self._name}")
                last.__cause__ = err
            finally:
                _attempt_source.reset(token)
                self._last_source = slot[0] or _details_source(self._device)
        assert last is not None
        raise last

    async def _establish_pairing(self) -> BleakClient:
        """Connect and pair; with ``retry_unpaired``, retry once unpaired if pairing is absent."""
        try:
            return await self._establish(pair=True)
        except _LINK_ERRORS as err:
            if not self._policy.retry_unpaired or not self._pairing_unsupported(err):
                raise
            _LOGGER.debug("%s: the adapter cannot pair (%s); trying unpaired", self._name, err)
            return await self._establish(pair=False)

    async def _establish(self, *, pair: bool) -> BleakClient:
        return await establish_connection(
            # Looked up at call time: Home Assistant swaps the class in after import.
            _source_recording(bleak_retry_connector.BleakClientWithServiceCache),
            self._device,
            self._name,
            disconnected_callback=self._on_disconnect,
            max_attempts=1,
            pair=pair,
            # With bleak's own cache a local connect returns before bluetoothd has finished
            # setting up GATT; without it bleak waits for ServicesResolved, which BlueZ's cache
            # for a bonded device keeps quick. A proxy keeps its cache: it reports the link only
            # once encrypted and set up.
            use_services_cache=bluez_device_path(self._device) is None,
        )

    async def _set_connection_params(self, client: BleakClient) -> None:
        """Ask for the policy's connection parameters where the backend can; never fail on it."""
        params = self._policy.connection_params
        setter = getattr(client, "set_connection_params", None)
        if params is None or setter is None:
            return
        try:
            await setter(*params)
        except Exception as err:  # best effort: every backend differs
            _LOGGER.debug("%s: connection parameters not set: %s", self._name, err)

    async def _subscribe(self, client: BleakClient) -> None:
        """Subscribe to replies, retrying once on the same link as the policy says."""
        policy = self._policy
        for final_try in (False, True):
            try:
                await client.start_notify(self._notify_uuid, self._on_notify)
            except BleakCharacteristicNotFoundError as err:
                # The service cache is stale (yalexs-ble finds the same), or this device does
                # not offer the characteristic: drop the cache so the next connection resolves
                # the services afresh.
                if hasattr(client, "clear_cache"):
                    with contextlib.suppress(Exception):
                        await client.clear_cache()
                raise LinkError(
                    f"{self._name}: characteristic {self._notify_uuid} not found; the device "
                    f"does not offer it, or a cached service list is stale ({err})"
                ) from err
            except (*_LINK_ERRORS, TimeoutError) as err:
                unencrypted = bool(_INSUFFICIENT_AUTHENTICATION.search(str(err)))
                retry = policy.subscribe_retry is SubscribeRetry.ANY_LINK_ERROR or (
                    policy.subscribe_retry is SubscribeRetry.INSUFFICIENT_AUTHENTICATION
                    and unencrypted
                )
                if final_try or not retry or not client.is_connected:
                    raise self._subscribe_error(err, unencrypted=unencrypted) from err
                _LOGGER.debug("%s: subscribing failed (%s); retrying once", self._name, err)
                await asyncio.sleep(policy.subscribe_retry_delay)
            else:
                return

    def _subscribe_error(self, err: BaseException, *, unencrypted: bool) -> LinkError:
        """Translate a failed subscription: weak pairing evidence only if the policy says so."""
        if unencrypted and self._policy.insufficient_authentication_is_evidence:
            # The link came up but was not encrypted, twice. Weak evidence about the bond.
            return PairingRequired(
                f"{self._name}: {err}", INSUFFICIENT_AUTHENTICATION_CODE, refused=False
            )
        return self._link_error(err)

    async def disconnect(self) -> None:
        """Unsubscribe and drop the link; safe to call when not connected."""
        client, self._client = self._client, None
        if client is None:
            return
        if client.is_connected:
            # Leaving notifications running can let a native callback fire into a torn-down
            # client (seen crashing a process on exit).
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1.0):
                    await client.stop_notify(self._notify_uuid)
        with contextlib.suppress(Exception):
            await client.disconnect()

    # -- requests ----------------------------------------------------------------------------

    async def request(
        self, frame: bytes, *, accept: Accept | None = None, timeout: float | None = None
    ) -> bytes:
        """Write ``frame`` and return the device's reply as a whole frame, one at a time.

        ``accept`` says whether a complete frame is this request's reply; one that is not is
        logged as unsolicited and the wait goes on within the same deadline. Without it the
        first frame answers. A request that does not complete with a reply, whether it timed
        out, was cancelled or failed to write, closes the link.
        """
        async with self._lock:
            client = self._require_client()
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bytes] = loop.create_future()
            self._assembler.reset()
            self._pieces.clear()
            self._unmatched = 0
            self._pending, self._accept = future, accept
            replied = False
            try:
                async with asyncio.timeout(self._request_timeout if timeout is None else timeout):
                    await self._write(client, frame)
                    reply = await future
                replied = True
            except TimeoutError as err:
                raise RequestTimeout(
                    f"{self._name}: no reply to {frame.hex(' ')}; {self._received()}"
                ) from err
            finally:
                self._pending = self._accept = None
                if future.done() and not future.cancelled():
                    future.exception()  # mark as retrieved
                else:
                    future.cancel()
                if not replied:
                    await self.disconnect()
            return reply

    async def send(self, frame: bytes) -> None:
        """Write ``frame`` without waiting for a reply; a failed write closes the link."""
        async with self._lock:
            client = self._require_client()
            try:
                await self._write(client, frame)
            except BaseException:
                await self.disconnect()
                raise

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise ConnectionLost(f"{self._name}: not connected")
        return self._client

    async def _write(self, client: BleakClient, frame: bytes) -> None:
        self.last_frames.append(("tx", frame))
        _LOGGER.debug("%s: tx %s", self._name, frame.hex(" "))
        size = self._write_size(client)
        for start in range(0, len(frame), size):
            if start:
                await asyncio.sleep(self._policy.chunk_spacing)
            try:
                await client.write_gatt_char(
                    self._write_uuid, frame[start : start + size], response=False
                )
            except _LINK_ERRORS as err:
                raise self._link_error(err) from err

    def _write_size(self, client: BleakClient) -> int:
        if self._policy.write_chunk is not None:
            return self._policy.write_chunk
        try:
            mtu = int(client.mtu_size)
        except Exception:  # some backends only know the MTU after an exchange
            return DEFAULT_WRITE_CHUNK
        return max(DEFAULT_WRITE_CHUNK, mtu - ATT_OVERHEAD)

    def _received(self) -> str:
        """Describe what came for a request that got no accepted reply."""
        unmatched = (
            f"; {self._unmatched} frame(s) did not answer the request" if self._unmatched else ""
        )
        if not self._pieces:
            return "nothing received" + unmatched
        pieces = list(self._pieces)
        quoted = " | ".join(piece.hex(" ") for piece in pieces[:MAX_PIECES_REPORTED])
        more = " | ..." if len(pieces) > MAX_PIECES_REPORTED else ""
        return f"{len(pieces)} piece(s) received: {quoted}{more}{unmatched}"

    def _on_notify(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        self._pieces.append(bytes(data))
        for frame in self._assembler.feed(data):
            self.last_frames.append(("rx", frame))
            self._reply_seen = True
            _LOGGER.debug("%s: rx %s", self._name, frame.hex(" "))
            pending = self._pending
            if pending is None or pending.done():
                self.unsolicited += 1
                _LOGGER.debug("%s: unsolicited frame %s", self._name, frame.hex(" "))
            elif self._accept is not None and not self._accepts(frame):
                self._unmatched += 1
                self.unsolicited += 1
                _LOGGER.debug(
                    "%s: frame did not answer the request: %s", self._name, frame.hex(" ")
                )
            else:
                pending.set_result(frame)

    def _accepts(self, frame: bytes) -> bool:
        assert self._accept is not None
        try:
            return self._accept(frame)
        except Exception:  # a check that cannot read the frame does not accept it
            _LOGGER.debug("%s: the reply check failed on %s", self._name, frame.hex(" "))
            return False

    def _on_disconnect(self, client: BleakClient) -> None:
        if self._client is None or client is not self._client:
            return  # our own disconnect, a failed attempt, or a late callback from an old link
        self._client = None
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(ConnectionLost(f"{self._name}: disconnected"))
        if self._disconnected_callback is not None:
            self._disconnected_callback()

    # -- classifying failures ----------------------------------------------------------------

    @staticmethod
    def _pairing_unsupported(err: BaseException) -> bool:
        if isinstance(err, NotImplementedError) or _PAIRING_UNAVAILABLE in str(err):
            return True
        match = _PAIRING_FAILED.search(str(err))
        return match is not None and int(match.group(1)) in PAIRING_UNSUPPORTED_CODES

    def _link_error(self, err: BaseException) -> LinkError:
        """Translate an error on an established link: never evidence about pairing."""
        return LinkError(f"{self._name}: {type(err).__name__}: {err}")

    def _translate(self, err: BaseException) -> LinkError:
        """Translate a failed connection attempt, telling refusals from other failures."""
        text = str(err)
        if match := _PAIRING_FAILED.search(text):
            code = int(match.group(1))
            if code in PAIRING_REFUSED_CODES:
                return PairingRequired(f"{self._name}: {text}", code)
            if code in PAIRING_UNSUPPORTED_CODES:
                return PairingUnsupported(f"{self._name}: {text}", code)
            return LinkError(f"{self._name}: {text}", code)
        if BLUEZ_AUTHENTICATION_FAILED in text:
            return PairingRequired(f"{self._name}: {text}")
        if self._pairing_unsupported(err):
            return PairingUnsupported(f"{self._name}: {text}")
        return LinkError(f"{self._name}: {type(err).__name__}: {text}")
