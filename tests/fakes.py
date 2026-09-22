"""The androidtvremote2 stand-in used by the controller tests."""

from __future__ import annotations


class CannotConnect(Exception):
    pass


class ConnectionClosed(Exception):
    pass


class InvalidAuth(Exception):
    pass


class FakeRemote:
    """Mirrors the behaviour observed during the spike, warm-up window included."""

    def __init__(self, host, *, certfile="", keyfile=""):
        self.host = host
        self.certfile = certfile
        self.keyfile = keyfile

        self.cert_exists = False
        self.connect_error: Exception | None = None
        self.pairing_error: Exception | None = None

        self.sent_keys: list[str] = []
        self.connect_count = 0
        self.disconnect_count = 0
        self.pairing_started = False
        self.received_pin: str | None = None

        self.mac = "AA:BB:CC:DD:EE:FF"
        self.is_on = True
        self.current_app = "com.google.android.apps.tv.launcherx"
        self.volume_info = {"level": 10, "max": 25, "muted": False}

        self._cb_volume: list = []
        self._cb_is_on: list = []
        self._cb_available: list = []
        self._cb_app: list = []

    # --- library API ---

    async def async_generate_cert_if_missing(self):
        if self.cert_exists:
            return False
        self.cert_exists = True
        return True

    async def async_get_name_and_mac(self):
        if self.connect_error:
            raise self.connect_error
        return "Chromecast", self.mac

    async def async_start_pairing(self):
        if self.connect_error:
            raise self.connect_error
        self.pairing_started = True

    async def async_finish_pairing(self, pairing_code):
        self.received_pin = pairing_code
        if self.pairing_error:
            raise self.pairing_error

    async def async_connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connect_count += 1

    def disconnect(self):
        self.disconnect_count += 1

    def send_key_command(self, key_code, direction=3):
        if self.connect_error:
            raise self.connect_error
        self.sent_keys.append(key_code)

    def add_volume_info_updated_callback(self, cb):
        self._cb_volume.append(cb)

    def add_is_on_updated_callback(self, cb):
        self._cb_is_on.append(cb)

    def add_is_available_updated_callback(self, cb):
        self._cb_available.append(cb)

    def add_current_app_updated_callback(self, cb):
        self._cb_app.append(cb)

    # --- driven from the test ---

    def report_volume(self, level, muted=False):
        self.volume_info = {"level": level, "max": 25, "muted": muted}
        for cb in self._cb_volume:
            cb(self.volume_info)

    def report_power(self, is_on):
        self.is_on = is_on
        for cb in self._cb_is_on:
            cb(is_on)

    def report_app(self, app):
        self.current_app = app
        for cb in self._cb_app:
            cb(app)

    def report_availability(self, available):
        for cb in self._cb_available:
            cb(available)
