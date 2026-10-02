"""Same-user project-window activation on native Ubuntu desktops."""
import os

from PySide6.QtNetwork import QLocalServer


def install_activation_server(window):
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    name = f"mcu-flasher-project-{os.getpid()}"
    QLocalServer.removeServer(name)

    def activate():
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.disconnectFromServer()
            connection.deleteLater()
            if window.isMinimized():
                window.showNormal()
            window.raise_()
            window.activateWindow()

    server.newConnection.connect(activate)
    if not server.listen(name):
        server.deleteLater()
        return None
    return server
