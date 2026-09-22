"""JSON Lines contract between the QML panel and the backend.

Every message is a single JSON object terminated by '\\n'. Validation is
deliberately strict: unknown actions, unknown fields and keys outside the
allowlist are rejected, so the panel cannot push the backend beyond its scope.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any

# Keys verified against a real device during the protocol spike.
# Names carry no KEYCODE_ prefix - that is the form send_key_command() takes.
# TV_POWER/STB_POWER/AVR_POWER are left out on purpose: no-ops on a Chromecast.
KEY_ALLOWLIST = frozenset({
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "BACK", "HOME", "MEDIA_PLAY_PAUSE", "VOLUME_DOWN", "VOLUME_UP",
    "MUTE", "POWER",
})

# Action -> set of extra fields it accepts (beyond "id" and "action").
_ACTIONS: dict[str, frozenset[str]] = {
    "discover": frozenset(),
    "select_device": frozenset({"host"}),
    "pair_start": frozenset(),
    "pair_finish": frozenset({"pin"}),
    "key": frozenset({"key"}),
    "status": frozenset(),
}

_PIN_PATTERN = re.compile(r"\A[A-Za-z0-9]{6}\Z")


class ProtocolError(Exception):
    """A message that breaks the contract. Carries an error code for the reply.

    `request_id` is filled in as soon as 'id' can be read from the message.
    Without it the panel could not tie the error back to the request it sent.
    """

    def __init__(self, code: str, message: str, request_id: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    id: int
    action: str
    host: str | None = None
    pin: str | None = None
    key: str | None = None


def _require_id(raw: Any) -> int:
    # bool subclasses int, but {"id": true} is not a valid identifier
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ProtocolError("invalid_request", "Field 'id' must be a positive integer")
    return raw


def require_local_host(raw: Any) -> str:
    """Validate a device address. Shared by IPC, stored state and discovery."""
    if not isinstance(raw, str):
        raise ProtocolError("invalid_host", "Field 'host' must be a string")
    try:
        addr = ipaddress.IPv4Address(raw)
    except ipaddress.AddressValueError:
        raise ProtocolError("invalid_host", "Not a valid IPv4 address") from None
    # Python counts 0.0.0.0 and 255.255.255.255 as private, and on Linux the
    # former connects to localhost - which contradicts rejecting loopback.
    if addr.is_unspecified or addr.is_reserved or addr.is_loopback or addr.is_multicast:
        raise ProtocolError("invalid_host", "Address must belong to the local network")
    if not (addr.is_private or addr.is_link_local):
        raise ProtocolError("invalid_host", "Address must belong to the local network")
    return str(addr)


# Short alias kept for brevity inside this module.
_require_host = require_local_host


def _require_pin(raw: Any) -> str:
    if not isinstance(raw, str) or not _PIN_PATTERN.match(raw):
        raise ProtocolError("invalid_pin", "The PIN is 6 letters or digits")
    return raw.upper()


def _require_key(raw: Any) -> str:
    if not isinstance(raw, str) or raw not in KEY_ALLOWLIST:
        raise ProtocolError("invalid_key", "Unsupported key")
    return raw


_FIELD_VALIDATORS = {"host": _require_host, "pin": _require_pin, "key": _require_key}


def parse_request(line: str) -> Request:
    """Turn one line from stdin into a validated request."""
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        raise ProtocolError("invalid_request", "Message is not valid JSON") from None

    if not isinstance(payload, dict):
        raise ProtocolError("invalid_request", "Message must be a JSON object")

    request_id = _require_id(payload.get("id"))

    # From here on 'id' is known, so every error has to carry it along.
    try:
        action = payload.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ProtocolError("unknown_action", "Unknown action")

        allowed = _ACTIONS[action]
        extra = set(payload) - {"id", "action"} - allowed
        if extra:
            raise ProtocolError(
                "invalid_request", f"Unexpected fields: {', '.join(sorted(extra))}"
            )

        values: dict[str, str] = {}
        for field in allowed:
            if field not in payload:
                raise ProtocolError("invalid_request", f"Missing required field '{field}'")
            values[field] = _FIELD_VALIDATORS[field](payload[field])
    except ProtocolError as err:
        err.request_id = request_id
        raise

    return Request(id=request_id, action=action, **values)


def ok_response(request_id: int, **fields: Any) -> dict[str, Any]:
    """Acknowledge a request.

    For keys this means only 'the command was sent'. The protocol sends back no
    confirmation for the D-pad, BACK or play/pause (established during the
    spike), so the panel must not infer that the device actually reacted.
    """
    return {"id": request_id, "ok": True, **fields}


def error_response(request_id: int, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "code": code, "message": message}


def state_event(state: str, message: str) -> dict[str, Any]:
    return {"event": "state", "state": state, "message": message}


def device_event(*, host: str, name: str) -> dict[str, Any]:
    """Announce which device the backend is working with.

    After an automatic connection the panel sends no 'select_device', so this
    event is its only source for the host and the name.
    """
    return {"event": "device", "host": host, "name": name}


def status_event(
    *,
    connected: bool,
    powered: bool,
    muted: bool,
    volume: int,
    volume_max: int = 0,
    app: str = "",
) -> dict[str, Any]:
    """Device status.

    `volume_max` comes from the device (a Chromecast reports 25) so the panel
    need not guess the scale. `app` is the raw package identifier - turning it
    into a friendly name is the UI's job; the backend knows no panel layout.
    """
    return {
        "event": "status",
        "connected": connected,
        "powered": powered,
        "muted": muted,
        "volume": volume,
        "volume_max": volume_max,
        "app": app,
    }


def encode(message: dict[str, Any]) -> str:
    """Serialize a message into a single JSON Lines record."""
    # JSON escapes '\n' inside strings, so a message can never break into two
    # lines; ensure_ascii=False keeps any non-ASCII device name readable.
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
