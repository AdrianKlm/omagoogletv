pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons

// A segmented volume meter.
//
// The segment count comes from the device (a Chromecast reports 25) rather
// than from the code: other hardware may use a different scale. The meter is
// read-only - the protocol cannot set a volume directly, only step it.
Item {
  id: root

  property int value: 0
  property int maximum: 0
  property bool muted: false
  property color foreground: Color.foreground

  readonly property int segments: Math.max(1, maximum)
  // A narrow gap: with 25 segments a wider one starts to dominate them.
  readonly property real gap: Style.space(1)
  readonly property real segmentWidth:
    Math.max(1, (width - gap * (segments - 1)) / segments)

  visible: maximum > 0
  // Deliberately short: a segment wider than it is tall reads as a level
  // rather than as a row of beads.
  implicitHeight: Style.space(6)

  Row {
    anchors.fill: parent
    spacing: root.gap

    Repeater {
      model: root.segments

      Rectangle {
        required property int index

        width: root.segmentWidth
        height: parent.height
        radius: Math.min(width, height) / 2
        color: root.foreground
        opacity: root.muted ? 0.12 : (index < root.value ? 0.95 : 0.18)

        Behavior on opacity { NumberAnimation { duration: 140 } }
      }
    }
  }
}
