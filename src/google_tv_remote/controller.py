"""A thin adapter over androidtvremote2.

It keeps an explicit state machine and translates library exceptions into the
error codes of the IPC contract. All protocol knowledge ends in this module -
the CLI and the panel see only states and events.

Behaviours established during the spike against a real device:
- after connecting there is a ~2 s warm-up window in which the device sends no
  events even though commands are delivered -> a separate READY state,
- `is_on == False` is the normal state of a powered-off device, not an error,
- POWER is a toggle, so it must work with is_on == False too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from enum import Enum
from typing import Any, Callable

from .discovery import Device
from .messages import KEY_ALLOWLIST, ProtocolError, device_event, state_event, status_event
from .storage import Storage

_log = logging.getLogger(__name__)

# Warm-up window measured on a Chromecast with Google TV 4K (~2 s), with slack.
DEFAULT_WARMUP = 2.5
# Bounded backoff - the panel must not retry forever.
DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)


class State(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    PAIRING = "pairing"
    CONNECTED = "connected"
    READY = "ready"
    ERROR = "error"


_STATE_MESSAGES = {
    State.DISCONNECTED: "Disconnected",
    State.CONNECTING: "Connecting…",
    State.PAIRING: "Enter the code from your TV",
    State.CONNECTED: "Connected",
    State.READY: "Ready",
    State.ERROR: "Error",
}


@contextlib.contextmanager
def _private_umask():
    """Force mode 0600 at the moment the key file is created.

    The library writes key.pem using the process umask and harden_credentials()
    tightens it only afterwards - during that window the key is readable by
    other processes of this user.
    """
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _default_remote_factory(host: str, certfile: str, keyfile: str):
    from androidtvremote2 import AndroidTVRemote

    return AndroidTVRemote("omagoogletv", certfile, keyfile, host)


class RemoteController:
    def __init__(
        self,
        storage: Storage,
        *,
        remote_factory: Callable[[str, str, str], Any] | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        warmup: float = DEFAULT_WARMUP,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
        errors: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self._storage = storage
        self._remote_factory = remote_factory or _default_remote_factory
        self._emit_cb = emit or (lambda _message: None)
        self._warmup = warmup
        self._backoff = backoff

        # Importing androidtvremote2 costs ~200 ms and pulls in protobuf and
        # cryptography. The exception types are resolved on first use, so a
        # user with no paired device does not pay that on every login.
        self._error_types = errors

        self._state = State.DISCONNECTED
        self._remote: Any | None = None
        self._device: Device | None = None
        self._lock = asyncio.Lock()
        self._warmup_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._last_status: dict[str, Any] | None = None

    @property
    def _errors(self) -> tuple[type[BaseException], ...]:
        if self._error_types is None:
            from androidtvremote2 import CannotConnect, ConnectionClosed, InvalidAuth

            self._error_types = (CannotConnect, ConnectionClosed, InvalidAuth)
        return self._error_types

    @property
    def _cannot_connect(self) -> type[BaseException]:
        return self._errors[0]

    @property
    def _connection_closed(self) -> type[BaseException]:
        return self._errors[1]

    @property
    def _invalid_auth(self) -> type[BaseException]:
        return self._errors[2]

    # --- state ---

    @property
    def state(self) -> State:
        return self._state

    @property
    def device(self) -> Device | None:
        return self._device

    def _emit_status(self) -> None:
        """Send the status only when it actually changed.

        Powering the device on fires three library callbacks at once, and each
        one would build an identical JSON line.
        """
        status = self.status()
        if status == self._last_status:
            return
        self._last_status = status
        self._emit_cb(status)

    def _announce_device(self, device: Device | None) -> None:
        self._emit_cb(device_event(
            host=device.host if device else "",
            name=device.name if device else "",
        ))

    def _set_state(self, state: State, message: str | None = None) -> None:
        self._state = state
        self._emit_cb(state_event(state.value, message or _STATE_MESSAGES[state]))

    def _is_live(self) -> bool:
        return self._remote is not None and self._state in (State.CONNECTED, State.READY)

    def status(self) -> dict[str, Any]:
        if not self._is_live():
            return status_event(connected=False, powered=False, muted=False, volume=0)
        info = self._remote.volume_info or {}
        return status_event(
            connected=True,
            powered=bool(self._remote.is_on),
            muted=bool(info.get("muted", False)),
            volume=int(info.get("level", 0)),
            volume_max=int(info.get("max", 0)),
            app=str(self._remote.current_app or ""),
        )

    # --- connection ---

    async def connect(self, device: Device) -> None:
        async with self._lock:
            if self._is_live() and self._device == device:
                return  # repeated clicks must not multiply connections
            await self._teardown()
            self._set_state(State.CONNECTING)
            remote = self._new_remote(device)
            mac = await self._verify_identity(remote, device, strict=True)
            await self._open(remote)
            self._adopt(remote, device, mac)

    def _new_remote(self, device: Device) -> Any:
        self._storage.ensure_dirs()
        return self._remote_factory(
            device.host, str(self._storage.certfile), str(self._storage.keyfile)
        )

    async def _verify_identity(self, remote: Any, device: Device, *, strict: bool) -> str:
        """Return the device MAC and check it against the remembered one.

        The library deliberately disables server certificate verification
        (check_hostname = False, verify_mode = CERT_NONE) - trust is bound by
        the PIN, not by a certificate authority. Without this check a changed
        DHCP lease or an impersonation on the LAN would be enough for the
        backend to send commands to a stranger and trust its status reports.
        `strict=False` while pairing, because a deliberate pairing is the only
        legitimate way to swap the device.

        The read goes to the pairing port (6467) rather than the control port
        (6466), so its failure does not prove the device is absent - and a
        sleeping TV is a normal state here, not an error. Identity checking
        must therefore never become a new reason for a failed connection: it
        refuses only on a genuine MAC mismatch, while reachability stays the
        job of _open().
        """
        try:
            _name, mac = await remote.async_get_name_and_mac()
        except Exception:
            _log.debug("Could not read the device MAC", exc_info=True)
            return ""

        mac = str(mac or "")
        if not strict:
            return mac

        expected = self._remembered_mac(device)
        if expected and mac and mac.lower() != expected.lower():
            self._set_state(State.ERROR, "A different device answers at this address")
            raise ProtocolError(
                "wrong_device",
                "A different device answers at this address — pair it again",
            )
        return mac

    async def _open(self, remote: Any) -> None:
        """Open the connection, translating library errors into contract codes."""
        try:
            await remote.async_connect()
        except self._invalid_auth:
            self._set_state(State.ERROR, "Certificate rejected — pair the device again")
            raise ProtocolError("needs_pairing", "Pairing required again") from None
        except (self._cannot_connect, self._connection_closed, OSError):
            self._set_state(State.ERROR, "The Chromecast is unreachable")
            raise ProtocolError("device_unreachable", "The Chromecast is unreachable") from None

    def _remembered_mac(self, device: Device) -> str:
        """The MAC stored earlier for this host, or an empty string."""
        state = self._storage.load_state() or {}
        return state.get("mac", "") if state.get("host") == device.host else ""

    def _adopt(self, remote: Any, device: Device, mac: str = "") -> None:
        self._remote = remote
        self._device = device
        self._attach_callbacks(remote)
        self._storage.harden_credentials()
        # An unread MAC must not erase the remembered one - the identity check
        # would then switch itself off silently and never come back.
        self._storage.save_state(
            host=device.host, name=device.name, mac=mac or self._remembered_mac(device)
        )
        self._announce_device(device)
        self._set_state(State.CONNECTED)
        # The device sends no events during the warm-up window, but it already
        # accepts commands. Without publishing what we know right now, the panel
        # would keep a stale `powered` for ~2 s and grey out the whole remote.
        self._emit_status()
        self._start_warmup()

    def _attach_callbacks(self, remote: Any) -> None:
        remote.add_volume_info_updated_callback(lambda _info: self._emit_status())
        remote.add_is_on_updated_callback(lambda _on: self._emit_status())
        remote.add_current_app_updated_callback(lambda _app: self._emit_status())
        remote.add_is_available_updated_callback(self._on_availability)

    # --- warm-up ---

    def _start_warmup(self) -> None:
        self._cancel(self._warmup_task)
        self._warmup_task = asyncio.create_task(self._warmup_then_ready())

    async def _warmup_then_ready(self) -> None:
        await asyncio.sleep(self._warmup)
        if self._state is State.CONNECTED:
            self._set_state(State.READY)
            self._emit_status()

    async def wait_ready(self) -> None:
        """Wait out the warm-up window. Used by tests and while shutting down."""
        await self._await(self._warmup_task)

    # --- reconnecting ---

    def _on_availability(self, available: bool) -> None:
        if available or self._state not in (State.CONNECTED, State.READY):
            return
        self._cancel(self._reconnect_task)
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        self._set_state(State.CONNECTING, "Connection lost — retrying")
        remote = self._remote
        for delay in self._backoff:
            await asyncio.sleep(delay)
            if remote is not self._remote:
                return  # another device was selected in the meantime
            try:
                await remote.async_connect()
            except (self._cannot_connect, self._connection_closed, OSError):
                continue
            except self._invalid_auth:
                self._set_state(State.ERROR, "Certificate rejected — pair the device again")
                return
            self._set_state(State.CONNECTED)
            self._emit_status()
            self._start_warmup()
            return

        # Backoff exhausted. Without closing the client a live socket would
        # linger for the rest of the process, and the state is unusable anyway.
        async with self._lock:
            # The guard covers the state change as well: otherwise a stale,
            # dead reconnect would flip a panel freshly connected to another
            # device over to an error.
            if remote is not self._remote:
                return
            try:
                remote.disconnect()
            except Exception:
                _log.debug("Error while disconnecting", exc_info=True)
            self._remote = None
            self._last_status = None
            self._set_state(State.ERROR, "Could not restore the connection")

    async def wait_reconnect(self) -> None:
        await self._await(self._reconnect_task)

    # --- pairing ---

    async def pair_start(self, device: Device) -> None:
        async with self._lock:
            await self._teardown()
            remote = self._new_remote(device)
            try:
                with _private_umask():
                    await remote.async_generate_cert_if_missing()
                self._storage.harden_credentials()
                await remote.async_start_pairing()
            except self._invalid_auth:
                self._set_state(State.ERROR, "The device refused pairing")
                raise ProtocolError("pairing_failed", "The device refused pairing") from None
            except (self._cannot_connect, self._connection_closed, OSError):
                self._set_state(State.ERROR, "The Chromecast is unreachable")
                raise ProtocolError(
                    "device_unreachable", "The Chromecast is unreachable"
                ) from None
            self._remote = remote
            self._device = device
            self._announce_device(device)
            self._set_state(State.PAIRING)

    async def pair_finish(self, pin: str) -> None:
        async with self._lock:
            if self._state is not State.PAIRING or self._remote is None:
                raise ProtocolError("not_pairing", "Pairing has not been started")
            remote, device = self._remote, self._device
            try:
                # The PIN never reaches logs or events - only the library.
                await remote.async_finish_pairing(pin)
            except self._invalid_auth:
                self._set_state(State.ERROR, "The code from the TV does not match")
                raise ProtocolError(
                    "pairing_failed", "The code from the TV does not match"
                ) from None
            except (self._cannot_connect, self._connection_closed, OSError):
                self._set_state(State.ERROR, "The Chromecast is unreachable")
                raise ProtocolError(
                    "device_unreachable", "The Chromecast is unreachable"
                ) from None

            self._storage.harden_credentials()
            self._set_state(State.CONNECTING)
            mac = await self._verify_identity(remote, device, strict=False)
            await self._open(remote)
            self._adopt(remote, device, mac)

    # --- keys ---

    def send_key(self, key: str) -> None:
        """Send a command. Success means 'sent', not 'the device reacted'."""
        if key not in KEY_ALLOWLIST:
            raise ProtocolError("invalid_key", "Unsupported key")
        if not self._is_live():
            raise ProtocolError("not_connected", "No connection to the device")
        try:
            self._remote.send_key_command(key)
        except (self._cannot_connect, self._connection_closed, OSError):
            raise ProtocolError("device_unreachable", "The Chromecast is unreachable") from None

    # --- cleanup ---

    async def _teardown(self) -> None:
        # Cancellation only lands at a suspension point, so without awaiting
        # the tasks disconnect() would run on an object still connecting, and
        # asyncio.run would close the loop with tasks left pending.
        warmup, reconnect = self._warmup_task, self._reconnect_task
        self._warmup_task = self._reconnect_task = None
        self._cancel(warmup)
        self._cancel(reconnect)
        await self._await(warmup)
        await self._await(reconnect)
        self._last_status = None
        if self._remote is not None:
            try:
                self._remote.disconnect()
            except Exception:  # disconnecting must never take the process down
                _log.debug("Error while disconnecting", exc_info=True)
            self._remote = None
        self._device = None

    async def shutdown(self) -> None:
        async with self._lock:
            await self._teardown()
            self._announce_device(None)
            self._set_state(State.DISCONNECTED)

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    async def _await(task: asyncio.Task | None) -> None:
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
