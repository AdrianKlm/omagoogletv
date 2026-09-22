"""The JSON Lines loop: stdin -> actions, stdout -> JSON only."""

import json

import pytest

from google_tv_remote.cli import Backend
from google_tv_remote.controller import RemoteController, State
from google_tv_remote.discovery import Device
from google_tv_remote.storage import Storage

from .fakes import CannotConnect, ConnectionClosed, FakeRemote, InvalidAuth

DEVICES = [Device(name="Chromecast", host="192.168.1.42")]


def _write_credentials(storage):
    """Pretend a device is paired - autoconnect requires a certificate."""
    storage.ensure_dirs()
    storage.certfile.write_text("cert")
    storage.keyfile.write_text("key")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    storage = Storage()

    output = []
    box = {"devices": list(DEVICES)}

    def factory(host, certfile, keyfile):
        box["remote"] = FakeRemote(host)
        return box["remote"]

    async def fake_discover(**kwargs):
        return box["devices"]

    ctrl = RemoteController(
        storage,
        remote_factory=factory,
        emit=lambda msg: output.append(msg),
        warmup=0.01,
        backoff=(0.0,),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    b = Backend(storage, ctrl, discover_fn=fake_discover, write=output.append)
    return b, output, box, storage


def replies(output, request_id=None):
    out = [m for m in output if "ok" in m]
    return [m for m in out if request_id is None or m["id"] == request_id]


async def send(backend, **fields):
    await backend.handle_line(json.dumps(fields))


# --- discover ---

async def test_discover_returns_the_device_list(backend):
    b, output, _, _ = backend
    await send(b, id=1, action="discover")
    assert replies(output, 1)[0] == {
        "id": 1, "ok": True, "devices": [{"name": "Chromecast", "host": "192.168.1.42"}]}


async def test_discover_without_devices_returns_an_empty_list(backend):
    b, output, box, _ = backend
    box["devices"] = []
    await send(b, id=1, action="discover")
    assert replies(output, 1)[0]["devices"] == []


# --- selecting a device and connecting ---

async def test_select_device_connects_and_acknowledges(backend):
    b, output, _, storage = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    assert replies(output, 2)[0]["ok"] is True
    assert storage.load_state()["host"] == "192.168.1.42"


async def test_select_device_fills_the_name_from_discovery(backend):
    b, _, _, storage = backend
    await send(b, id=1, action="discover")
    await send(b, id=2, action="select_device", host="192.168.1.42")
    assert storage.load_state()["name"] == "Chromecast"


async def test_select_device_without_a_certificate_asks_for_pairing(backend):
    b, output, box, _ = backend

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = InvalidAuth("no certificate")
        box["remote"] = r
        return r

    b.controller._remote_factory = factory
    await send(b, id=2, action="select_device", host="192.168.1.42")
    reply = replies(output, 2)[0]
    assert reply["ok"] is False and reply["code"] == "needs_pairing"


async def test_pairing_can_start_right_after_a_failed_connection(backend):
    b, output, box, _ = backend

    def broken(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = InvalidAuth("no certificate")
        box["remote"] = r
        return r

    b.controller._remote_factory = broken
    await send(b, id=2, action="select_device", host="192.168.1.42")

    def working(host, certfile, keyfile):
        box["remote"] = FakeRemote(host)
        return box["remote"]

    b.controller._remote_factory = working
    await send(b, id=3, action="pair_start")
    assert replies(output, 3)[0]["ok"] is True


async def test_an_unreachable_device_yields_device_unreachable(backend):
    b, output, box, _ = backend

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = CannotConnect("no such host")
        box["remote"] = r
        return r

    b.controller._remote_factory = factory
    await send(b, id=2, action="select_device", host="192.168.1.42")
    assert replies(output, 2)[0]["code"] == "device_unreachable"


# --- pairing ---

async def test_pair_start_without_a_selected_device(backend):
    b, output, _, _ = backend
    await send(b, id=3, action="pair_start")
    assert replies(output, 3)[0]["code"] == "no_device"


async def test_the_full_pairing_flow(backend):
    b, output, box, _ = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    await send(b, id=3, action="pair_start")
    await send(b, id=4, action="pair_finish", pin="9e76ab")
    assert replies(output, 4)[0]["ok"] is True
    assert box["remote"].received_pin == "9E76AB"


async def test_a_wrong_pin_does_not_end_the_process(backend):
    b, output, box, _ = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    await send(b, id=3, action="pair_start")
    box["remote"].pairing_error = InvalidAuth("wrong code")
    await send(b, id=4, action="pair_finish", pin="000000")
    assert replies(output, 4)[0]["code"] == "pairing_failed"
    await send(b, id=5, action="status")
    assert replies(output, 5)[0]["ok"] is True


async def test_the_pin_never_appears_on_the_output(backend):
    b, output, _, _ = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    await send(b, id=3, action="pair_start")
    await send(b, id=4, action="pair_finish", pin="SECRT1")
    assert "SECRT1" not in json.dumps(output, ensure_ascii=False)


# --- keys ---

async def test_a_key_once_connected(backend):
    b, output, box, _ = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    await send(b, id=5, action="key", key="DPAD_UP")
    assert replies(output, 5)[0] == {"id": 5, "ok": True}
    assert box["remote"].sent_keys == ["DPAD_UP"]


async def test_a_key_without_a_connection(backend):
    b, output, _, _ = backend
    await send(b, id=5, action="key", key="DPAD_UP")
    assert replies(output, 5)[0]["code"] == "not_connected"


async def test_a_key_outside_the_allowlist(backend):
    b, output, _, _ = backend
    await send(b, id=5, action="key", key="KEYCODE_DELETE")
    assert replies(output, 5)[0]["code"] == "invalid_key"


# --- status ---

async def test_status_returns_the_state(backend):
    b, output, _, _ = backend
    await send(b, id=2, action="select_device", host="192.168.1.42")
    await send(b, id=7, action="status")
    reply = replies(output, 7)[0]
    assert reply["ok"] is True
    assert reply["status"]["connected"] is True


# --- loop robustness ---

async def test_invalid_json_does_not_end_the_loop(backend):
    b, output, _, _ = backend
    await b.handle_line("{this is not json")
    assert replies(output)[-1]["code"] == "invalid_request"
    await send(b, id=1, action="discover")
    assert replies(output, 1)[0]["ok"] is True


async def test_an_empty_line_is_ignored(backend):
    b, output, _, _ = backend
    await b.handle_line("   \n")
    assert output == []


async def test_an_unknown_action(backend):
    b, output, _, _ = backend
    await send(b, id=9, action="do_anything")
    assert replies(output, 9)[0]["code"] == "unknown_action"


async def test_an_unexpected_exception_gives_internal_error_not_a_crash(backend):
    b, output, _, _ = backend

    async def explode(**kwargs):
        raise RuntimeError("something went wrong")

    b._discover = explode
    await send(b, id=1, action="discover")
    assert replies(output, 1)[0]["code"] == "internal_error"


async def test_the_internal_error_message_leaks_no_details(backend):
    b, output, _, _ = backend

    async def explode(**kwargs):
        raise RuntimeError("/home/someone/.local/share/omagoogletv/key.pem")

    b._discover = explode
    await send(b, id=1, action="discover")
    assert "key.pem" not in json.dumps(output)


# --- automatic connection on start-up ---

async def test_autoconnect_uses_the_remembered_device(backend):
    b, output, box, storage = backend
    storage.save_state(host="192.168.1.42", name="Chromecast")
    _write_credentials(storage)
    await b.autoconnect()
    assert b.controller.state in (State.CONNECTED, State.READY)
    assert box["remote"].connect_count == 1


async def test_autoconnect_without_a_remembered_device_does_nothing(backend):
    b, _, _, _ = backend
    await b.autoconnect()
    assert b.controller.state is State.DISCONNECTED


async def test_autoconnect_survives_an_unreachable_device(backend):
    b, output, box, storage = backend
    storage.save_state(host="192.168.1.42", name="Chromecast")
    _write_credentials(storage)

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = CannotConnect("powered off")
        box["remote"] = r
        return r

    b.controller._remote_factory = factory
    await b.autoconnect()
    assert b.controller.state is State.ERROR


# --- first run without credentials ---

async def test_autoconnect_without_a_certificate_does_not_try_to_connect(backend):
    # A new user must not see a "certificate rejected" error about something
    # they never had.
    b, output, box, storage = backend
    storage.save_state(host="192.168.1.42", name="Chromecast")
    assert not storage.certfile.exists()

    await b.autoconnect()

    assert "remote" not in box, "a connection was attempted without a certificate"
    assert b.controller.state is State.DISCONNECTED
    messages = [m for m in output if m.get("event") == "state"]
    assert messages and "pair" in messages[-1]["message"].lower()


async def test_autoconnect_with_a_certificate_does_try_to_connect(backend):
    b, _, box, storage = backend
    storage.save_state(host="192.168.1.42", name="Chromecast")
    _write_credentials(storage)

    await b.autoconnect()

    assert box["remote"].connect_count == 1


# --- initial state and start-up robustness ---

async def test_start_without_a_remembered_device_announces_a_state(backend):
    # After a backend restart the panel must get a fresh state, otherwise it
    # keeps the error message left by the previous, dead process.
    b, output, _, _ = backend
    await b.autoconnect()
    state_events = [m for m in output if m.get("event") == "state"]
    assert state_events and state_events[-1]["state"] == "disconnected"


async def test_autoconnect_survives_an_unexpected_exception(backend):
    # A damaged cert.pem makes the cryptography library raise ValueError -
    # that must not kill the process before the stdin loop starts.
    b, output, _, storage = backend
    storage.save_state(host="192.168.1.42", name="Chromecast")
    _write_credentials(storage)

    def exploding_factory(host, certfile, keyfile):
        raise ValueError("damaged certificate")

    b.controller._remote_factory = exploding_factory
    await b.autoconnect()

    state_events = [m for m in output if m.get("event") == "state"]
    assert state_events[-1]["state"] == "error"
    # the process stays alive and keeps serving requests
    await send(b, id=1, action="status")
    assert replies(output, 1)[0]["ok"] is True
