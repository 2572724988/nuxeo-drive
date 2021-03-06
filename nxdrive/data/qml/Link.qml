import QtQuick 2.13

ScaledText {
    id: control

    signal clicked()

    color: nuxeoBlue

    horizontalAlignment: Text.AlignLeft
    verticalAlignment: Text.AlignVCenter

    MouseArea {
        id: linkArea
        width: parent.width * 3/2
        height: parent.height * 3/2
        anchors.centerIn: parent
        onClicked: control.clicked()
        cursorShape: Qt.PointingHandCursor
    }
}
