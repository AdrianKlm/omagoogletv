"""Chromecast discovery over mDNS, plus support for a manually typed IP.

We never scan the subnet - we only ask for services announced under
_androidtvremote2._tcp.local. The zeroconf layer hides behind the
`ServiceSource` protocol, so tests inject a fake and never touch the network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .messages import ProtocolError, require_local_host

SERVICE_TYPE = "_androidtvremote2._tcp.local."

DEFAULT_TIMEOUT = 5.0
# Headroom on top of the budget handed to the source: a hard backstop in case
# the source ignores its own deadline. It also has to cover cleanup in the
# finally block, otherwise cancellation leaves sockets open.
_GRACE = 2.0
# How long resolution requests already in flight may run past the budget.
_RESOLVE_GRACE = 0.5
# Minimum listening time before the list counts as complete. Without it the
# first device would cut the search short for the rest; with it a typical
# discovery takes ~1.5 s instead of the full 5 s, and the CLI loop is not
# blocked longer than it has to be.
_MIN_BROWSE = 1.5
# Polling step of the listening loop.
_POLL = 0.2

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Device:
    name: str
    host: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "host": self.host}


@dataclass
class RawService:
    """A raw mDNS record, before validation."""

    name: str
    addresses: list[str] = field(default_factory=list)
    port: int = 0


class ServiceSource(Protocol):
    async def browse(self, service_type: str, timeout: float) -> list[RawService]: ...


def _clean_name(name: str) -> str:
    # zeroconf returns "Living room._androidtvremote2._tcp.local."
    suffix = "." + SERVICE_TYPE
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name.strip().rstrip(".")


def _first_local_ipv4(addresses: list[str]) -> str | None:
    """The device announces both IPv4 and IPv6; we speak the protocol over IPv4."""
    for raw in addresses:
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr.version != 4:
            continue
        try:
            return require_local_host(str(addr))
        except ProtocolError:
            continue
    return None


def _normalize(services: list[RawService]) -> list[Device]:
    found: dict[str, Device] = {}
    for service in services:
        host = _first_local_ipv4(service.addresses)
        if host is None or host in found:
            continue
        found[host] = Device(name=_clean_name(service.name), host=host)
    return sorted(found.values(), key=lambda d: (d.name, ipaddress.IPv4Address(d.host)))


async def discover(
    *,
    source: ServiceSource | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Device]:
    """Return the devices found. Never raises and never hangs forever."""
    if source is None:
        source = ZeroconfSource()
    try:
        raw = await asyncio.wait_for(source.browse(SERVICE_TYPE, timeout), timeout + _GRACE)
    except (TimeoutError, asyncio.TimeoutError):
        _log.warning("Discovery exceeded its %.1f s budget", timeout)
        return []
    except Exception:
        # No interface, avahi disabled, a zeroconf failure - the panel should
        # get an empty list and the offer to type an IP, not an exception.
        _log.warning("Discovery failed", exc_info=True)
        return []
    return _normalize(raw)


def device_from_host(host: str, name: str = "") -> Device:
    """Fallback path when mDNS does not work. Validated like any IPC input."""
    return Device(name=name, host=require_local_host(host))


class ZeroconfSource:
    """The real zeroconf-backed source. Not used in unit tests.

    It watches its own time budget and returns whatever it managed to resolve.
    Previously a single slow-answering service could drag the whole call past
    the deadline in discover(), and the resulting cancellation threw away the
    devices already found, leaving the panel showing "no devices".
    """

    async def browse(self, service_type: str, timeout: float) -> list[RawService]:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        found: dict[str, RawService] = {}
        pending: set[asyncio.Task] = set()

        aiozc = None
        browser = None
        try:
            # Constructed inside the try: if setting up the browser fails, the
            # zeroconf threads and sockets still get cleaned up.
            aiozc = AsyncZeroconf()

            async def resolve(name: str) -> None:
                remaining = max(0.5, deadline - loop.time())
                info = AsyncServiceInfo(service_type, name)
                if await info.async_request(aiozc.zeroconf, int(remaining * 1000)):
                    found[name] = RawService(
                        name=name,
                        addresses=list(info.parsed_addresses()),
                        port=info.port or 0,
                    )

            def on_change(zeroconf, service_type, name, state_change) -> None:
                if state_change is not ServiceStateChange.Added:
                    return
                task = loop.create_task(resolve(name))
                pending.add(task)
                # Without this, tasks started just before the end would be
                # neither awaited nor cancelled.
                task.add_done_callback(pending.discard)

            browser = AsyncServiceBrowser(aiozc.zeroconf, service_type, handlers=[on_change])

            earliest_finish = loop.time() + _MIN_BROWSE
            while loop.time() < deadline:
                await asyncio.sleep(min(_POLL, max(0.0, deadline - loop.time())))
                if found and not pending and loop.time() >= earliest_finish:
                    break

            if pending:
                await asyncio.wait(set(pending), timeout=_RESOLVE_GRACE)
        except Exception:
            _log.warning("Browsing mDNS failed", exc_info=True)
        finally:
            for task in list(pending):
                task.cancel()
            if browser is not None:
                await browser.async_cancel()
            if aiozc is not None:
                await aiozc.async_close()

        return list(found.values())
