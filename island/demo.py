"""Backend falso para testar a ilha sem nada tocando (e para gerar snapshots)."""
from __future__ import annotations

import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPointF, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient

from .media import MediaState, Track


def fake_cover(c1, c2, w=300, h=300) -> bytes:
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor(c1))
    g.setColorAt(1, QColor(c2))
    p.fillRect(img.rect(), g)
    rg = QRadialGradient(QPointF(w * 0.7, h * 0.3), w * 0.5)
    rg.setColorAt(0, QColor(255, 255, 255, 120))
    rg.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(img.rect(), rg)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba.data())


class DemoBackend(QObject):
    changed = Signal(object)

    def __init__(self):
        super().__init__()
        now = time.monotonic()
        self.tracks = [
            Track(sid="spotify", aumid="Spotify.exe", app="Spotify", title="Noite Longa (Ao Vivo no Estúdio)",
                  artist="Banda Exemplo", playing=True, position=42, duration=214, stamp=now, can_seek=True,
                  can_prev=True, can_next=True, shuffle=False, repeat=0,
                  thumb=fake_cover("#ff5f6d", "#5b247a")),
            Track(sid="chrome", aumid="Chrome", app="Chrome", title="Como fazer pão em casa — receita completa",
                  artist="Canal de Culinária", playing=False, position=310, duration=1260, stamp=now,
                  can_seek=True, can_prev=False, can_next=True,
                  thumb=fake_cover("#00c9ff", "#1a3a5c", 480, 270)),
        ]
        self.active = 0
        self.empty = False
        self._t = QTimer(self)
        self._t.timeout.connect(self.emit)

    def start(self):
        self._t.start(400)
        self.emit()

    def stop(self):
        self._t.stop()

    def state(self) -> MediaState:
        if self.empty:
            return MediaState([], None)
        return MediaState(list(self.tracks), self.tracks[self.active])

    def emit(self):
        self.changed.emit(self.state())

    def select(self, sid):
        self.active = next((i for i, t in enumerate(self.tracks) if t.sid == sid), self.active)
        self.emit()

    def command(self, name, sid, *args):
        t = next((t for t in self.tracks if t.sid == sid), None)
        if not t:
            return
        now = time.monotonic()
        t.position, t.stamp = t.position_now(now), now
        if name == "toggle":
            t.playing = not t.playing
        elif name == "seek":
            t.position = args[0]
        elif name in ("next", "prev"):
            t.position = 0
            t.title = ("Próxima Faixa" if name == "next" else "Faixa Anterior") + f" #{int(now) % 100}"
        elif name == "shuffle":
            t.shuffle = bool(args[0])
        elif name == "repeat":
            t.repeat = int(args[0])
        self.emit()
