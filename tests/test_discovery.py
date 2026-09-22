"""Device discovery over mDNS and manually typed IPs."""

import asyncio

import pytest

from google_tv_remote.discovery import (
    _GRACE,
    SERVICE_TYPE,
    Device,
    RawService,
    device_from_host,
    discover,
)
from google_tv_remote.messages import ProtocolError


class FakeSource:
    """Stands in for zeroconf in tests - no network traffic at all."""

    def __init__(self, services, delay=0.0):
        self.services = services
        self.delay = delay
        self.calls = []

    async def browse(self, service_type, timeout):
        self.calls.append((service_type, timeout))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.services


def service(name="Chromecast", addresses=("192.168.1.42",), port=6466):
    return RawService(name=name, addresses=list(addresses), port=port)


# --- happy path ---

async def test_finds_a_device():
    result = await discover(source=FakeSource([service()]))
    assert result == [Device(name="Chromecast", host="192.168.1.42")]


async def test_uses_the_right_service_type():
    source = FakeSource([])
    await discover(source=source)
    assert source.calls[0][0] == SERVICE_TYPE
    assert SERVICE_TYPE == "_androidtvremote2._tcp.local."


async def test_no_devices_yields_an_empty_list():
    assert await discover(source=FakeSource([])) == []


async def test_strips_the_service_type_suffix_from_the_name():
    raw = service(name="Living room._androidtvremote2._tcp.local.")
    assert (await discover(source=FakeSource([raw])))[0].name == "Living room"


async def test_result_is_sorted_deterministically():
    result = await discover(source=FakeSource([
        service("Bedroom", ("192.168.1.120",)),
        service("Chromecast", ("192.168.1.113",)),
        service("Chromecast", ("192.168.1.99",)),
    ]))
    # Name first, then the address. Within the two Chromecasts .99 comes
    # before .113 because sorting goes by address value, not by text - as
    # text "192.168.1.113" would come before "192.168.1.99".
    assert [(d.name, d.host) for d in result] == [
        ("Bedroom", "192.168.1.120"),
        ("Chromecast", "192.168.1.99"),
        ("Chromecast", "192.168.1.113"),
    ]


async def test_device_data_serializes_to_the_contract():
    assert Device(name="Chromecast", host="192.168.1.42").as_dict() == {
        "name": "Chromecast", "host": "192.168.1.42"}


# --- robustness ---

async def test_skips_a_service_without_an_address():
    assert await discover(source=FakeSource([service(addresses=())])) == []


async def test_skips_non_local_addresses():
    assert await discover(source=FakeSource([service(addresses=("8.8.8.8",))])) == []


async def test_skips_ipv6_and_keeps_ipv4():
    # the same service is announced over IPv4 and IPv6 (seen during the spike)
    raw = service(addresses=("fe80::1", "192.168.1.42"))
    assert (await discover(source=FakeSource([raw])))[0].host == "192.168.1.42"


async def test_deduplicates_the_same_device():
    result = await discover(source=FakeSource([service(), service(), service()]))
    assert len(result) == 1


async def test_keeps_a_service_with_an_empty_name_but_a_valid_host():
    result = await discover(source=FakeSource([service(name="")]))
    assert result == [Device(name="", host="192.168.1.42")]


async def test_timeout_is_passed_through_to_the_source():
    source = FakeSource([])
    await discover(source=source, timeout=2.5)
    assert source.calls[0][1] == 2.5


async def test_exceeded_timeout_yields_an_empty_list_instead_of_hanging():
    # The bound is tied to the constant rather than hard-coded: changing the
    # headroom should not break a test that guards against hanging.
    source = FakeSource([service()], delay=30.0)
    result = await asyncio.wait_for(
        discover(source=source, timeout=0.1), timeout=0.1 + _GRACE + 1.0)
    assert result == []


async def test_a_source_failure_does_not_escape():
    class BrokenSource:
        async def browse(self, service_type, timeout):
            raise OSError("no network interface")

    assert await discover(source=BrokenSource()) == []


# --- manual IP ---

def test_manual_ip_yields_a_device():
    assert device_from_host("192.168.1.42") == Device(name="", host="192.168.1.42")


def test_manual_ip_keeps_the_given_name():
    assert device_from_host("192.168.1.42", name="Living room").name == "Living room"


@pytest.mark.parametrize("bad", ["8.8.8.8", "127.0.0.1", "not-an-address", "", "10.0.0.1; reboot"])
def test_manual_ip_rejects_an_invalid_host(bad):
    with pytest.raises(ProtocolError):
        device_from_host(bad)
