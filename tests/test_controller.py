"""The androidtvremote2 adapter: connecting, pairing, keys, reconnecting.

These tests never touch a real TV - they use the stand-in from tests/fakes.py.
"""

import asyncio

import pytest

from google_tv_remote.controller import RemoteController, State
from google_tv_remote.discovery import Device
from google_tv_remote.messages import ProtocolError
from google_tv_remote.storage import Storage

from .fakes import CannotConnect, ConnectionClosed, FakeRemote, InvalidAuth

DEVICE = Device(name="Chromecast", host="192.168.1.42")


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Storage()


@pytest.fixture
def env(storage):
    events = []
    box = {}

    def factory(host, certfile, keyfile):
        box["remote"] = FakeRemote(host, certfile=certfile, keyfile=keyfile)
        return box["remote"]

    ctrl = RemoteController(
        storage,
        remote_factory=factory,
        emit=events.append,
        warmup=0.01,
        backoff=(0.0, 0.0),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    return ctrl, events, box


def states(events):
    return [e["state"] for e in events if e.get("event") == "state"]


def statuses(events):
    return [e for e in events if e.get("event") == "status"]


# --- connecting ---

async def test_connection_walks_the_states_up_to_ready(env):
    ctrl, events, _ = env
    await ctrl.connect(DEVICE)
    assert ctrl.state == State.CONNECTED
    await ctrl.wait_ready()
    assert ctrl.state == State.READY
    assert states(events) == ["connecting", "connected", "ready"]


async def test_connect_does_not_block_on_the_warmup_window(storage):
    # warm-up takes ~2 s on a real device; connect() must not wait it out
    ctrl = RemoteController(
        storage,
        remote_factory=lambda h, c, k: FakeRemote(h),
        emit=lambda e: None,
        warmup=5.0,
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    await asyncio.wait_for(ctrl.connect(DEVICE), timeout=1.0)
    assert ctrl.state == State.CONNECTED
    await ctrl.shutdown()


async def test_connecting_remembers_the_device(env, storage):
    ctrl, _, _ = env
    await ctrl.connect(DEVICE)
    assert storage.load_state() == {
        "host": "192.168.1.42", "name": "Chromecast", "mac": "AA:BB:CC:DD:EE:FF"}


async def test_connecting_tightens_credential_permissions(env, storage):
    ctrl, _, _ = env
    storage.ensure_dirs()
    storage.certfile.write_text("x")
    storage.certfile.chmod(0o644)
    await ctrl.connect(DEVICE)
    assert oct(storage.certfile.stat().st_mode)[-3:] == "600"


async def test_reconnecting_to_the_same_device_is_a_no_op(env):
    # repeated clicks in the panel must not multiply connections
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    first = box["remote"]
    await ctrl.connect(DEVICE)
    assert box["remote"] is first, "a second client was created"
    assert first.connect_count == 1
    assert first.disconnect_count == 0


async def test_connecting_to_another_device_closes_the_previous_one(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    first = box["remote"]
    await ctrl.connect(Device(name="Bedroom", host="192.168.1.120"))
    assert box["remote"] is not first
    assert first.disconnect_count == 1
    assert box["remote"].connect_count == 1


async def test_concurrent_connects_run_one_at_a_time(env, storage):
    ctrl, _, _ = env
    await asyncio.gather(*(ctrl.connect(DEVICE) for _ in range(5)))
    assert ctrl.state in (State.CONNECTED, State.READY)
    assert storage.load_state()["host"] == "192.168.1.42"


async def test_unreachable_device_gives_an_error_and_the_error_state(env):
    ctrl, events, _ = env

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = CannotConnect("no such host")
        return r

    ctrl._remote_factory = factory
    with pytest.raises(ProtocolError) as err:
        await ctrl.connect(DEVICE)
    assert err.value.code == "device_unreachable"
    assert ctrl.state == State.ERROR
    assert states(events)[-1] == "error"


async def test_a_revoked_certificate_requires_pairing_again(env):
    ctrl, _, _ = env

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = InvalidAuth("certificate rejected")
        return r

    ctrl._remote_factory = factory
    with pytest.raises(ProtocolError) as err:
        await ctrl.connect(DEVICE)
    assert err.value.code == "needs_pairing"


async def test_a_failed_connection_does_not_remember_the_device(env, storage):
    ctrl, _, _ = env

    def factory(host, certfile, keyfile):
        r = FakeRemote(host)
        r.connect_error = CannotConnect("no such host")
        return r

    ctrl._remote_factory = factory
    with pytest.raises(ProtocolError):
        await ctrl.connect(DEVICE)
    assert storage.load_state() is None


# --- pairing ---

async def test_pairing_goes_through_the_pairing_state(env):
    ctrl, events, box = env
    await ctrl.pair_start(DEVICE)
    assert ctrl.state == State.PAIRING
    assert box["remote"].pairing_started
    assert "pairing" in states(events)


async def test_pairing_generates_a_certificate_when_missing(env):
    ctrl, _, box = env
    await ctrl.pair_start(DEVICE)
    assert box["remote"].cert_exists


async def test_pair_finish_passes_the_pin_and_connects(env):
    ctrl, _, box = env
    await ctrl.pair_start(DEVICE)
    await ctrl.pair_finish("9E76AB")
    assert box["remote"].received_pin == "9E76AB"
    assert ctrl.state in (State.CONNECTED, State.READY)


async def test_pair_finish_without_pair_start_is_an_error(env):
    ctrl, _, _ = env
    with pytest.raises(ProtocolError) as err:
        await ctrl.pair_finish("9E76AB")
    assert err.value.code == "not_pairing"


async def test_a_wrong_pin_gives_a_readable_error(env):
    ctrl, _, box = env
    await ctrl.pair_start(DEVICE)
    box["remote"].pairing_error = InvalidAuth("wrong code")
    with pytest.raises(ProtocolError) as err:
        await ctrl.pair_finish("000000")
    assert err.value.code == "pairing_failed"


async def test_the_pin_never_reaches_the_events(env):
    ctrl, events, box = env
    await ctrl.pair_start(DEVICE)
    box["remote"].pairing_error = InvalidAuth("wrong code")
    with pytest.raises(ProtocolError):
        await ctrl.pair_finish("SECRT1")
    assert "SECRT1" not in str(events)


async def test_a_second_pair_start_does_not_run_two_pairings(env):
    ctrl, _, box = env
    await ctrl.pair_start(DEVICE)
    first = box["remote"]
    await ctrl.pair_start(DEVICE)
    assert first.disconnect_count == 1


# --- keys ---

async def test_sends_a_key_once_connected(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()
    ctrl.send_key("DPAD_UP")
    assert box["remote"].sent_keys == ["DPAD_UP"]


async def test_a_key_works_during_the_warmup_window(env):
    # the spike confirmed commands are delivered right after connecting
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    assert ctrl.state == State.CONNECTED
    ctrl.send_key("VOLUME_UP")
    assert box["remote"].sent_keys == ["VOLUME_UP"]


async def test_a_key_without_a_connection_is_an_error(env):
    ctrl, _, _ = env
    with pytest.raises(ProtocolError) as err:
        ctrl.send_key("DPAD_UP")
    assert err.value.code == "not_connected"


async def test_a_key_outside_the_allowlist_is_rejected_here_too(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    with pytest.raises(ProtocolError) as err:
        ctrl.send_key("TV_POWER")
    assert err.value.code == "invalid_key"
    assert box["remote"].sent_keys == []


async def test_a_broken_connection_while_sending_gives_an_error(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    box["remote"].connect_error = ConnectionClosed("broken")
    with pytest.raises(ProtocolError) as err:
        ctrl.send_key("DPAD_UP")
    assert err.value.code == "device_unreachable"


async def test_a_key_works_while_the_device_is_powered_off(env):
    # POWER is a toggle - it has to work with is_on=False too
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    box["remote"].report_power(False)
    ctrl.send_key("POWER")
    assert box["remote"].sent_keys == ["POWER"]


# --- status ---

async def test_a_volume_change_emits_a_status(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    box["remote"].report_volume(9)
    last = statuses(events)[-1]
    assert last["volume"] == 9 and last["muted"] is False


async def test_muting_emits_a_status(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    box["remote"].report_volume(0, muted=True)
    assert statuses(events)[-1]["muted"] is True


async def test_powering_the_device_off_is_not_an_error(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()
    box["remote"].report_power(False)
    assert ctrl.state == State.READY
    assert states(events)[-1] != "error"
    assert statuses(events)[-1]["powered"] is False


async def test_status_carries_the_scale_from_the_device(env):
    # a Chromecast reports max 25; the panel must not hard-code that number
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    assert ctrl.status()["volume_max"] == 25


async def test_status_carries_the_current_app(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    box["remote"].current_app = "com.google.android.youtube.tv"
    assert ctrl.status()["app"] == "com.google.android.youtube.tv"


async def test_an_app_change_emits_a_status(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    box["remote"].report_app("com.netflix.ninja")
    assert statuses(events)[-1]["app"] == "com.netflix.ninja"


async def test_status_without_a_connection_does_not_blow_up(env):
    ctrl, _, _ = env
    assert ctrl.status()["connected"] is False


async def test_status_after_connecting_has_the_full_shape(env):
    ctrl, _, _ = env
    await ctrl.connect(DEVICE)
    s = ctrl.status()
    assert set(s) == {"event", "connected", "powered", "muted", "volume",
                      "volume_max", "app"}
    assert s["connected"] is True


# --- reconnecting ---

async def test_losing_availability_starts_a_reconnect(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()
    box["remote"].report_availability(False)
    await ctrl.wait_reconnect()
    assert ctrl.state in (State.CONNECTED, State.READY)
    assert box["remote"].connect_count >= 2


async def test_reconnecting_has_a_bounded_number_of_attempts(storage):
    events = []
    remote = FakeRemote("192.168.1.42")

    ctrl = RemoteController(
        storage,
        remote_factory=lambda h, c, k: remote,
        emit=events.append,
        warmup=0.01,
        backoff=(0.0, 0.0, 0.0),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()

    remote.connect_error = CannotConnect("network gone")
    attempts_before = remote.connect_count
    remote.report_availability(False)
    await ctrl.wait_reconnect()

    assert ctrl.state == State.ERROR
    assert remote.connect_count == attempts_before  # no attempt succeeded
    assert states(events)[-1] == "error"
    await ctrl.shutdown()


async def test_a_recovered_connection_returns_to_ready(storage):
    events = []
    remote = FakeRemote("192.168.1.42")
    ctrl = RemoteController(
        storage,
        remote_factory=lambda h, c, k: remote,
        emit=events.append,
        warmup=0.01,
        backoff=(0.0, 0.0, 0.0),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()

    remote.connect_error = CannotConnect("temporary")
    remote.report_availability(False)
    await asyncio.sleep(0)
    remote.connect_error = None
    await ctrl.wait_reconnect()
    await ctrl.wait_ready()

    assert ctrl.state == State.READY
    await ctrl.shutdown()


async def test_shutdown_disconnects_and_cleans_up_tasks(env):
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()
    assert box["remote"].disconnect_count == 1
    assert ctrl.state == State.DISCONNECTED


async def test_shutdown_without_a_connection_does_not_blow_up(env):
    ctrl, _, _ = env
    await ctrl.shutdown()


# --- announcing the selected device ---

def device_events(events):
    return [e for e in events if e.get("event") == "device"]


async def test_connecting_announces_the_device(env):
    # after an automatic connection the panel has no other source of truth
    ctrl, events, _ = env
    await ctrl.connect(DEVICE)
    assert device_events(events)[-1] == {
        "event": "device", "host": "192.168.1.42", "name": "Chromecast"}


async def test_shutdown_announces_no_device(env):
    ctrl, events, _ = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()
    assert device_events(events)[-1] == {"event": "device", "host": "", "name": ""}


async def test_pairing_announces_the_selected_device(env):
    ctrl, events, _ = env
    await ctrl.pair_start(DEVICE)
    assert device_events(events)[-1]["host"] == "192.168.1.42"


# --- cleanup after an exhausted reconnect ---

async def test_an_exhausted_reconnect_closes_the_client(storage):
    events = []
    remote = FakeRemote("192.168.1.42")
    ctrl = RemoteController(
        storage,
        remote_factory=lambda h, c, k: remote,
        emit=events.append,
        warmup=0.01,
        backoff=(0.0, 0.0),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()

    remote.connect_error = CannotConnect("network gone")
    remote.report_availability(False)
    await ctrl.wait_reconnect()

    assert ctrl.state == State.ERROR
    # without this a live socket lingers for the rest of the process
    assert remote.disconnect_count == 1
    with pytest.raises(ProtocolError) as err:
        ctrl.send_key("DPAD_UP")
    assert err.value.code == "not_connected"
    await ctrl.shutdown()


async def test_certificate_generation_uses_a_restrictive_umask(env):
    # The library writes the key with the process umask; for a moment it would
    # be readable by other processes of this user.
    import os

    ctrl, _, _ = env
    recorded = []
    original = os.umask

    def spy(new):
        recorded.append(new)
        return original(new)

    os.umask = spy
    try:
        await ctrl.pair_start(DEVICE)
    finally:
        os.umask = original

    assert 0o077 in recorded


# --- identity of the device at the remembered address ---

async def test_connecting_remembers_the_mac(env, storage):
    ctrl, _, _ = env
    await ctrl.connect(DEVICE)
    assert storage.load_state()["mac"] == "AA:BB:CC:DD:EE:FF"


async def test_refuses_to_connect_to_another_device_at_the_same_ip(env, storage):
    # The protocol does not verify the server certificate, so after a DHCP
    # lease change the backend would trust a stranger.
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()

    def stranger_factory(host, certfile, keyfile):
        stranger = FakeRemote(host)
        stranger.mac = "00:11:22:33:44:55"
        box["remote"] = stranger
        return stranger

    ctrl._remote_factory = stranger_factory
    with pytest.raises(ProtocolError) as err:
        await ctrl.connect(DEVICE)
    assert err.value.code == "wrong_device"
    assert ctrl.state == State.ERROR


async def test_pairing_may_replace_the_device_at_the_same_ip(env, storage):
    # A deliberate re-pairing is the only way to swap the device.
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()

    def new_factory(host, certfile, keyfile):
        replacement = FakeRemote(host)
        replacement.mac = "00:11:22:33:44:55"
        box["remote"] = replacement
        return replacement

    ctrl._remote_factory = new_factory
    await ctrl.pair_start(DEVICE)
    await ctrl.pair_finish("9E76AB")
    assert storage.load_state()["mac"] == "00:11:22:33:44:55"


async def test_no_remembered_mac_does_not_block_the_connection(env, storage):
    ctrl, _, _ = env
    storage.save_state(host="192.168.1.42", name="Chromecast")
    await ctrl.connect(DEVICE)
    assert ctrl.state in (State.CONNECTED, State.READY)


# --- no repeated statuses ---

async def test_an_identical_status_is_not_sent_twice(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()
    before = len(statuses(events))
    # three library callbacks for the same, unchanged state
    box["remote"].report_volume(10)
    box["remote"].report_power(True)
    box["remote"].report_app(box["remote"].current_app)
    assert len(statuses(events)) == before, "a repeated status reached the output"


async def test_a_changed_status_still_goes_out(env):
    ctrl, events, box = env
    await ctrl.connect(DEVICE)
    await ctrl.wait_ready()
    before = len(statuses(events))
    box["remote"].report_volume(7)
    assert len(statuses(events)) == before + 1


def factory_without_mac_readout(box):
    """A device whose MAC cannot be read (pairing port closed)."""
    def factory(host, certfile, keyfile):
        silent = FakeRemote(host)

        async def refuse():
            raise CannotConnect("pairing port closed")

        silent.async_get_name_and_mac = refuse
        box["remote"] = silent
        return silent
    return factory


async def test_an_unreadable_mac_does_not_block_the_connection(env, storage):
    # The MAC is read from the pairing port while control goes over another.
    # A sleeping TV is a normal state here, so the identity check must not
    # become a new reason for a failed connection.
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()

    ctrl._remote_factory = factory_without_mac_readout(box)
    await ctrl.connect(DEVICE)
    assert ctrl.state in (State.CONNECTED, State.READY)


async def test_an_unreadable_mac_does_not_erase_the_remembered_one(env, storage):
    # Otherwise the identity check would switch itself off and never return.
    ctrl, _, box = env
    await ctrl.connect(DEVICE)
    await ctrl.shutdown()
    assert storage.load_state()["mac"] == "AA:BB:CC:DD:EE:FF"

    ctrl._remote_factory = factory_without_mac_readout(box)
    await ctrl.connect(DEVICE)
    assert storage.load_state()["mac"] == "AA:BB:CC:DD:EE:FF"


async def test_status_is_published_as_soon_as_the_connection_is_adopted(env):
    # The device stays silent during the warm-up window while already taking
    # commands. Without an immediate status the panel would hold a stale
    # `powered` for ~2 s and grey out the whole remote.
    ctrl, events, _ = env
    await ctrl.connect(DEVICE)
    assert ctrl.state == State.CONNECTED
    assert statuses(events), "no status before READY"
    assert statuses(events)[-1]["powered"] is True


async def test_a_recovered_connection_also_publishes_a_status(storage):
    events = []
    remote = FakeRemote("192.168.1.42")
    ctrl = RemoteController(
        storage,
        remote_factory=lambda h, c, k: remote,
        emit=events.append,
        warmup=5.0,
        backoff=(0.0, 0.0),
        errors=(CannotConnect, ConnectionClosed, InvalidAuth),
    )
    await ctrl.connect(DEVICE)
    remote.report_availability(False)
    await ctrl.wait_reconnect()
    before_ready = [e for e in events if e.get("event") == "status"]
    assert before_ready, "reconnect left the panel without a status"
    await ctrl.shutdown()
