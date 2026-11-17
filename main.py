"""Music Video Bar — ilha dinâmica para o que está tocando no Windows.

    pythonw main.py               roda normalmente (ícone na bandeja)
    python  main.py --demo        roda com músicas falsas, para testar a interface
    python  main.py --snapshot D  salva PNGs de cada modo em D e sai
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from island import winutil
from island.ui import WIN_H, WIN_W, Island

HOTKEY_TEXT = "Ctrl+Alt+M"
PIPE_NAME = "MusicVideoBarIsland-" + os.environ.get("USERNAME", "user")


def poke_running_instance():
    """Já tem uma ilha rodando: pede para ela abrir a central (atalho da área de trabalho)."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    QCoreApplication(sys.argv)
    sock = QLocalSocket()
    sock.connectToServer(PIPE_NAME)
    if sock.waitForConnected(1000):
        sock.write(b"open")
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()


def listen_for_pokes(island):
    from PySide6.QtNetwork import QLocalServer
    QLocalServer.removeServer(PIPE_NAME)
    server = QLocalServer(island)

    def on_conn():
        c = server.nextPendingConnection()
        if c is not None:
            island.set_expanded(True)
            c.disconnectFromServer()

    server.newConnection.connect(on_conn)
    server.listen(PIPE_NAME)
    return server


def setup_logging():
    d = os.path.join(os.environ.get("LOCALAPPDATA", "."), "MusicVideoBar")
    os.makedirs(d, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(os.path.join(d, "log.txt"), encoding="utf-8")]
        + ([logging.StreamHandler()] if sys.stderr else []),
    )
    return d


def icon_pixmap(size=64) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64, size / 64)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(20, 20, 24))
    p.drawRoundedRect(QRectF(4, 18, 56, 28), 14, 14)
    p.setBrush(QColor(255, 255, 255, 60))
    p.drawRoundedRect(QRectF(4, 18, 56, 28), 14, 14)
    p.setBrush(QColor(20, 20, 24))
    p.drawRoundedRect(QRectF(6, 20, 52, 24), 12, 12)
    p.setBrush(QColor(120, 220, 160))
    for i, h in enumerate((8, 14, 10)):
        p.drawRoundedRect(QRectF(38 + i * 6, 32 - h / 2, 3.5, h), 1.7, 1.7)
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(QPointF(18, 32), 6, 6)
    p.end()
    return pm


def tray_icon() -> QIcon:
    icon = QIcon()
    for sz in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(icon_pixmap(sz))
    return icon


def build_menu(island: Island, settings: QSettings, app: QApplication, hotkey_ok: bool) -> QMenu:
    m = QMenu()
    m.setStyleSheet("""
        QMenu { background:#141418; color:#eee; border:1px solid #2a2a30; border-radius:10px; padding:6px; }
        QMenu::item { padding:7px 22px 7px 28px; border-radius:6px; }
        QMenu::item:selected { background:#2a2a33; }
        QMenu::item:disabled { color:#777; }
        QMenu::separator { height:1px; background:#2a2a30; margin:5px 8px; }
        QMenu::indicator { width:14px; height:14px; left:8px; }
    """)
    title = m.addAction("Music Video Bar")
    title.setEnabled(False)
    m.addSeparator()

    def toggle(label, key, default=True):
        a = QAction(label, m, checkable=True)
        a.setChecked(bool(settings.value(key, default, type=bool)))
        a.toggled.connect(lambda v: settings.setValue(key, v))
        m.addAction(a)

    toggle("Expandir ao passar o mouse", "hover_expand")
    toggle("Modo discreto sobre janelas maximizadas", "discreet_maximized")
    toggle("Esconder quando nada toca", "hide_when_idle")
    toggle("Esconder em tela cheia (jogos, vídeos)", "hide_fullscreen")
    toggle("Mostrar HUD de volume", "volume_hud")
    toggle("Mostrar aviso ao trocar de faixa", "peek_on_change")
    m.addSeparator()

    auto = QAction("Iniciar com o Windows", m, checkable=True)
    auto.setChecked(winutil.autostart_enabled())
    auto.toggled.connect(winutil.set_autostart)
    m.addAction(auto)
    hk = m.addAction(f"Abrir/fechar: {HOTKEY_TEXT}" + ("" if hotkey_ok else " (em uso por outro app)"))
    hk.setEnabled(False)
    m.addSeparator()
    m.addAction("Sair", app.quit)
    return m


def snapshot(island: Island, backend, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    bg = QImage(WIN_W, WIN_H, QImage.Format_ARGB32)
    p = QPainter(bg)
    g = QLinearGradient(0, 0, WIN_W, WIN_H)
    g.setColorAt(0, QColor("#3a6186"))
    g.setColorAt(1, QColor("#89253e"))
    p.fillRect(bg.rect(), g)
    p.end()
    import math
    import random
    island.on_state(backend.state())
    for i in range(6):
        island.update_bars(0.5 + i * 0.05, 0.016)
    rnd = random.Random(7)
    island.wave.extend(0.25 + 0.6 * abs(math.sin(i * 0.7)) * rnd.uniform(0.6, 1) for i in range(30))
    t = island.track
    played = int(t.position_now() / t.duration * 84) + 1
    island.tl_waves[t.key] = [(0.3 + 0.6 * abs(math.sin(i * 0.31)) * rnd.uniform(0.5, 1)) if i < played else 0.0
                              for i in range(84)]
    shots = ["idle", "sliver", "compact", "volume", "peek", "expanded"]
    for mode in shots:
        island.render_mode(mode, bg).save(os.path.join(out_dir, f"{mode}.png"))
    island.track.playing = False
    island.render_mode("compact", bg).save(os.path.join(out_dir, "compact_paused.png"))
    backend.select("chrome")
    island.on_state(backend.state())
    island.render_mode("expanded", bg).save(os.path.join(out_dir, "expanded_video.png"))
    backend.empty = True
    island.on_state(backend.state())
    island.render_mode("expanded", bg).save(os.path.join(out_dir, "expanded_empty.png"))
    print("snapshots em", out_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--snapshot")
    ap.add_argument("--make-icon", help="salva o ícone .ico neste caminho e sai")
    args = ap.parse_args()

    logdir = setup_logging()
    log = logging.getLogger("main")

    if args.make_icon:
        app = QApplication(sys.argv)
        os.makedirs(os.path.dirname(args.make_icon), exist_ok=True)
        pm = icon_pixmap(256)
        ok = pm.save(args.make_icon, "ICO")
        print("ícone salvo" if ok else "falhou", args.make_icon)
        return 0 if ok else 1

    if not args.snapshot and not winutil.claim_single_instance():
        log.info("já existe uma instância rodando; abrindo a central dela")
        poke_running_instance()
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Music Video Bar")
    settings = QSettings("MusicVideoBar", "Island")

    if args.demo or args.snapshot:
        from island.audio import DemoAudio
        from island.demo import DemoBackend
        backend, audio = DemoBackend(), DemoAudio()
    else:
        from island.audio import Audio
        from island.media import MediaBackend
        backend, audio = MediaBackend(), Audio()

    island = Island(backend, audio, settings)

    if args.snapshot:
        snapshot(island, backend, args.snapshot)
        return 0

    hotkey = winutil.HotkeyThread(winutil.MOD_CONTROL | winutil.MOD_ALT, ord("M"),
                                  lambda: island.hotkey_pressed.emit())
    hotkey.start()
    hotkey.ok.wait(1.0)

    menu = build_menu(island, settings, app, not hotkey.failed)
    island.settings_menu_requested.connect(lambda pt: menu.popup(pt))
    tray = QSystemTrayIcon(tray_icon(), app)
    tray.setToolTip(f"Music Video Bar — {HOTKEY_TEXT} abre a central")
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: island.toggle_expanded() if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    island.show()
    pokes = listen_for_pokes(island)  # noqa: F841 (precisa ficar vivo)
    backend.start()
    log.info("iniciado (demo=%s). logs em %s", args.demo, logdir)

    rc = app.exec()
    backend.stop()
    hotkey.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
