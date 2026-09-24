# Protocol spike — results

Result: **GO**

## 1. Environment

| Component | Version |
|---|---|
| Omarchy | 4.0.4-1 |
| Python | 3.14.7 |
| avahi-browse | 0.9-rc5 |
| avahi-daemon | active |
| git | 2.55.0 |
| androidtvremote2 | 0.3.2 |
| zeroconf | 0.151.3 |

Transitive dependencies: `aiofiles==25.1.0`, `cffi==2.1.1`, `cryptography==50.0.1`,
`ifaddr==0.2.0`, `protobuf==7.36.2`, `pycparser==3.0`.

Everything was installed into a local `.venv/`. No administrator rights, no global install.

## 2. Discovering the device

Command: `avahi-browse -rt _androidtvremote2._tcp` — answered in ~2 s, with no
subnet scanning.

| Field | Value |
|---|---|
| Service name | `Chromecast` |
| Type | `_androidtvremote2._tcp.local.` |
| Hostname | `Android_<device-identifier>.local` |
| Address | `192.168.1.42` |
| Port | `6466` (pairing: `6467`) |
| TXT | `bt=AA:BB:CC:DD:EE:FF` |
| Interface | `wlan0`, laptop `192.168.1.10/24` |

The service is announced over both IPv4 and IPv6. The name `Chromecast` is
generic — the panel should show the IP address as well, because the name alone
does not tell two devices apart.

`async_get_name_and_mac()` returned `name=Chromecast`,
`mac=AA:BB:CC:DD:EE:FF`, matching the TXT record.

## 3. Pairing

- The certificate was generated locally by `async_generate_cert_if_missing()` → `True`.
- The PIN (6 characters, alphanumeric — capitals and digits) appeared on the TV.
- `async_finish_pairing()` succeeded on the first attempt.
- The PIN was passed through a temporary file outside the repository and deleted
  right after use (`unlink` in a `finally` block); only its length appears in logs.

Important: **the PIN is alphanumeric, not numeric.** The QML field must not be
limited to digits; it should enforce capitals and a length of 6.

## 4. Key tests

All keys were accepted without exception. The "confirmation" column separates
protocol events coming back from the device from visual verification on screen.

| Key | Result | Confirmation |
|---|---|---|
| `DPAD_UP` | works | visual (selection moved) |
| `DPAD_DOWN` | works | visual |
| `DPAD_LEFT` | works | visual |
| `DPAD_RIGHT` | works | visual |
| `DPAD_CENTER` | works | visual |
| `BACK` | works | visual |
| `HOME` | works | `current_app` event → `com.google.android.apps.tv.launcherx` |
| `MEDIA_PLAY_PAUSE` | works | visual |
| `VOLUME_DOWN` | works | `volume_info` event, `level 10 → 9` |
| `VOLUME_UP` | works | `volume_info` event, `level 9 → 10` |
| `MUTE` | works | `volume_info` event, `muted: false → true`, `level → 0` |
| `POWER` | works (toggle) | `is_on` event both ways — details below |

Keys that did not work: **none**.

Naming: `send_key_command()` takes names without the `KEYCODE_` prefix
(`"DPAD_UP"`), and also plain `int`s. The allowlist should work on unprefixed
names and map them explicitly.

### `POWER` — tested separately

| Variant | Result |
|---|---|
| `POWER` | **works**, behaves as a toggle |
| `TV_POWER` | accepted without error, **no effect** (no-op) |
| `STB_POWER` | accepted without error, **no effect** (no-op) |
| `AVR_POWER` | accepted without error, **no effect** (no-op) |

- powering off: `is_on True → False` after 1105 ms (`is_on` event),
- powering on: `is_on False → True` after 302 ms,
- **the TLS connection survived standby** — once off, the device still answered
  and reported its state; waking it needed neither a reconnect nor re-pairing.

Implementation conclusions: the allowlist contains `POWER` only (the other three
variants are useless and would merely mislead). Because `POWER` toggles, the
panel must not have separate "on"/"off" buttons — it has one button and takes
the current state from `is_on`. Powering the device off is **not** the same as
losing the connection and must not be presented as an error.

## 5. Latency

`send_key_command()` is non-blocking — it returns after 0.2–0.6 ms because it
only writes into the TLS buffer. **That is not a latency measurement.** Real
timing was measured as the round trip from sending `VOLUME_*` to the
`volume_info` event arriving from the device:

| Metric | Value |
|---|---|
| RTT median | 61 ms |
| RTT min | 37 ms |
| RTT max | 148 ms |
| `async_connect()` time | 134–205 ms (3 measurements) |

In practice the reaction feels immediate, with no perceptible lag. Wi-Fi
network, same subnet.

## 6. Reconnecting after a process restart

The process was stopped and started again three times. Every time
`async_connect()` succeeded **without asking for a PIN again**, in 134–205 ms,
with a correct initial state (`is_on`, `current_app`, `volume_info`).

Certificate and key: `~/.local/share/io.github.adrianklm.omagoogletv/credentials/{cert.pem,key.pem}`,
mode `0600`, directories `0700`. Kept out of the repository; `.gitignore`
additionally blocks `credentials/`, `*.pem`, `*.key`, `*.crt`, `state.json`.

## 7. Error behaviour

| Scenario | Behaviour |
|---|---|
| A free IP on the LAN (`192.168.1.199`) | `CannotConnect`, fast failure, no hang |
| No certificate for `keys`/`reconnect` | readable message, exit code ≠ 0 |

The library also exposes `InvalidAuth` (revoked certificate) and
`ConnectionClosed`.

## 8. Risks and conclusions

1. **A ~2 s warm-up window after `async_connect()`** — the most important
   finding. Commands sent immediately after connecting are **delivered** (the
   volume really does change), but the device sends no `volume_info` events in
   that window. It repeated across three runs: the first two commands produced
   no event. After waiting 2.5 s the first command gave an RTT of 148 ms and a
   correct event.
   → The backend **must not** treat a missing event as a failed command, and the
   UI should not present "connected" as ready until the initial state settles.
2. **No confirmation for the D-pad, BACK and play/pause.** The protocol confirms
   only changes of volume, app and power. The IPC contract has to separate
   "command sent" from "the device reacted"; `{"id":5,"ok":true}` means only the
   former.
3. **The device name is generic** (`Chromecast`) — the panel must show the IP
   next to the name.
4. **The PIN is alphanumeric** — do not restrict the input field to digits.
5. **`POWER` verified.** It works as a toggle and the connection survives
   standby. The backend has to distinguish "device powered off" (`is_on=False`,
   a normal state) from "device unreachable" (`CannotConnect`, an error state) —
   two different things as far as the UI is concerned.
6. **Volume is global and quantised** (`max: 25`) — changes are immediately
   visible to other clients; the panel should reflect the state from events
   rather than a counter of its own.
7. A real Wi-Fi outage was not tested (a process restart was tested instead).
   The library's `keep_reconnecting()` is a candidate for automatic recovery.

## 9. Exit criteria

| Requirement | Status |
|---|---|
| Pairing works | met |
| Four directions and OK work | met |
| At least two volume commands work | met (3: down, up, mute) |
| The certificate allows reconnecting | met (3 restarts, no PIN) |

## 10. Recommendation

**GO.** The stack is confirmed against a real device: `androidtvremote2==0.3.2`
handles pairing, persistent credentials and the full key range at a latency
below the threshold of noticeability. No reason was found to change the
architecture.

> The MAC address and the unique mDNS hostname have been replaced with example
> values in this document. They are permanent hardware identifiers and do not
> belong in a repository.

## 11. Reproducing

```bash
avahi-browse -rt _androidtvremote2._tcp
```

The spike script was explicitly marked **throwaway** and removed from the
working tree once the spike closed — `omarchy plugin add` clones the whole
repository, so one-off code has no business landing in a user's plugin
directory. The production backend was then written test-first.
