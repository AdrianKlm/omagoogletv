pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import qs.Ui

// Device selection and the PIN form. Shown while there is no connection.
//
// Handles mouse and keyboard alike: the panel forwards the arrows and Enter
// here whenever the remote is inactive, so the whole first-run path can be
// walked without a mouse.
Column {
  id: root

  property var service: null
  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  // The panel needs to know when the PIN field has focus, so it stops
  // capturing letters.
  readonly property bool editing: pinField.activeFocus

  // The panel always sets `service`, but bindings also evaluate before that
  // assignment - without a guard the first pass ends in an error.
  readonly property bool canPair:
    !!service && !service.pairing && service.selectedHost !== ""
  readonly property int actionCount:
    service ? 1 + service.devices.length + (canPair ? 1 : 0) : 0

  // While the PIN is being typed the panel hands keys to the field, so Escape
  // has to come back from here - otherwise the panel cannot be left by
  // keyboard.
  signal escapeRequested()

  // -1 means "no cursor": until the user presses an arrow nothing is
  // highlighted and Enter triggers no accidental action.
  property int cursor: -1

  spacing: Style.space(6)

  function moveCursor(delta) {
    if (actionCount === 0) return
    if (cursor < 0 || cursor >= actionCount) {
      cursor = delta > 0 ? 0 : actionCount - 1
      return
    }
    cursor = (cursor + delta + actionCount) % actionCount
  }

  function activate() {
    // The device list shrinks on a re-scan; a cursor pointing past it must
    // not land on the pairing button that nobody selected.
    if (cursor < 0 || cursor >= actionCount) return
    if (cursor === 0) { service.discover(); return }
    var deviceIndex = cursor - 1
    if (deviceIndex < service.devices.length) {
      service.selectDevice(service.devices[deviceIndex].host)
      return
    }
    if (canPair) service.pairStart()
  }

  function resetCursor() { cursor = -1 }

  function submitPin() {
    if (pinField.text.length !== 6) return
    service.pairFinish(pinField.text)
    pinField.text = ""
  }

  // Once the backend enters pairing, focus should go straight to the PIN field.
  Connections {
    target: root.service

    function onRemoteStateChanged() {
      if (!root.service.pairing) return
      root.resetCursor()
      // The field is still invisible until the `visible` binding is
      // re-evaluated, and Qt refuses focus to an invisible item.
      Qt.callLater(function() {
        if (root.service.pairing) pinField.forceActiveFocus()
      })
    }

    // After a re-scan the list differs - the old cursor means nothing.
    function onDevicesChanged() { root.resetCursor() }
  }

  Button {
    width: parent.width
    text: root.service.discovering ? "Searching…" : "Search for devices"
    iconText: "󰍉"
    tooltipText: "Discover Chromecasts over mDNS"
    enabled: !root.service.discovering && root.service.helperRunning
    hasCursor: root.cursor === 0
    foreground: root.foreground
    fontFamily: root.fontFamily
    bordered: true
    leftAlign: true
    onClicked: root.service.discover()
  }

  Repeater {
    model: root.service.devices

    Button {
      required property var modelData
      required property int index

      width: root.width
      // The mDNS name is often generic ("Chromecast"), so the IP is part of
      // the label - otherwise two devices would look identical.
      text: (modelData.name && modelData.name !== "" ? modelData.name : "Chromecast")
        + "  ·  " + modelData.host
      iconText: root.service.selectedHost === modelData.host ? "󰄬" : "󰠹"
      tooltipText: "Connect to " + modelData.host
      selected: root.service.selectedHost === modelData.host
      hasCursor: root.cursor === index + 1
      foreground: root.foreground
      fontFamily: root.fontFamily
      bordered: true
      leftAlign: true
      onClicked: root.service.selectDevice(modelData.host)
    }
  }

  Text {
    width: parent.width
    visible: !root.service.discovering && root.service.devices.length === 0 && !root.service.pairing
    text: "No devices found. Check that the Chromecast is on the same network."
    color: Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.Wrap
  }

  Button {
    width: parent.width
    visible: root.canPair
    text: "Pair this device"
    iconText: "󰌆"
    tooltipText: "The PIN will appear on the TV"
    hasCursor: root.cursor === root.actionCount - 1 && root.cursor > 0
    foreground: root.foreground
    fontFamily: root.fontFamily
    bordered: true
    leftAlign: true
    onClicked: root.service.pairStart()
  }

  Column {
    width: parent.width
    visible: root.service.pairing
    spacing: Style.space(6)

    Text {
      width: parent.width
      text: "Enter the 6-character code from the TV"
      color: Qt.darker(root.foreground, 1.3)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.Wrap
    }

    TextField {
      id: pinField
      width: parent.width
      placeholderText: "e.g. 9E76AB"
      foreground: root.foreground
      // The PIN is alphanumeric, not digits only - settled during the spike.
      validator: RegularExpressionValidator { regularExpression: /[A-Za-z0-9]{0,6}/ }
      onAccepted: root.submitPin()
      Keys.onEscapePressed: function(event) {
        root.escapeRequested()
        event.accepted = true
      }
    }

    Button {
      width: parent.width
      text: "Confirm code"
      iconText: "󰄬"
      enabled: pinField.text.length === 6
      foreground: root.foreground
      fontFamily: root.fontFamily
      bordered: true
      leftAlign: true
      onClicked: root.submitPin()
    }
  }
}
