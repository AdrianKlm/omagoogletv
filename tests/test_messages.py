"""The JSON Lines contract between QML and the backend."""

import json

import pytest

from google_tv_remote.messages import (
    KEY_ALLOWLIST,
    ProtocolError,
    encode,
    error_response,
    ok_response,
    parse_request,
    state_event,
    status_event,
)


# --- accepted requests ---

def test_discover_takes_no_extra_fields():
    req = parse_request('{"id":1,"action":"discover"}')
    assert req.id == 1
    assert req.action == "discover"


def test_select_device_returns_host():
    req = parse_request('{"id":2,"action":"select_device","host":"192.168.1.42"}')
    assert req.host == "192.168.1.42"


def test_pair_finish_normalizes_pin_to_upper_case():
    # The Google TV PIN is alphanumeric (established in the spike), not digits only
    req = parse_request('{"id":4,"action":"pair_finish","pin":"9e76ab"}')
    assert req.pin == "9E76AB"


def test_key_accepts_a_key_from_the_allowlist():
    req = parse_request('{"id":5,"action":"key","key":"DPAD_UP"}')
    assert req.key == "DPAD_UP"


@pytest.mark.parametrize("key", sorted(KEY_ALLOWLIST))
def test_every_allowlisted_key_passes(key):
    assert parse_request(json.dumps({"id": 1, "action": "key", "key": key})).key == key


def test_allowlist_holds_exactly_the_supported_range():
    assert KEY_ALLOWLIST == frozenset({
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
        "BACK", "HOME", "MEDIA_PLAY_PAUSE", "VOLUME_DOWN", "VOLUME_UP",
        "MUTE", "POWER",
    })


def test_allowlist_excludes_power_variants_that_are_no_ops():
    # spike: TV_POWER/STB_POWER/AVR_POWER are accepted but do nothing
    for dead_key in ("TV_POWER", "STB_POWER", "AVR_POWER"):
        assert dead_key not in KEY_ALLOWLIST


# --- rejected requests ---

def test_rejects_invalid_json():
    with pytest.raises(ProtocolError) as err:
        parse_request("{this is not json")
    assert err.value.code == "invalid_request"


def test_rejects_json_that_is_not_an_object():
    for payload in ("[1,2,3]", '"text"', "42", "null"):
        with pytest.raises(ProtocolError):
            parse_request(payload)


def test_rejects_key_outside_the_allowlist():
    with pytest.raises(ProtocolError) as err:
        parse_request('{"id":5,"action":"key","key":"KEYCODE_DELETE"}')
    assert err.value.code == "invalid_key"


def test_rejects_key_with_the_keycode_prefix():
    # send_key_command takes unprefixed names; two spellings of one key is worse
    with pytest.raises(ProtocolError):
        parse_request('{"id":5,"action":"key","key":"KEYCODE_DPAD_UP"}')


def test_rejects_unknown_action():
    with pytest.raises(ProtocolError) as err:
        parse_request('{"id":9,"action":"run_arbitrary_command"}')
    assert err.value.code == "unknown_action"


def test_rejects_missing_action():
    with pytest.raises(ProtocolError):
        parse_request('{"id":9}')


def test_rejects_extra_fields():
    with pytest.raises(ProtocolError) as err:
        parse_request('{"id":1,"action":"discover","cmd":"rm -rf /"}')
    assert err.value.code == "invalid_request"


@pytest.mark.parametrize("bad_id", ["1", 1.5, None, -1, 0, True])
def test_rejects_bad_id(bad_id):
    with pytest.raises(ProtocolError):
        parse_request(json.dumps({"id": bad_id, "action": "discover"}))


@pytest.mark.parametrize("bad_type", [123, None, [], {}, True])
def test_rejects_key_of_an_unsafe_type(bad_type):
    with pytest.raises(ProtocolError):
        parse_request(json.dumps({"id": 5, "action": "key", "key": bad_type}))


def test_rejects_missing_required_field():
    with pytest.raises(ProtocolError):
        parse_request('{"id":2,"action":"select_device"}')
    with pytest.raises(ProtocolError):
        parse_request('{"id":4,"action":"pair_finish"}')


# --- host validation ---

@pytest.mark.parametrize("host", ["192.168.1.42", "10.0.0.5", "172.16.0.1", "169.254.1.1"])
def test_accepts_local_addresses(host):
    assert parse_request(json.dumps({"id": 2, "action": "select_device", "host": host})).host == host


@pytest.mark.parametrize("host", [
    "8.8.8.8",            # public
    "127.0.0.1",          # loopback - a Chromecast is never there
    "224.0.0.1",          # multicast
    "not-an-address",
    "192.168.1.42; rm -rf /",
    "192.168.1.999",
    "192.168.1.42:6466",  # the port does not belong in the host field
    "",
])
def test_rejects_invalid_or_non_local_host(host):
    with pytest.raises(ProtocolError) as err:
        parse_request(json.dumps({"id": 2, "action": "select_device", "host": host}))
    assert err.value.code == "invalid_host"


# --- PIN validation ---

@pytest.mark.parametrize("pin", ["12345", "1234567", "", "ABC 12", "ABC-12", "ÄÖÜ123"])
def test_rejects_invalid_pin(pin):
    with pytest.raises(ProtocolError) as err:
        parse_request(json.dumps({"id": 4, "action": "pair_finish", "pin": pin}))
    assert err.value.code == "invalid_pin"


# --- responses and events ---

def test_ok_response_carries_id_and_ok():
    assert ok_response(5) == {"id": 5, "ok": True}


def test_ok_response_adds_extra_fields():
    resp = ok_response(1, devices=[{"name": "Chromecast", "host": "192.168.1.42"}])
    assert resp["devices"][0]["host"] == "192.168.1.42"


def test_error_response_carries_code_and_message():
    resp = error_response(9, "device_unreachable", "The Chromecast is unreachable")
    assert resp == {"id": 9, "ok": False, "code": "device_unreachable",
                    "message": "The Chromecast is unreachable"}


def test_state_event_has_the_agreed_shape():
    assert state_event("pairing", "Enter the code from your TV") == {
        "event": "state", "state": "pairing", "message": "Enter the code from your TV"}


def test_status_event_has_the_agreed_shape():
    ev = status_event(connected=True, powered=True, muted=False, volume=37)
    assert ev == {"event": "status", "connected": True, "powered": True,
                  "muted": False, "volume": 37, "volume_max": 0, "app": ""}


def test_status_event_carries_volume_scale_and_app():
    ev = status_event(connected=True, powered=True, muted=False, volume=11,
                      volume_max=25, app="com.google.android.youtube.tv")
    assert ev["volume_max"] == 25
    assert ev["app"] == "com.google.android.youtube.tv"


def test_encode_ends_with_newline_and_has_none_inside():
    line = encode({"event": "state", "state": "error", "message": "error\nwith a newline"})
    assert line.endswith("\n")
    assert line.count("\n") == 1


def test_encode_preserves_non_ascii_characters():
    assert "Wohnzimmer-Fernseher" in encode({"message": "Wohnzimmer-Fernseher"})


def test_encode_produces_parsable_json():
    assert json.loads(encode(ok_response(1)))["ok"] is True


# --- tying errors back to the request ---

def test_validation_error_carries_the_request_id():
    # the panel has to match an error with the request that caused it
    with pytest.raises(ProtocolError) as err:
        parse_request('{"id":42,"action":"key","key":"KEYCODE_DELETE"}')
    assert err.value.request_id == 42


def test_unknown_action_error_carries_the_request_id():
    with pytest.raises(ProtocolError) as err:
        parse_request('{"id":7,"action":"whatever"}')
    assert err.value.request_id == 7


def test_error_before_the_id_was_read_uses_zero():
    with pytest.raises(ProtocolError) as err:
        parse_request("{this is not json")
    assert err.value.request_id == 0


# --- device event ---

def test_device_event_carries_host_and_name():
    from google_tv_remote.messages import device_event
    assert device_event(host="192.168.1.42", name="Chromecast") == {
        "event": "device", "host": "192.168.1.42", "name": "Chromecast"}


def test_device_event_without_a_device_has_empty_fields():
    from google_tv_remote.messages import device_event
    assert device_event(host="", name="") == {"event": "device", "host": "", "name": ""}


# --- edge-case addresses ---

@pytest.mark.parametrize("host", ["0.0.0.0", "255.255.255.255", "240.0.0.1"])
def test_rejects_unusable_addresses_python_still_calls_private(host):
    # 0.0.0.0 connects to localhost on Linux, and both are is_private=True
    with pytest.raises(ProtocolError) as err:
        parse_request(json.dumps({"id": 2, "action": "select_device", "host": host}))
    assert err.value.code == "invalid_host"
