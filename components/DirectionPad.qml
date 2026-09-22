pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import "."

// The direction cross with OK in the middle. It knows key names only, never
// the protocol.
//
// The empty cells must be exactly the size of the buttons: Grid sizes a column
// to its widest item and aligns content to the left, so a mismatch of even a
// few pixels shifts the whole cross sideways.
Item {
  id: root

  property color foreground: Color.foreground
  property string fontFamily: Style.font.family

  property real buttonSize: Style.space(32)
  property real gap: Style.space(4)

  // Passed down to the buttons so the flash works for the keyboard too.
  property string activeKey: ""
  property int activeSeq: 0

  signal keyRequested(string keyName)

  implicitWidth: buttonSize * 3 + gap * 2
  implicitHeight: buttonSize * 3 + gap * 2

  component Spacer: Item {
    width: root.buttonSize
    height: root.buttonSize
  }

  // `enabled` is not redeclared here: Item.enabled already propagates to the
  // children, and shadowing it would leave the base value stuck at true.
  component PadButton: RemoteButton {
    size: root.buttonSize
    foreground: root.foreground
    activeKey: root.activeKey
    activeSeq: root.activeSeq
    onKeyRequested: function(k) { root.keyRequested(k) }
  }

  Grid {
    anchors.centerIn: parent
    columns: 3
    spacing: root.gap

    Spacer {}

    PadButton {
      keyName: "DPAD_UP"
      iconText: "󰅃"
      tooltipText: "Up  (↑ / k)"
    }

    Spacer {}

    PadButton {
      keyName: "DPAD_LEFT"
      iconText: "󰅁"
      tooltipText: "Left  (← / h)"
    }

    PadButton {
      keyName: "DPAD_CENTER"
      iconText: "󰄯"
      tooltipText: "OK  (Enter)"
    }

    PadButton {
      keyName: "DPAD_RIGHT"
      iconText: "󰅂"
      tooltipText: "Right  (→ / l)"
    }

    Spacer {}

    PadButton {
      keyName: "DPAD_DOWN"
      iconText: "󰅀"
      tooltipText: "Down  (↓ / j)"
    }

    Spacer {}
  }
}
