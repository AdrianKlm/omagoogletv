"""The JSON Lines loop tying the QML panel to the controller.

Only JSON reaches stdout - one message per line. Every log goes to stderr so
the IPC channel stays intact. Lines are handled sequentially, so repeated
clicks in the panel cannot start two pairings at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import stat
import sys
from typing import Any, Awaitable, Callable

from .controller import RemoteController
from .discovery import Device, device_from_host, discover
from .messages import (
    ProtocolError,
    Request,
    encode,
    error_response,
    ok_response,
    parse_request,
    state_event,
)
from .storage import Storage

_log = logging.getLogger("google_tv_remote")


class Backend:
    def __init__(
        self,
        storage: Storage,
        controller: RemoteController,
        *,
        discover_fn: Callable[..., Awaitable[list[Device]]] = discover,
        write: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.storage = storage
        self.controller = controller
        self._discover = discover_fn
        self._write = write or _write_stdout
        self._selected: Device | None = None
        self._known_names: dict[str, str] = {}

    # --- loop ---

    async def handle_line(self, line: str) -> None:
        if not line.strip():
            return
        try:
            request = parse_request(line)
        except ProtocolError as err:
            self._write(error_response(err.request_id, err.code, err.message))
            return

        try:
            await self._dispatch(request)
        except ProtocolError as err:
            self._write(error_response(request.id, err.code, err.message))
        except Exception:
            # Details can contain credential paths - they stay in stderr.
            _log.exception("Unhandled error during action %s", request.action)
            self._write(error_response(request.id, "internal_error", "Internal error"))

    async def _dispatch(self, request: Request) -> None:
        handler = getattr(self, f"_action_{request.action}")
        await handler(request)

    # --- actions ---

    async def _action_discover(self, request: Request) -> None:
        devices = await self._discover()
        self._known_names = {d.host: d.name for d in devices if d.name}
        self._write(ok_response(request.id, devices=[d.as_dict() for d in devices]))

    async def _action_select_device(self, request: Request) -> None:
        device = device_from_host(request.host, self._known_names.get(request.host, ""))
        # The choice is remembered even on failure: the panel must be able to
        # move straight to pairing without discovering all over again.
        self._selected = device
        await self.controller.connect(device)
        self._write(ok_response(request.id))

    async def _action_pair_start(self, request: Request) -> None:
        if self._selected is None:
            raise ProtocolError("no_device", "Select a device first")
        await self.controller.pair_start(self._selected)
        self._write(ok_response(request.id))

    async def _action_pair_finish(self, request: Request) -> None:
        await self.controller.pair_finish(request.pin)
        self._write(ok_response(request.id))

    async def _action_key(self, request: Request) -> None:
        self.controller.send_key(request.key)
        self._write(ok_response(request.id))

    async def _action_status(self, request: Request) -> None:
        self._write(ok_response(request.id, status=self.controller.status()))

    # --- start ---

    async def autoconnect(self) -> None:
        """Connect to the remembered device. No state and failure are both normal.

        It always ends by announcing a state: after a backend restart the panel
        still holds the state of the previous, dead process, and without this
        it would sit on a red error despite a working connection.
        """
        state = self.storage.load_state()
        if state is None:
            self._write(state_event("disconnected", "Select a device"))
            return

        # Without a certificate the connection would fail with "certificate
        # rejected" - confusing for someone who has never paired a device.
        if not self.storage.certfile.exists():
            self._write(state_event("disconnected", "Pair a device to get started"))
            return

        device = Device(name=state["name"], host=state["host"])
        self._selected = device
        try:
            await self.controller.connect(device)
        except ProtocolError as err:
            _log.info("Automatic connection failed: %s", err.code)
        except Exception:
            # A damaged certificate can raise ValueError out of cryptography.
            # Such an error must not kill the process before the stdin loop runs.
            _log.exception("Automatic connection ended with an unexpected error")
            self._write(state_event("error", "Could not connect — pair the device again"))


def _write_stdout(message: dict[str, Any]) -> None:
    try:
        sys.stdout.write(encode(message))
        sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        # The panel vanished while a library callback was emitting an event.
        # The process is about to end anyway - no reason to blow up the dispatch.
        _log.debug("Output channel closed", exc_info=True)


def _stdin_is_pollable() -> bool:
    """Whether stdin can be registered with epoll.

    A regular file and /dev/null cannot. connect_read_pipe() does not report
    this as an error: for /dev/null it succeeds, and PermissionError only
    surfaces later inside the _add_reader callback - readline() would then wait
    forever. So the descriptor type is checked up front instead of catching an
    exception.
    """
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError):
        return False
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return True
    return stat.S_ISCHR(mode) and sys.stdin.isatty()


async def _stdin_lines() -> Any:
    """Read stdin line by line without blocking the event loop."""
    loop = asyncio.get_running_loop()

    if not _stdin_is_pollable():
        _log.debug("stdin is not a pipe — reading in a thread")
        async for line in _stdin_lines_threaded(loop):
            yield line
        return

    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (ValueError, OSError):
        async for line in _stdin_lines_threaded(loop):
            yield line
        return

    while True:
        line = await reader.readline()
        if not line:
            return
        yield line.decode("utf-8", errors="replace")


async def _stdin_lines_threaded(loop: asyncio.AbstractEventLoop) -> Any:
    """Fallback path for a stdin that cannot be registered with epoll."""
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        yield line


async def run() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    storage = Storage()
    storage.ensure_dirs()
    controller = RemoteController(storage, emit=_write_stdout)
    backend = Backend(storage, controller)

    await backend.autoconnect()

    # Quickshell closes the helper with a signal. Without handling it shutdown()
    # never runs, so the session is not closed cleanly and the loop dies with
    # pending tasks ("Task was destroyed but it is pending!" in the shell log).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            _log.debug("Could not install a handler for signal %s", sig)

    async def pump() -> None:
        async for line in _stdin_lines():
            await backend.handle_line(line)

    pump_task = asyncio.create_task(pump())
    stop_task = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({pump_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Draining the tasks gets its own try: when the pump ends with an
        # exception, awaiting it again re-raises, and without this that would
        # skip shutdown() - exactly what this block exists to guarantee.
        try:
            for task in (pump_task, stop_task):
                task.cancel()
            for task in (pump_task, stop_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            await controller.shutdown()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
