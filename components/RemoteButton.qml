import QtQuick
import qs.Commons
import qs.Ui

// The shared remote button. One place decides the size, the tooltip and when
// the button is inactive.
//
// The protocol confirms neither the D-pad, nor BACK, nor play/pause (settled
// during the spike), so the flash on press is the only honest feedback: it
// says "sent", not "the device reacted". The panel bumps `activeSeq` on every
// command sent, which makes the same effect work for mouse and keyboard - and
// with the keyboard it doubles as a hint of the mapping.
PanelActionButton {
  id: root

  // A key name from the backend allowlist; empty means a local action.
  property string keyName: ""

  property string activeKey: ""
  property int activeSeq: 0

  signal keyRequested(string keyName)

  bordered: true
  focusable: false
  size: Style.space(32)
  fontSize: Style.font.iconLarge

  onClicked: if (root.keyName !== "") root.keyRequested(root.keyName)

  onActiveSeqChanged: {
    if (root.keyName !== "" && root.keyName === root.activeKey) pulseAnimation.restart()
  }

  Rectangle {
    id: pulseOverlay
    anchors.fill: parent
    radius: root.radius
    color: Color.accent
    opacity: 0
    // Below the glyph, above the button background.
    z: -1
  }

  NumberAnimation {
    id: pulseAnimation
    target: pulseOverlay
    property: "opacity"
    from: 0.5
    to: 0
    duration: 260
    easing.type: Easing.OutCubic
  }
}
