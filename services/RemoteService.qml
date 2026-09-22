pragma Singleton

import QtQuick
import Quickshell.Io

// Keeps a single backend process alive and turns JSON Lines into QML
// properties.
//
// A singleton, because the Omarchy bar instantiates widgets through
// `Variants { model: Quickshell.screens }` - one BarPanel per screen. Without
// this, plugging in a second monitor would spawn a second backend: another TLS
// session to the same Chromecast, two processes writing state.json and - worst
// of all - two concurrent calls to async_generate_cert_if_missing() on the same
// cert.pem/key.pem pair, a real risk of invalidating the pairing.
//
// The panel knows nothing about the protocol, TLS or the androidtvremote2
// library - it sees only a state, a device list and a status. The property is
// called `remoteState` because `state` already belongs to Item and drives
// visual states.
Item {
  id: root
  visible: false

  // A singleton cannot be configured from outside, so it works out the backend
  // path itself. decodeURIComponent, because Qt.resolvedUrl percent-encodes -
  // a directory with a space would otherwise break the launch.
  readonly property string helperPath:
    decodeURIComponent(Qt.resolvedUrl("../bin/google-tv-remote").toString()
      .replace(/^file:\/\//, ""))

  property string remoteState: "disconnected"
  property string stateMessage: "Disconnected"
  property var devices: []
  property bool powered: false
  property bool muted: false
  property int volume: 0
  property int volumeMax: 0
  property string appId: ""
  property string lastError: ""
  property bool discovering: false
  property string selectedHost: ""
  property string deviceName: ""

  // `connected` and `ready` differ only by the warm-up window (~2 s) in which
  // the device accepts commands but sends no events. Keys work in both, so
  // `live` covers the pair.
  readonly property bool live: remoteState === "connected" || remoteState === "ready"
  readonly property bool pairing: remoteState === "pairing"
  readonly property bool busy: remoteState === "connecting"
  readonly property bool failed: remoteState === "error"
  readonly property bool helperRunning: proc.running

  property int _nextId: 1
  property var _pending: ({})
  property int _restarts: 0
  property bool _stopping: false
  property bool _restartRequested: false

  readonly property int _maxRestarts: 3

  // --- process lifetime ---
  //
  // A singleton lives as long as the whole shell, so its Component.onDestruction
  // never fires when the plugin is disabled or reloaded - the backend was left
  // orphaned, holding a TLS session to the TV until the shell restarted.
  // So the panels decide instead: each one checks in when created and checks
  // out when destroyed, and the process lives while at least one is around.
  property int _panels: 0

  function attach() {
    _panels++
    releaseTimer.stop()
    start()
  }

  function detach() {
    _panels = Math.max(0, _panels - 1)
    // Reloading the plugin destroys the old panels before creating the new
    // ones, so closing immediately would drop the connection on every change
    // of bar settings. A short grace period lets such a swap through.
    if (_panels === 0) releaseTimer.restart()
  }

  Timer {
    id: releaseTimer
    interval: 3000
    repeat: false
    onTriggered: if (root._panels === 0) root.stop()
  }

  function start() {
    if (proc.running || helperPath === "") return
    _stopping = false
    restartTimer.stop()
    proc.running = true
  }

  function stop() {
    _stopping = true
    restartTimer.stop()
    if (proc.running) proc.running = false
  }

  function restart() {
    // A restart request has to survive the process exit: SIGTERM is
    // asynchronous, so clearing _stopping via Qt.callLater used to land before
    // onExited and the user got a crash message instead of a clean restart.
    _restarts = 0
    restartTimer.stop()
    if (proc.running) {
      _restartRequested = true
      _stopping = true
      proc.running = false
    } else {
      _stopping = false
      start()
    }
  }

  // --- komendy ---

  function _send(action, extra) {
    if (!proc.running) {
      lastError = "The backend is not running"
      return -1
    }
    var id = _nextId++
    var message = { "id": id, "action": action }
    if (extra) {
      for (var key in extra) message[key] = extra[key]
    }
    _pending[id] = action
    proc.write(JSON.stringify(message) + "\n")
    return id
  }

  function discover() {
    discovering = true
    devices = []
    discoverWatchdog.restart()
    if (_send("discover", null) < 0) {
      discovering = false
      discoverWatchdog.stop()
    }
  }

  function selectDevice(host) {
    selectedHost = String(host || "")
    _send("select_device", { "host": selectedHost })
  }

  function pairStart() { _send("pair_start", null) }
  function pairFinish(pin) { _send("pair_finish", { "pin": String(pin || "") }) }
  function key(name) { _send("key", { "key": String(name || "") }) }
  function refreshStatus() { _send("status", null) }

  // --- odbior ---

  function _handleLine(raw) {
    var line = String(raw || "").trim()
    if (line === "") return

    var message
    try {
      message = JSON.parse(line)
    } catch (e) {
      // The IPC channel is strictly JSON; garbage means a backend bug, not data.
      console.warn("omagoogletv", "unparsable line from the backend")
      return
    }
    if (!message || typeof message !== "object") return

    if (message.event === "state") {
      remoteState = String(message.state || "disconnected")
      stateMessage = String(message.message || "")
      lastError = remoteState === "error" ? stateMessage : ""
      return
    }

    if (message.event === "status") {
      _applyStatus(message)
      return
    }

    // After an automatic connection the panel sends no select_device, so this
    // is its only way to learn which device the backend works with.
    if (message.event === "device") {
      selectedHost = String(message.host || "")
      deviceName = String(message.name || "")
      return
    }

    if (message.id === undefined) return

    var action = _pending[message.id] || ""
    delete _pending[message.id]

    if (message.ok === true) {
      // Without this a one-off error (say a transient device_unreachable on
      // send) would stay red forever: once connected the backend emits no
      // further state events that would clear it.
      lastError = ""
      if (action === "discover") {
        devices = message.devices || []
        discovering = false
        discoverWatchdog.stop()
      } else if (action === "status" && message.status) {
        _applyStatus(message.status)
      }
      return
    }

    if (action === "discover") {
      discovering = false
      discoverWatchdog.stop()
    }
    lastError = String(message.message || message.code || "Error")
  }

  function _applyStatus(status) {
    powered = status.powered === true
    muted = status.muted === true
    volume = status.volume !== undefined ? status.volume : 0
    volumeMax = status.volume_max !== undefined ? status.volume_max : 0
    appId = status.app !== undefined ? String(status.app) : ""
  }

  Process {
    id: proc
    command: root.helperPath !== "" ? [root.helperPath] : []
    stdinEnabled: true

    // Only a backend that survived a while resets the restart counter.
    // Resetting it on every message caused an endless loop: the launcher
    // managed to emit a state event before dying on a setup error.
    onStarted: healthyTimer.restart()

    stdout: SplitParser {
      onRead: function(line) { root._handleLine(line) }
    }

    // The backend logs to stderr only; that goes to `qs log`, not to IPC.
    stderr: SplitParser {
      onRead: function(line) { console.log("omagoogletv", String(line)) }
    }

    onExited: function(exitCode, exitStatus) {
      healthyTimer.stop()
      root._onExited(exitCode)
    }
  }

  function _onExited(exitCode) {
    _pending = ({})
    discovering = false
    discoverWatchdog.stop()

    if (_restartRequested) {
      _restartRequested = false
      _stopping = false
      remoteState = "connecting"
      stateMessage = "Restarting the backend…"
      lastError = ""
      restartTimer.interval = 200
      restartTimer.start()
      return
    }

    if (_stopping) {
      remoteState = "disconnected"
      stateMessage = "Disconnected"
      return
    }

    remoteState = "error"
    if (_restarts < _maxRestarts) {
      _restarts++
      // Bounded backoff: 1 s, 2 s, 4 s. After that the panel waits for the user.
      restartTimer.interval = 1000 * Math.pow(2, _restarts - 1)
      stateMessage = "The backend stopped — retrying"
      restartTimer.start()
    } else {
      stateMessage = "The backend will not start — run bin/setup"
    }
    lastError = stateMessage
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: {
      if (!root._stopping) proc.running = true
    }
  }

  // The backend counts as healthy only once it has run for a good while.
  Timer {
    id: healthyTimer
    interval: 30000
    repeat: false
    onTriggered: root._restarts = 0
  }

  // Discovery with no answer must not leave the panel stuck on "Searching…".
  Timer {
    id: discoverWatchdog
    interval: 15000
    repeat: false
    onTriggered: {
      if (root.discovering) {
        root.discovering = false
        root.lastError = "Discovery did not answer"
      }
    }
  }

  // With no panels the backend is of no use to anyone - it starts on the
  // first attach(). Shutting the shell down destroys the singleton.
  Component.onDestruction: stop()
}
