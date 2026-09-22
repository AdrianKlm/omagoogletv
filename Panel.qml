pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import qs.Ui
import "services"
import "components"

// The bar icon and the remote panel. The panel knows nothing about the
// protocol or TLS - it talks only to RemoteService, which turns JSON Lines
// into QML properties.
Panel {
  id: root
  moduleName: "omagoogletv"
  ipcTarget: "omagoogletv"

  readonly property string stateIcon: {
    if (root.remote.pairing) return "󰌆"
    if (root.remote.busy) return "󰝲"
    if (root.remote.failed) return "󰌸"
    if (root.remote.live) return root.remote.powered ? "󰠹" : "󰐥"
    return "󰻅"
  }

  readonly property string deviceLabel: {
    if (root.remote.selectedHost === "") return "No device"
    // The mDNS name is often generic, so the IP stays visible next to it.
    if (root.remote.deviceName === "") return root.remote.selectedHost
    return root.remote.deviceName + " · " + root.remote.selectedHost
  }

  // App names are a presentation concern - the backend knows only the package
  // identifier and should not care what the panel calls it.
  readonly property var appNames: ({
    "com.google.android.apps.tv.launcherx": "Home",
    "com.google.android.tvlauncher": "Home",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.google.android.apps.mediashell": "Casting",
    "com.netflix.ninja": "Netflix",
    "com.canal.android.canal": "Canal+",
    "com.spotify.tv.android": "Spotify",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.wbd.stream": "HBO Max",
    "com.plexapp.android": "Plex",
    "org.jellyfin.androidtv": "Jellyfin",
    "org.videolan.vlc": "VLC"
  })

  function appLabel(packageName) {
    var id = String(packageName || "")
    if (id === "") return ""
    if (appNames[id] !== undefined) return appNames[id]
    // Fallback for unknown packages: the last meaningful part of the name.
    var parts = id.split(".").filter(function(part) {
      return ["", "com", "org", "net", "android", "tv", "app", "apps"].indexOf(part) < 0
    })
    if (parts.length === 0) return id
    var last = parts[parts.length - 1]
    return last.charAt(0).toUpperCase() + last.slice(1)
  }

  // Bumped on every command sent; buttons with a matching name flash. Works
  // the same for the mouse and for the keyboard.
  property string lastKey: ""
  property int lastKeySeq: 0

  function sendKey(name) {
    // One gate for mouse and keyboard alike. Buttons used to be disabled on a
    // sleeping device while a shortcut still sent the command - and the greyed
    // out button flashed anyway, because the counter kept rising.
    if (!root.remote.powered && name !== "POWER") return
    root.remote.key(name)
    lastKey = name
    lastKeySeq++
  }

  // A singleton shared by every bar instance (one per screen).
  readonly property var remote: RemoteService

  // The singleton outlives the plugin being unloaded, so the panels decide how
  // long the backend lives. Without this, disabling the plugin left the process
  // running.
  Component.onCompleted: RemoteService.attach()
  Component.onDestruction: RemoteService.detach()

  // The panel state is fed by events. Should one ever be lost, the user would
  // stare at a stale volume or app - so we refresh exactly when it opens.
  onOpenedChanged: if (opened && root.remote.live) root.remote.refreshStatus()

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.stateIcon
    tooltipText: "Google TV — " + root.remote.stateMessage
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(280))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      // While typing the PIN the letters belong to the field, not to the remote.
      blocked: pairingView.visible && pairingView.editing

      // Enter emits returnRequested and activateRequested; Space emits only the
      // latter. The flag separates OK (Enter) from play/pause (Space). It is
      // also cleared by Qt.callLater, so an Enter with no activateRequested
      // behind it cannot swallow the next Space.
      property bool enterHandled: false

      function markEnter() {
        enterHandled = true
        Qt.callLater(function() { keyCatcher.enterHandled = false })
      }

      onMoveRequested: function(dx, dy) {
        // With no connection the arrows walk the device list, not the TV.
        if (!root.remote.live) {
          if (dy !== 0) pairingView.moveCursor(dy)
          return
        }
        if (dy < 0) root.sendKey("DPAD_UP")
        else if (dy > 0) root.sendKey("DPAD_DOWN")
        else if (dx < 0) root.sendKey("DPAD_LEFT")
        else if (dx > 0) root.sendKey("DPAD_RIGHT")
      }

      onReturnRequested: {
        markEnter()
        if (root.remote.live) root.sendKey("DPAD_CENTER")
        else pairingView.activate()
      }

      onActivateRequested: {
        if (enterHandled) { enterHandled = false; return }
        if (root.remote.live) root.sendKey("MEDIA_PLAY_PAUSE")
        else pairingView.activate()
      }

      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      onTextKey: function(text) {
        if (text === "q") { root.close(); return }
        if (!root.remote.live) return
        // Backspace arrives here as "\b" - PanelKeyCatcher has no separate
        // signal for it.
        if (text === "b" || text === "\b") root.sendKey("BACK")
        else if (text === "g") root.sendKey("HOME")
        else if (text === "p") root.sendKey("MEDIA_PLAY_PAUSE")
        else if (text === "m") root.sendKey("MUTE")
        else if (text === "-" || text === "_") root.sendKey("VOLUME_DOWN")
        else if (text === "+" || text === "=") root.sendKey("VOLUME_UP")
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        // ---------- header ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(titleColumn.implicitHeight, stateGlyph.implicitHeight)

          Column {
            id: titleColumn
            anchors.left: parent.left
            anchors.right: stateGlyph.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "Google TV"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.remote.stateMessage.toUpperCase()
              color: root.remote.failed ? Color.urgent : Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Text {
            id: stateGlyph
            textFormat: Text.PlainText
            text: root.stateIcon
            color: root.remote.failed ? Color.urgent : root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        PanelSeparator { width: parent.width }

        // ---------- remote ----------
        Column {
          width: parent.width
          visible: root.remote.live
          spacing: Style.space(8)

          // Two lines instead of one: the name plus the IP does not fit across the
          // panel next to the state, and eliding hid the volume of all things.
          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.deviceLabel
            color: Qt.darker(root.bar.foreground, 1.3)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.remote.powered && root.remote.appId !== ""
            text: "󰐊  " + root.appLabel(root.remote.appId)
            color: Qt.darker(root.bar.foreground, 1.3)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: !root.remote.powered
            text: "Device is off"
            color: Qt.darker(root.bar.foreground, 1.3)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }

          // Volume: a meter plus the number, because the bar alone does not say how
          // much exactly.
          Item {
            width: parent.width
            visible: root.remote.powered
            implicitHeight: Math.max(meter.implicitHeight, volumeLabel.implicitHeight)

            VolumeMeter {
              id: meter
              anchors.left: parent.left
              anchors.right: volumeLabel.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
              value: root.remote.volume
              maximum: root.remote.volumeMax
              muted: root.remote.muted
              foreground: root.bar.foreground
            }

            Text {
              id: volumeLabel
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.remote.muted ? "󰝟" : String(root.remote.volume)
              color: Qt.darker(root.bar.foreground, 1.2)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          DirectionPad {
            anchors.horizontalCenter: parent.horizontalCenter
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            // There is nothing to navigate on a sleeping device; POWER wakes it.
            enabled: root.remote.powered
            activeKey: root.lastKey
            activeSeq: root.lastKeySeq
            onKeyRequested: function(k) { root.sendKey(k) }
          }

          // Back and Home
          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(4)

            RemoteButton {
              keyName: "BACK"
              iconText: "󰌍"
              tooltipText: "Back  (Backspace / b)"
              enabled: root.remote.powered
              foreground: root.bar.foreground
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              onKeyRequested: function(k) { root.sendKey(k) }
            }

            RemoteButton {
              keyName: "HOME"
              iconText: "󰋜"
              tooltipText: "Home  (g)"
              enabled: root.remote.powered
              foreground: root.bar.foreground
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              onKeyRequested: function(k) { root.sendKey(k) }
            }
          }

          PanelSeparator { width: parent.width }

          // Media and volume
          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(4)

            RemoteButton {
              keyName: "MEDIA_PLAY_PAUSE"
              enabled: root.remote.powered
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              iconText: "󰐎"
              tooltipText: "Play / pause  (Space / p)"
              foreground: root.bar.foreground
              onKeyRequested: function(k) { root.sendKey(k) }
            }

            RemoteButton {
              keyName: "MUTE"
              enabled: root.remote.powered
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              iconText: root.remote.muted ? "󰝟" : "󰕾"
              tooltipText: root.remote.muted ? "Unmute  (m)" : "Mute  (m)"
              foreground: root.bar.foreground
              onKeyRequested: function(k) { root.sendKey(k) }
            }

            RemoteButton {
              keyName: "VOLUME_DOWN"
              enabled: root.remote.powered
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              iconText: "󰝞"
              tooltipText: "Volume down  (−)"
              foreground: root.bar.foreground
              onKeyRequested: function(k) { root.sendKey(k) }
            }

            RemoteButton {
              keyName: "VOLUME_UP"
              enabled: root.remote.powered
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              iconText: "󰝝"
              tooltipText: "Volume up  (+)"
              foreground: root.bar.foreground
              onKeyRequested: function(k) { root.sendKey(k) }
            }

            RemoteButton {
              keyName: "POWER"
              activeKey: root.lastKey
              activeSeq: root.lastKeySeq
              iconText: "󰐥"
              // POWER is a toggle - one button, not two.
              tooltipText: root.remote.powered ? "Turn off" : "Turn on"
              foreground: root.remote.powered ? root.bar.foreground : Color.accent
              onKeyRequested: function(k) { root.sendKey(k) }
            }
          }
        }

        // ---------- pairing ----------
        PairingView {
          id: pairingView
          onEscapeRequested: root.close()
          width: parent.width
          visible: !root.remote.live
          service: root.remote
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
        }

        // ---------- error ----------
        Text {
          textFormat: Text.PlainText
          visible: root.remote.lastError !== ""
          width: parent.width
          text: root.remote.lastError
          color: Color.urgent
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        Button {
          width: parent.width
          // Also when the process is alive but stuck - that is when the button is
          // needed most.
          visible: root.remote.failed
          text: "Restart the backend"
          iconText: "󰑐"
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          bordered: true
          leftAlign: true
          onClicked: root.remote.restart()
        }
      }
    }
  }
}
