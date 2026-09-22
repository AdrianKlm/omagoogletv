# omagoogletv

A local remote for Chromecast with Google TV, as a bar widget for Omarchy. The
backend speaks the Android TV Remote Protocol v2 directly to the device — no
ADB, no Google account, no cloud service. Everything stays on the local network.

## Requirements

- Omarchy 4.0.4-1, Python 3.12+
- `avahi-daemon` running (discovery over mDNS)
- A Chromecast on the same local network

## Installation

```bash
omarchy plugin add https://github.com/AdrianKlm/omagoogletv.git
omarchy plugin enable omagoogletv right
```

That is the whole thing. The Python backend installs itself: the first time you
click the icon, the launcher builds an isolated environment in
`~/.local/share/omagoogletv/venv/` and the panel shows "First run — installing
dependencies…" for a few seconds. Nothing lands outside your home directory,
nothing is installed globally and no `sudo` is involved.

The only moment a network connection is needed is that first build, which pulls
the pinned dependencies from PyPI. Every one of them is verified against the
sha256 sums in `requirements.lock`.

Run `./bin/setup` by hand if you would rather prepare the environment up front,
or to repair a broken one. It is also re-run automatically whenever the
dependencies change or the system Python is upgraded.

### From a local checkout

```bash
ln -s "$PWD" ~/.config/omarchy/plugins/omagoogletv
omarchy-shell shell rescanPlugins
omarchy plugin enable omagoogletv right
```

## Using the panel

| Key | Action |
|---|---|
| arrows or `h/j/k/l` | D-pad (with no connection: the device list) |
| `Enter` | OK (with no connection: pick from the list) |
| `Backspace` or `b` | Back |
| `g` | Home |
| `Space` or `p` | Play/pause |
| `-` / `+` | Volume down / up |
| `m` | Mute |
| `q` or `Escape` | Close the panel |

Every button works with the mouse too and carries a tooltip.

## Running the backend directly

```bash
./bin/google-tv-remote
```

The backend reads one JSON message per line from `stdin` and answers on
`stdout`. Logs go to `stderr` only.

### First pairing

```json
{"id":1,"action":"discover"}
{"id":2,"action":"select_device","host":"192.168.1.42"}
{"id":3,"action":"pair_start"}
{"id":4,"action":"pair_finish","pin":"9E76AB"}
```

The PIN is **alphanumeric** and appears on the TV. Once paired, later runs
connect to the remembered device automatically — no PIN.

### Control

```json
{"id":5,"action":"key","key":"DPAD_UP"}
{"id":6,"action":"status"}
```

Allowed keys: `DPAD_UP`, `DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`, `DPAD_CENTER`,
`BACK`, `HOME`, `MEDIA_PLAY_PAUSE`, `VOLUME_DOWN`, `VOLUME_UP`, `MUTE`, `POWER`.
Anything outside that list is rejected.

`{"ok":true}` for a key means **"the command was sent"**, not "the device
reacted". The protocol only confirms changes of volume, app and power — as
established during the protocol spike.

### States

`disconnected` → `connecting` → `pairing` → `connected` → `ready`, plus `error`.

`connected` and `ready` are separate on purpose: for ~2 s after connecting the
device accepts commands but sends no events. Keys already work in `connected`.

A powered-off TV (`powered: false`) is a **normal state**, not an error. The
error is `device_unreachable`.

### Device identity

The protocol deliberately skips server certificate verification — trust is
bound by the PIN, not by a certificate authority. So the device MAC is recorded
at pairing time and checked on every connection. If different hardware answers
at the remembered address (a changed DHCP lease, an impersonation on the
network), the connection ends with `wrong_device` and you have to pair again
deliberately.

The check can only **refuse** — it is never the reason a connection fails. The
MAC is read from the pairing port while control runs over another one, so when
the read fails the backend connects normally and leaves the remembered MAC
untouched.

## User data

| What | Where | Mode |
|---|---|---|
| Python environment | `~/.local/share/omagoogletv/venv/` | inside a 0700 directory |
| certificate and key | `~/.local/share/omagoogletv/credentials/` | 0600 |
| remembered device (host, name, MAC) | `~/.local/state/omagoogletv/state.json` | 0600 |

None of it reaches the repository.

## Removing all data

```bash
rm -rf ~/.local/share/omagoogletv
rm -rf ~/.local/state/omagoogletv
```

Deleting `credentials/` invalidates the pairing — the next connection asks for
a PIN again.

## Updating

```bash
git pull && ./bin/setup
```

`requirements.lock` pins exact versions **together with sha256 sums**, so a
swapped artifact on PyPI will not install silently. It also contains
`setuptools` — not as a runtime dependency, but so the package can be built
with `--no-build-isolation`, i.e. without fetching anything outside the
checksum check. Bump versions by hand, and only after re-testing pairing and
the keys against real hardware.

## Tests

Keep the development environment **outside** the plugin directory — the Omarchy
validator rejects symlinks inside it, and a `venv` is full of them.

Installation takes two steps, because `requirements.lock` carries checksums.
Pip then switches to verification mode for **all** packages at once, and in
that mode it accepts neither `-e .` nor unpinned tools:

```bash
python3 -m venv ~/.cache/omagoogletv/venv
V=~/.cache/omagoogletv/venv/bin/python
$V -m pip install -r requirements.lock
$V -m pip install -e . pytest pytest-asyncio
$V -m pytest -q
omarchy plugin validate .
```

The unit tests use a device stand-in. Pairing, keys, volume and reconnecting
need a test against real hardware — see `docs/spike-results.md`.

## License

MIT — see [LICENSE](LICENSE).
