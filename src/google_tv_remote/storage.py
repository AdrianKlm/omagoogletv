"""XDG paths, persistent state and permissions of sensitive files.

Credentials never reach the repository. Directories use mode 0700; the
certificate, the key and the state file use 0600.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any

from .messages import ProtocolError, require_local_host

APP_ID = "omagoogletv"

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _xdg_dir(variable: str, fallback: str) -> pathlib.Path:
    # An empty value counts as unset - the XDG spec says the same.
    value = os.environ.get(variable) or ""
    base = pathlib.Path(value) if value else pathlib.Path.home() / fallback
    return base / APP_ID


class Storage:
    """Resolves paths on construction, so tests can swap the environment."""

    def __init__(self) -> None:
        self.data_dir = _xdg_dir("XDG_DATA_HOME", ".local/share")
        self.credentials_dir = self.data_dir / "credentials"
        self.certfile = self.credentials_dir / "cert.pem"
        self.keyfile = self.credentials_dir / "key.pem"
        self.state_file = _xdg_dir("XDG_STATE_HOME", ".local/state") / "state.json"

    # --- directories and permissions ---

    def ensure_dirs(self) -> None:
        """Create the directories, tightening the mode if they were too open."""
        for directory in (self.data_dir, self.credentials_dir, self.state_file.parent):
            directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            directory.chmod(_DIR_MODE)

    def harden_credentials(self) -> None:
        """Called once the library has generated the certificate."""
        for path in (self.certfile, self.keyfile):
            if path.exists():
                path.chmod(_FILE_MODE)

    # --- state ---

    def load_state(self) -> dict[str, Any] | None:
        """Return the remembered device, or None when it is missing or untrustworthy.

        The state file can be replaced from outside the application, so it is
        validated exactly like a message arriving over IPC.
        """
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

        if not isinstance(raw, dict):
            return None

        try:
            host = require_local_host(raw.get("host"))
        except ProtocolError:
            return None

        name = raw.get("name")
        mac = raw.get("mac")
        return {
            "host": host,
            "name": name if isinstance(name, str) else "",
            "mac": mac if isinstance(mac, str) else "",
        }

    def save_state(self, *, host: str, name: str, mac: str = "") -> None:
        """Write the state atomically: temporary file first, then os.replace().

        The MAC is what tells us whether the remembered address still holds the
        same device - the protocol does not verify the server certificate.
        """
        host = require_local_host(host)
        self.ensure_dirs()
        payload = json.dumps({"host": host, "name": name, "mac": mac}, ensure_ascii=False)

        directory = self.state_file.parent
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(temporary, _FILE_MODE)
            os.replace(temporary, self.state_file)
        except BaseException:
            # A failed write must neither corrupt the old state nor leave litter.
            pathlib.Path(temporary).unlink(missing_ok=True)
            raise

    def clear_state(self) -> None:
        self.state_file.unlink(missing_ok=True)
