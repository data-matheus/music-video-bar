"""A ilha: janela sem borda no topo da tela, desenhada à mão com QPainter.

Modos (do menor ao maior):
  hidden    nada tocando: invisível, mas encostar o mouse no topo-centro abre
  idle      nada tocando com "esconder" desligado: pílula mínima
  compact   tocando: capa + título + barrinhas reagindo ao som
  volume    HUD de volume (aparece quando o volume muda por qualquer lugar)
  peek      troca de faixa: mostra capa grande + título por alguns segundos
  expanded  central de controle: capa, timeline arrastável, controles, volume, fontes
"""
from __future__ import annotations

import math
import random
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import winutil
from .media import MediaState, Track, process_hint

WIN_W, WIN_H = 540, 300
TOP = 8

COMPACT = (272, 36)
WAVE_N = 18          # amostras da onda rolando na pílula
TL_BUCKETS = 84      # colunas da timeline-onda (estilo SoundCloud)
IDLE = (120, 26)
HIDDEN = (140, 22)
SLIVER = (210, 12)    # modo discreto: linha de progresso sobre janelas maximizadas
SLIVER_TOP = 2
VOLUME = (290, 40)
PEEK = (390, 70)
EXP_W = 440

BG = QColor(6, 6, 8, 248)
TXT = QColor(255, 255, 255, 240)
TXT2 = QColor(255, 255, 255, 140)
TXT3 = QColor(255, 255, 255, 90)

G_SHUFFLE, G_REPEAT, G_REPEAT1 = "", "", ""
G_VOL = ["", "", "", ""]
G_MUTE, G_NOTE = "", ""


def fmt_time(s: float) -> str:
    s = int(max(0, s))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def accent_from(img: QImage) -> QColor:
    """Cor de destaque da capa: média ponderada por saturação×brilho."""
    small = img.scaled(28, 28, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
    rs = gs = bs = wt = 0.0
    for y in range(small.height()):
        for x in range(small.width()):
            c = QColor(small.pixel(x, y))
            h, s, v, _ = c.getHsvF()
            w = (s * v) ** 2 + 0.002
            rs += c.redF() * w
            gs += c.greenF() * w
            bs += c.blueF() * w
            wt += w
    c = QColor.fromRgbF(rs / wt, gs / wt, bs / wt)
    h, s, v, _ = c.getHsvF()
    if s < 0.12:
        return QColor(235, 235, 240)
    return QColor.fromHsvF(max(0.0, h), min(0.85, max(0.45, s)), max(0.88, v))


class Spring:
    """Mola amortecida (ζ≈0.75): dá o "quique" estilo Dynamic Island."""

    def __init__(self, x, k=230.0, d=23.0):
        self.x = self.target = float(x)
        self.v = 0.0
        self.k, self.d = k, d

    def snap(self, x=None):
        self.x = self.target = float(self.target if x is None else x)
        self.v = 0.0

    def step(self, dt) -> bool:
        if self.x == self.target and self.v == 0.0:
            return False
        n = max(1, int(dt / 0.004))
        h = dt / n
        for _ in range(n):
            self.v += (self.k * (self.target - self.x) - self.d * self.v) * h
            self.x += self.v * h
        if abs(self.target - self.x) < 0.05 and abs(self.v) < 0.5:
            self.snap()
        return True


def approach(cur, target, rate, dt):
    return target + (cur - target) * math.exp(-rate * dt)


class Art:
    def __init__(self, key, data: bytes):
        self.key = key
        img = QImage.fromData(data)
        self.ok = not img.isNull()
        self.img = img
        self.wide = self.ok and img.width() / max(1, img.height()) > 1.25
        self.accent = accent_from(img) if self.ok else QColor(235, 235, 240)
        self._cache = {}

    def pix(self, w, h, dpr):
        k = (int(w), int(h), dpr)
        if k not in self._cache:
            p = QPixmap.fromImage(self.img.scaled(int(w * dpr), int(h * dpr), Qt.KeepAspectRatioByExpanding,
                                                  Qt.SmoothTransformation))
            p.setDevicePixelRatio(dpr)
            self._cache[k] = p
        return self._cache[k]


def _font(px, weight=QFont.Normal, families=("Segoe UI Variable Display", "Segoe UI")):
    f = QFont()
    f.setFamilies(list(families))
    f.setPixelSize(int(px))
    f.setWeight(weight)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


class Island(QWidget):
    settings_menu_requested = Signal(object)  # QPoint global
    hotkey_pressed = Signal()  # emitido de outra thread; entregue na thread da UI

    def __init__(self, backend, audio, settings):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Music Video Bar")
        self.resize(WIN_W, WIN_H)

        self.backend, self.audio, self.settings = backend, audio, settings
        backend.changed.connect(self.on_state)
        self.hotkey_pressed.connect(self.toggle_expanded)

        self.state = MediaState([], None)
        self.track: Track | None = None
        self.art: Art | None = None
        self.accent = QColor(235, 235, 240)
        self.accent_target = QColor(self.accent)

        self.w, self.h = Spring(HIDDEN[0]), Spring(HIDDEN[1])
        self.top = Spring(TOP)
        self.alpha = 0.0
        self.layer = {m: 0.0 for m in ("idle", "sliver", "compact", "volume", "peek", "expanded", "empty")}
        self.mode = "hidden"
        self.force_mode = None  # usado pelos snapshots

        now = time.monotonic()
        self.expanded = False
        self.opened_at = 0.0
        self.was_hovered = False
        self.hover_start = None
        self.last_hover = 0.0
        self.peek_until = 0.0
        self.vol_hud_until = 0.0
        self.last_playing = 0.0
        self.suppressed = False
        self.discreet = False  # janela maximizada embaixo da ilha
        self._last_fs = self._last_vol = self._last_top = 0.0
        self._last_tick = now

        self.vol = audio.volume() if audio else None
        self.muted = audio.muted() if audio else False
        self.level = 0.0
        self.bars = [0.1] * 5
        self._phase = [random.uniform(0, 6.28) for _ in range(5)]
        self._speed = [random.uniform(5.0, 9.0) for _ in range(5)]
        self.silent_for = 0.0
        self.wave = deque([0.0] * WAVE_N, maxlen=WAVE_N)
        self._wave_t = 0.0
        self.tl_waves = {}  # chave da faixa -> [nível por coluna da timeline]

        self.hits = []
        self.mouse = QPointF(-1, -1)
        self.drag = None          # "seek" | "vol"
        self.drag_value = 0.0
        self.pressed = (None, 0.0)
        self.seek_override = None  # (pos, stamp, until)
        self.play_override = None  # (playing, until)
        self.title_t0 = now
        self.click_through = None

        self.f_title = _font(16, QFont.DemiBold)
        self.f_sub = _font(13)
        self.f_small = _font(11, QFont.DemiBold)
        self.f_time = _font(11)
        self.f_compact = _font(12.5, QFont.Medium)
        self.f_peek_t = _font(14.5, QFont.DemiBold)
        self.f_peek_s = _font(12.5)
        self.f_chip = _font(11.5, QFont.Medium)
        self.f_icon = _font(15, families=("Segoe Fluent Icons", "Segoe MDL2 Assets"))
        self.f_icon_s = _font(13, families=("Segoe Fluent Icons", "Segoe MDL2 Assets"))
        self.f_icon_l = _font(22, families=("Segoe Fluent Icons", "Segoe MDL2 Assets"))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)
        self._watched_screen = None
        self.place()
        QGuiApplication.instance().primaryScreenChanged.connect(lambda *_: self.place())

    # ------------------------------------------------------------ posição
    def place(self):
        scr = QGuiApplication.primaryScreen()
        g = scr.geometry()
        self.move(g.center().x() - WIN_W // 2, g.top())
        if scr is not self._watched_screen:
            self._watched_screen = scr
            scr.geometryChanged.connect(lambda *_: self.place())

    def pill(self) -> QRectF:
        w, h = max(1.0, self.w.x), max(1.0, self.h.x)
        return QRectF((WIN_W - w) / 2, self.top.x, w, h)

    def setting(self, key, default=True) -> bool:
        return bool(self.settings.value(key, default, type=bool)) if self.settings else default

    # ------------------------------------------------------------ dados
    def on_state(self, st: MediaState):
        prev = self.track
        self.state = st
        t = st.active
        now = time.monotonic()
        if self.play_override and now < self.play_override[1] and t:
            t.playing = self.play_override[0]
        if t and t.playing:
            self.last_playing = now
        changed = (prev is None) != (t is None) or (t and prev and t.key != prev.key)
        if changed:
            self.title_t0 = now
            self.seek_override = None
            if t and t.title and prev is not None and self.setting("peek_on_change") and not self.expanded:
                self.peek_until = now + 3.5
        if t and t.thumb and (self.art is None or self.art.key != (t.key, len(t.thumb))):
            art = Art((t.key, len(t.thumb)), t.thumb)
            self.art = art if art.ok else None
        elif t and not t.thumb and changed:
            self.art = None
        if t is None:
            self.art = None
        self.accent_target = self.art.accent if self.art else QColor(235, 235, 240)
        self.track = t
        self.update()

    def position(self) -> float:
        t = self.track
        if not t:
            return 0.0
        now = time.monotonic()
        if self.drag == "seek":
            return self.drag_value
        if self.seek_override and now < self.seek_override[2]:
            pos, stamp, _ = self.seek_override
            if t.playing:
                pos += now - stamp
            return min(pos, t.duration) if t.duration else pos
        return t.position_now(now)

    # ------------------------------------------------------------ modo
    def target_mode(self, now) -> str:
        if self.force_mode:
            return self.force_mode
        if self.suppressed:
            return "hidden"
        if self.expanded:
            return "expanded"
        if now < self.vol_hud_until:
            return "volume"
        t = self.track
        if t and now < self.peek_until:
            return "peek"
        if t and (t.playing or now - self.last_playing < 25):
            return "sliver" if self.discreet else "compact"
        if self.setting("hide_when_idle"):
            return "hidden"
        return "sliver" if self.discreet else "idle"

    def expanded_height(self) -> float:
        if not self.track:
            return 146
        h = 246
        if self.track.duration <= 0:
            h -= 26
        if len(self.state.sessions) > 1:
            h += 36
        return h

    def dims(self, mode):
        return {
            "hidden": HIDDEN, "idle": IDLE, "sliver": SLIVER, "compact": COMPACT, "volume": VOLUME, "peek": PEEK,
            "expanded": (EXP_W, self.expanded_height()),
        }[mode]

    def set_expanded(self, on: bool):
        if on == self.expanded:
            return
        self.expanded = on
        self.opened_at = time.monotonic()
        self.was_hovered = False
        if on:
            self.peek_until = self.vol_hud_until = 0.0

    def toggle_expanded(self):
        self.set_expanded(not self.expanded)

    # ------------------------------------------------------------ loop
    def tick(self):
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        hwnd = int(self.winId())

        if now - self._last_fs > 0.6:
            self._last_fs = now
            lay = winutil.foreground_layout(hwnd)
            if lay is not None:
                fs = lay[0] and self.setting("hide_fullscreen")
                if fs and not self.suppressed and self.expanded:
                    self.set_expanded(False)
                self.suppressed = fs
                self.discreet = lay[1] and not lay[0] and self.setting("discreet_maximized")
        if now - self._last_top > 4 and self.alpha > 0:
            self._last_top = now
            winutil.reassert_topmost(hwnd)
        if self.audio and now - self._last_vol > 0.15 and self.drag != "vol":
            self._last_vol = now
            v, m = self.audio.volume(), self.audio.muted()
            if self.vol is not None and v is not None and (abs(v - self.vol) > 0.004 or m != self.muted):
                if self.setting("volume_hud") and not self.expanded and not self.suppressed:
                    self.vol_hud_until = now + 1.6
            self.vol, self.muted = v, m

        self.poll_hover(now)

        mode = self.target_mode(now)
        self.mode = mode
        tw, th = self.dims(mode)
        self.w.target, self.h.target = tw, th
        self.top.target = SLIVER_TOP if mode == "sliver" else TOP
        moving = self.w.step(dt) | self.h.step(dt) | self.top.step(dt)

        vis = 0.0 if mode == "hidden" else 1.0
        self.alpha = approach(self.alpha, vis, 14 if vis else 10, dt)
        if abs(self.alpha - vis) < 0.003:
            self.alpha = vis
        else:
            moving = True

        layer_mode = "empty" if (mode == "expanded" and not self.track) else mode
        settled = abs(self.h.x - th) < th * 0.25 and abs(self.w.x - tw) < tw * 0.25
        for k in self.layer:
            target = 1.0 if (k == layer_mode and (settled or k in ("compact", "idle", "sliver"))) else 0.0
            rate = 16 if target else 26
            nv = approach(self.layer[k], target, rate, dt)
            if abs(nv - target) < 0.004:
                nv = target
            if nv != self.layer[k]:
                moving = True
            self.layer[k] = nv

        a = self.accent
        b = self.accent_target
        if a != b:
            f = 1 - math.exp(-6 * dt)
            na = QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * f, a.greenF() + (b.greenF() - a.greenF()) * f,
                                 a.blueF() + (b.blueF() - a.blueF()) * f)
            if abs(na.redF() - b.redF()) + abs(na.greenF() - b.greenF()) + abs(na.blueF() - b.blueF()) < 0.01:
                na = QColor(b)
            self.accent = na
            moving = True

        playing = bool(self.track and self.track.playing)
        live = self.alpha > 0 and playing
        if live:
            self.update_bars(now, dt)

        # Discreto: nada que só "mostra" (linha, aviso, HUD) rouba clique das abas.
        ct = self.alpha == 0 or (self.discreet and mode in ("sliver", "peek", "volume"))
        if ct != self.click_through:
            self.click_through = ct
            winutil.set_click_through(hwnd, ct)

        if moving or live or self.drag:
            self.update()
            self.timer.setInterval(16 if (moving or self.drag) else 33)
        else:
            self.timer.setInterval(16 if self.hover_start else 90)

    def update_bars(self, now, dt):
        pk = self.audio.peak() if self.audio else None
        if pk is None:
            lvl = 0.55
        else:
            lvl = min(1.0, (pk ** 0.6) * 1.25)
            self.silent_for = self.silent_for + dt if pk < 0.002 else 0.0
            if self.silent_for > 0.6:
                lvl = 0.12  # aba muda / volume do app zerado: respira devagar
        self.level += (lvl - self.level) * (0.55 if lvl > self.level else 0.12)
        if now - self._wave_t >= 0.05:
            self._wave_t = now
            raw = lvl if pk is not None else 0.0
            self.wave.append(raw)
            t = self.track
            if t and t.duration > 0 and raw > 0.13 and self.drag != "seek":
                buckets = self.tl_waves.get(t.key)
                if buckets is None:
                    buckets = self.tl_waves[t.key] = [0.0] * TL_BUCKETS
                    if len(self.tl_waves) > 40:
                        self.tl_waves.pop(next(iter(self.tl_waves)))
                i = min(TL_BUCKETS - 1, int(self.position() / t.duration * TL_BUCKETS))
                buckets[i] = max(buckets[i], raw)
        for i in range(len(self.bars)):
            wob = 0.35 + 0.65 * abs(math.sin(now * self._speed[i] + self._phase[i]))
            tgt = max(0.12, min(1.0, self.level * wob * 1.15))
            self.bars[i] += (tgt - self.bars[i]) * 0.35

    def poll_hover(self, now):
        g = QCursor.pos()
        local = QPointF(self.mapFromGlobal(g))
        scr = QGuiApplication.primaryScreen().geometry()
        edge = abs(g.x() - scr.center().x()) < 160 and g.y() <= scr.top() + 3
        if self.mode in ("sliver", "peek", "volume") and self.discreet:
            # Sobre o navegador só a borda extrema do topo abre — passar o
            # mouse pelas abas não dispara nada.
            over, hot = False, edge
        else:
            p = self.pill().adjusted(-6, -self.top.x, 6, 6)
            over = self.alpha > 0.3 and p.contains(local)
            hot = self.mode in ("hidden", "idle") and not self.suppressed and edge
        hover = over or hot
        if hover:
            self.last_hover = now
            if self.hover_start is None:
                self.hover_start = now
            if self.expanded:
                self.was_hovered = True
        else:
            self.hover_start = None

        delay = 0.4 if hot and not over else 0.2
        if (not self.expanded and hover and self.setting("hover_expand") and not self.suppressed
                and now - self.hover_start > delay):
            self.set_expanded(True)
            self.was_hovered = True
        if self.expanded and not self.drag and not self.force_mode:
            if self.was_hovered and not hover and now - self.last_hover > 0.45:
                self.set_expanded(False)
            elif not self.was_hovered and now - self.opened_at > 5:
                self.set_expanded(False)

    # ------------------------------------------------------------ desenho
    def paintEvent(self, _):
        if self.alpha <= 0:
            return
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        p.setOpacity(self.alpha)
        r = self.pill()
        rad = min(r.height() / 2, 30.0)
        path = QPainterPath()
        path.addRoundedRect(r, rad, rad)
        p.fillPath(path, BG)

        ex = self.layer["expanded"] + self.layer["empty"]
        if ex > 0 and self.track and self.art:
            g = QRadialGradient(QPointF(r.left() + 60, r.top() + 40), 260)
            c = QColor(self.accent)
            c.setAlpha(int(46 * ex))
            g.setColorAt(0, c)
            c2 = QColor(self.accent)
            c2.setAlpha(0)
            g.setColorAt(1, c2)
            p.fillPath(path, g)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        p.drawPath(path)

        p.setClipPath(path)
        self.hits = []
        cx = r.center().x()
        for name, fn in (("idle", self.draw_idle), ("sliver", self.draw_sliver), ("compact", self.draw_compact), ("volume", self.draw_volume),
                         ("peek", self.draw_peek), ("expanded", self.draw_expanded), ("empty", self.draw_empty)):
            op = self.layer[name]
            if op > 0.01:
                p.save()
                p.setOpacity(self.alpha * op)
                fn(p, r, cx, op > 0.5)
                p.restore()
        p.end()

    # -- peças reutilizáveis
    def draw_art(self, p, rect: QRectF, radius, icon_px=None):
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        if self.art:
            pix = self.art.pix(rect.width(), rect.height(), self.devicePixelRatioF())
            p.save()
            p.setClipPath(path, Qt.IntersectClip)
            pw, ph = pix.width() / pix.devicePixelRatio(), pix.height() / pix.devicePixelRatio()
            p.drawPixmap(QRectF(rect.center().x() - pw / 2, rect.center().y() - ph / 2, pw, ph), pix,
                         QRectF(pix.rect()))
            p.restore()
        else:
            g = QLinearGradient(rect.topLeft(), rect.bottomRight())
            g.setColorAt(0, QColor(60, 60, 70))
            g.setColorAt(1, QColor(28, 28, 34))
            p.fillPath(path, g)
            f = _font(icon_px or rect.height() * 0.45, families=("Segoe Fluent Icons", "Segoe MDL2 Assets"))
            p.setFont(f)
            p.setPen(TXT2)
            p.drawText(rect, Qt.AlignCenter, G_NOTE)

    def draw_bars(self, p, rect: QRectF):
        n = len(self.bars)
        bw = rect.width() / (n * 1.8 - 0.8)
        gap = bw * 0.8
        playing = bool(self.track and self.track.playing)
        c = QColor(self.accent)
        if not playing:
            c.setAlpha(120)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for i in range(n):
            v = self.bars[i] if playing else 0.12
            h = max(bw, rect.height() * v)
            x = rect.left() + i * (bw + gap)
            p.drawRoundedRect(QRectF(x, rect.center().y() - h / 2, bw, h), bw / 2, bw / 2)

    def draw_wave(self, p, rect: QRectF):
        """Onda espelhada rolando (som real, ~1 s de histórico)."""
        n = len(self.wave)
        step = rect.width() / n
        bw = max(1.6, step * 0.55)
        playing = bool(self.track and self.track.playing)
        c = QColor(self.accent)
        p.setPen(Qt.NoPen)
        cy = rect.center().y()
        for i, v in enumerate(self.wave):
            # some suavemente na ponta mais antiga (esquerda)
            fade = min(1.0, (i + 1) / 5)
            c.setAlphaF((0.35 + 0.65 * fade) if playing else 0.45)
            p.setBrush(c)
            h = max(bw, rect.height() * (v if playing else 0.0) * (0.55 + 0.45 * fade))
            x = rect.left() + i * step + (step - bw) / 2
            p.drawRoundedRect(QRectF(x, cy - h / 2, bw, h), bw / 2, bw / 2)

    def timeline_wave(self, p, rect: QRectF, frac, enabled=True):
        """Timeline estilo SoundCloud: a onda vai sendo desenhada conforme toca;
        o que ainda não tocou aparece como pontinhos (não inventamos onda)."""
        t = self.track
        buckets = self.tl_waves.get(t.key) if t else None
        frac = max(0.0, min(1.0, frac))
        hov = enabled and (self.hovered(rect.adjusted(0, -4, 0, 4)) or self.drag == "seek")
        n = TL_BUCKETS
        step = rect.width() / n
        bw = max(2.0, step * 0.58)
        cy = rect.center().y()
        played = QColor(self.accent)
        rest = QColor(255, 255, 255, 60)
        p.setPen(Qt.NoPen)
        for i in range(n):
            v = buckets[i] if buckets else 0.0
            h = max(bw, rect.height() * min(1.0, v) * 0.95)
            x = rect.left() + i * step + (step - bw) / 2
            p.setBrush(played if (i + 0.5) / n <= frac else rest)
            p.drawRoundedRect(QRectF(x, cy - h / 2, bw, h), bw / 2, bw / 2)
        if hov:
            x = rect.left() + rect.width() * frac
            p.setBrush(QColor(255, 255, 255))
            p.drawRoundedRect(QRectF(x - 1.5, rect.top() - 2, 3, rect.height() + 4), 1.5, 1.5)
        if enabled:
            self.hit("seek", rect.adjusted(-6, -4, 6, 4))

    def text(self, p, rect: QRectF, s, font, color, align=Qt.AlignLeft | Qt.AlignVCenter):
        p.setFont(font)
        p.setPen(color)
        fm = QFontMetricsF(font)
        p.drawText(rect, align, fm.elidedText(s or "", Qt.ElideRight, rect.width()))

    def marquee(self, p, rect: QRectF, s, font, color):
        fm = QFontMetricsF(font)
        tw = fm.horizontalAdvance(s)
        if tw <= rect.width():
            return self.text(p, rect, s, font, color)
        over = tw - rect.width() + 8
        speed = 32.0
        pause = 2.2
        period = pause * 2 + over / speed
        ph = (time.monotonic() - self.title_t0) % period
        off = 0 if ph < pause else min(over, (ph - pause) * speed)
        p.save()
        p.setClipRect(rect, Qt.IntersectClip)
        p.setFont(font)
        p.setPen(color)
        p.drawText(QRectF(rect.left() - off, rect.top(), tw + 4, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, s)
        p.restore()

    def hovered(self, rect: QRectF) -> bool:
        return rect.contains(self.mouse)

    def hit(self, name, rect, *arg):
        self.hits.append((name, QRectF(rect), arg))

    def glyph_button(self, p, name, center: QPointF, glyph, size=34, enabled=True, active=False, font=None):
        r = QRectF(center.x() - size / 2, center.y() - size / 2, size, size)
        if enabled:
            self.hit(name, r)
            if self.hovered(r):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 26))
                p.drawEllipse(r)
        scale = 0.88 if (self.pressed[0] == name and time.monotonic() - self.pressed[1] < 0.12) else 1.0
        p.save()
        p.translate(center)
        p.scale(scale, scale)
        p.translate(-center)
        col = QColor(self.accent) if active else (TXT if enabled else QColor(255, 255, 255, 55))
        if callable(glyph):
            glyph(p, center, col)
        else:
            p.setFont(font or self.f_icon)
            p.setPen(col)
            p.drawText(r, Qt.AlignCenter, glyph)
        p.restore()
        if active:
            p.setPen(Qt.NoPen)
            p.setBrush(self.accent)
            p.drawEllipse(QPointF(center.x(), center.y() + 12), 1.8, 1.8)

    @staticmethod
    def shape_play(p, c: QPointF, col, s=1.0):
        path = QPainterPath()
        path.moveTo(c.x() - 6 * s, c.y() - 8 * s)
        path.lineTo(c.x() + 9 * s, c.y())
        path.lineTo(c.x() - 6 * s, c.y() + 8 * s)
        path.closeSubpath()
        p.setPen(QPen(col, 2.2 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(col)
        p.drawPath(path)

    @staticmethod
    def shape_pause(p, c: QPointF, col, s=1.0):
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        for dx in (-5.5, 2.5):
            p.drawRoundedRect(QRectF(c.x() + dx * s, c.y() - 8 * s, 3.4 * s, 16 * s), 1.4 * s, 1.4 * s)

    @staticmethod
    def shape_skip(p, c: QPointF, col, forward=True):
        d = 1 if forward else -1
        p.setPen(QPen(col, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(col)
        for ox in (-6, 3):
            path = QPainterPath()
            x0 = c.x() + ox * d
            path.moveTo(x0 - 4 * d, c.y() - 6)
            path.lineTo(x0 + 5 * d, c.y())
            path.lineTo(x0 - 4 * d, c.y() + 6)
            path.closeSubpath()
            p.drawPath(path)

    def vol_glyph(self):
        if self.muted or (self.vol is not None and self.vol <= 0.001):
            return G_MUTE
        v = self.vol or 0
        return G_VOL[0 if v < 0.01 else 1 if v < 0.34 else 2 if v < 0.67 else 3]

    def slider(self, p, name, rect: QRectF, frac, color, enabled=True):
        hov = enabled and (self.hovered(rect.adjusted(0, -8, 0, 8)) or self.drag == name)
        th = 7.0 if hov else 4.5
        bar = QRectF(rect.left(), rect.center().y() - th / 2, rect.width(), th)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(bar, th / 2, th / 2)
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            p.setBrush(color)
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(th, bar.width() * frac), th), th / 2, th / 2)
        if hov:
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(QPointF(bar.left() + bar.width() * frac, bar.center().y()), 6.5, 6.5)
        if enabled:
            self.hit(name, rect.adjusted(-6, -9, 6, 9))

    # -- modos
    def draw_idle(self, p, r, cx, active):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawEllipse(QPointF(cx, r.center().y()), 2.5, 2.5)

    def draw_sliver(self, p, r, cx, active):
        t = self.track
        if not t:
            return
        inner = r.adjusted(7, 2, -7, -2)
        if t.duration > 0:
            # mesma onda da timeline, em miniatura: tocado = onda colorida
            self.timeline_wave(p, inner, self.position() / t.duration, enabled=False)
        else:
            self.draw_wave(p, inner)  # ao vivo/sem duração: onda rolando

    def draw_compact(self, p, r, cx, active):
        t = self.track
        if not t:
            return
        w, h = COMPACT
        left, top = cx - w / 2, r.top() + (r.height() - h) / 2
        art = QRectF(left + 7, top + 6, 24, 24)
        self.draw_art(p, art, 7, 13)
        self.draw_wave(p, QRectF(left + w - 66, top + 8, 54, 20))
        col = TXT if t.playing else TXT2
        label = t.title if not t.artist else f"{t.title}  ·  {t.artist}"
        self.text(p, QRectF(art.right() + 10, top, w - 118, h), label, self.f_compact, col)

    def draw_volume(self, p, r, cx, active):
        w, h = VOLUME
        left, top = cx - w / 2, r.top() + (r.height() - h) / 2
        p.setFont(self.f_icon)
        p.setPen(TXT)
        p.drawText(QRectF(left + 10, top, 30, h), Qt.AlignCenter, self.vol_glyph())
        frac = 0.0 if self.muted else (self.vol or 0.0)
        self.slider(p, "vol", QRectF(left + 46, top + h / 2 - 6, w - 104, 12), frac, TXT, enabled=active)
        txt = "Mudo" if self.muted else f"{round((self.vol or 0) * 100)}"
        self.text(p, QRectF(left + w - 50, top, 38, h), txt, self.f_small, TXT2, Qt.AlignRight | Qt.AlignVCenter)

    def draw_peek(self, p, r, cx, active):
        t = self.track
        if not t:
            return
        w, h = PEEK
        left, top = cx - w / 2, r.top() + (r.height() - h) / 2
        aw = 82 if (self.art and self.art.wide) else 48
        art = QRectF(left + 11, top + 11, aw, 48)
        self.draw_art(p, art, 11)
        tx = art.right() + 12
        tw = left + w - 44 - tx
        self.text(p, QRectF(tx, top + 14, tw, 22), t.title, self.f_peek_t, TXT)
        self.text(p, QRectF(tx, top + 36, tw, 20), t.artist or t.app, self.f_peek_s, TXT2)
        self.draw_bars(p, QRectF(left + w - 36, top + h / 2 - 9, 22, 18))
        if active:
            self.hit("art", art)

    def draw_expanded(self, p, r, cx, active):
        t = self.track
        if not t:
            return
        L = cx - EXP_W / 2 + 20
        R = cx + EXP_W / 2 - 20
        y = r.top() + 20
        wide = bool(self.art and self.art.wide)
        art = QRectF(L, y, 128 if wide else 72, 72)
        self.draw_art(p, art, 14)
        if active:
            self.hit("art", art)
            if self.hovered(art):
                path = QPainterPath()
                path.addRoundedRect(art, 14, 14)
                p.fillPath(path, QColor(0, 0, 0, 90))
                p.setFont(self.f_icon)
                p.setPen(TXT)
                p.drawText(art, Qt.AlignCenter, "")  # abrir em nova janela

        tx = art.right() + 14
        tw = R - 34 - tx
        app = t.app.upper()
        p.setFont(self.f_small)
        fm = QFontMetricsF(self.f_small)
        p.setPen(Qt.NoPen)
        p.setBrush(self.accent)
        p.drawEllipse(QPointF(tx + 3, y + 10), 3, 3)
        self.text(p, QRectF(tx + 11, y + 2, fm.horizontalAdvance(app) + 4, 16), app, self.f_small, self.accent)
        self.marquee(p, QRectF(tx, y + 20, tw + 30, 26), t.title or "Sem título", self.f_title, TXT)
        self.text(p, QRectF(tx, y + 46, tw + 30, 20), t.artist or t.album, self.f_sub, TXT2)
        self.draw_bars(p, QRectF(R - 22, y + 2, 22, 16))

        y = art.bottom() + 16
        if t.duration > 0:
            pos = self.position()
            self.timeline_wave(p, QRectF(L, y - 4, R - L, 24), pos / t.duration, enabled=active and t.can_seek)
            self.text(p, QRectF(L, y + 22, 80, 14), fmt_time(pos), self.f_time, TXT3)
            self.text(p, QRectF(R - 80, y + 22, 80, 14), fmt_time(t.duration), self.f_time, TXT3,
                      Qt.AlignRight | Qt.AlignVCenter)
            y += 50
        else:
            y += 14

        cy = y + 20
        playing = t.playing
        big = QRectF(cx - 23, cy - 23, 46, 46)
        if active:
            self.hit("toggle", big)
        sc = 0.9 if (self.pressed[0] == "toggle" and time.monotonic() - self.pressed[1] < 0.12) else 1.0
        p.save()
        p.translate(big.center())
        p.scale(sc, sc)
        p.translate(-big.center())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 250 if self.hovered(big) else 232))
        p.drawEllipse(big)
        dark = QColor(10, 10, 12)
        (self.shape_pause if playing else self.shape_play)(p, QPointF(big.center().x() + (0 if playing else 1.5),
                                                                       big.center().y()), dark, 1.0)
        p.restore()

        en = active
        self.glyph_button(p, "prev", QPointF(cx - 70, cy), lambda p_, c, col: self.shape_skip(p_, c, col, False),
                          enabled=en and t.can_prev)
        self.glyph_button(p, "next", QPointF(cx + 70, cy), lambda p_, c, col: self.shape_skip(p_, c, col, True),
                          enabled=en and t.can_next)
        self.glyph_button(p, "shuffle", QPointF(cx - 140, cy), G_SHUFFLE, enabled=en and t.shuffle is not None,
                          active=bool(t.shuffle), font=self.f_icon_s)
        rep = t.repeat or 0
        self.glyph_button(p, "repeat", QPointF(cx + 140, cy), G_REPEAT1 if rep == 1 else G_REPEAT,
                          enabled=en and t.repeat is not None, active=rep > 0, font=self.f_icon_s)

        y = cy + 38
        self.draw_volume_row(p, L, R, y, active)
        y += 34
        if len(self.state.sessions) > 1:
            self.draw_chips(p, L, R, y, active)

    def draw_volume_row(self, p, L, R, y, active):
        self.glyph_button(p, "mute", QPointF(L + 12, y + 10), self.vol_glyph(), size=30, enabled=active,
                          font=self.f_icon_s)
        frac = 0.0 if self.muted else (self.vol or 0.0)
        self.slider(p, "vol", QRectF(L + 34, y + 4, R - L - 76, 12), frac, TXT, enabled=active)
        txt = "Mudo" if self.muted else f"{round((self.vol or 0) * 100)}%"
        self.text(p, QRectF(R - 40, y, 40, 20), txt, self.f_time, TXT2, Qt.AlignRight | Qt.AlignVCenter)

    def draw_chips(self, p, L, R, y, active):
        x = L
        fm = QFontMetricsF(self.f_chip)
        for s in self.state.sessions:
            label = s.app
            w = fm.horizontalAdvance(label) + (30 if s.playing else 22)
            if x + w > R:
                break
            rect = QRectF(x, y, w, 26)
            cur = self.track and s.sid == self.track.sid
            bg = QColor(255, 255, 255, 46 if cur else (30 if self.hovered(rect) else 16))
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(rect, 13, 13)
            tx = x + 11
            if s.playing:
                p.setBrush(self.accent if cur else TXT2)
                p.drawEllipse(QPointF(tx + 2.5, y + 13), 3, 3)
                tx += 9
            self.text(p, QRectF(tx, y, w, 26), label, self.f_chip, TXT if cur else TXT2)
            if active:
                self.hit("chip", rect, s.sid)
            x += w + 8

    def draw_empty(self, p, r, cx, active):
        L = cx - EXP_W / 2 + 20
        R = cx + EXP_W / 2 - 20
        y = r.top() + 20
        ic = QRectF(L, y, 52, 52)
        path = QPainterPath()
        path.addRoundedRect(ic, 14, 14)
        p.fillPath(path, QColor(255, 255, 255, 18))
        p.setFont(self.f_icon_l)
        p.setPen(TXT2)
        p.drawText(ic, Qt.AlignCenter, G_NOTE)
        self.text(p, QRectF(ic.right() + 14, y + 6, R - ic.right() - 14, 22), "Nada tocando", self.f_title, TXT)
        self.text(p, QRectF(ic.right() + 14, y + 28, R - ic.right() - 14, 20),
                  "Dê play no Spotify, YouTube, VLC…", self.f_sub, TXT2)
        self.draw_volume_row(p, L, R, y + 72, active)

    # ------------------------------------------------------------ mouse
    def hit_at(self, pos):
        for name, rect, arg in reversed(self.hits):
            if rect.contains(pos):
                return name, rect, arg
        return None

    def mouseMoveEvent(self, e):
        self.mouse = e.position()
        if self.drag:
            self.drag_update(self.mouse)
        h = self.hit_at(self.mouse)
        self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
        self.update()

    def leaveEvent(self, e):
        self.mouse = QPointF(-1, -1)
        self.update()

    def mousePressEvent(self, e):
        pos = e.position()
        if not self.pill().contains(pos):
            e.ignore()
            return
        if e.button() == Qt.RightButton:
            self.settings_menu_requested.emit(e.globalPosition().toPoint())
            return
        if e.button() == Qt.MiddleButton:
            self.do("toggle")
            return
        if e.button() != Qt.LeftButton:
            return
        h = self.hit_at(pos)
        if not h:
            if not self.expanded:
                self.set_expanded(True)
            return
        name, rect, arg = h
        self.pressed = (name, time.monotonic())
        if name in ("seek", "vol"):
            self.drag = name
            self.drag_rect = rect.adjusted(6, 0, -6, 0)
            self.drag_update(pos)
        elif name == "chip":
            self.backend.select(arg[0])
        else:
            self.do(name)
        self.update()

    def drag_update(self, pos):
        r = self.drag_rect
        frac = max(0.0, min(1.0, (pos.x() - r.left()) / max(1.0, r.width())))
        if self.drag == "seek" and self.track:
            self.drag_value = frac * self.track.duration
        elif self.drag == "vol" and self.audio:
            self.audio.set_volume(frac)
            self.vol, self.muted = frac, False
            if not self.expanded:
                self.vol_hud_until = time.monotonic() + 1.6

    def mouseReleaseEvent(self, e):
        if self.drag == "seek" and self.track:
            now = time.monotonic()
            self.seek_override = (self.drag_value, now, now + 1.5)
            self.backend.command("seek", self.track.sid, self.drag_value)
        self.drag = None
        self.update()

    def wheelEvent(self, e):
        if not self.pill().contains(e.position()) or not self.audio:
            e.ignore()
            return
        steps = e.angleDelta().y() / 120.0
        v = (self.audio.volume() or 0.0) + 0.02 * steps
        self.audio.set_volume(v)
        self.vol, self.muted = max(0.0, min(1.0, v)), False
        if not self.expanded:
            self.vol_hud_until = time.monotonic() + 1.6
        self.update()

    def do(self, name):
        t = self.track
        now = time.monotonic()
        if name == "mute" and self.audio:
            self.audio.set_muted(not self.muted)
            self.muted = not self.muted
            return
        if not t:
            return
        if name == "toggle":
            t.playing = not t.playing
            self.play_override = (t.playing, now + 1.0)
            if t.playing:
                self.last_playing = now
            self.backend.command("toggle", t.sid)
        elif name in ("next", "prev"):
            self.backend.command(name, t.sid)
        elif name == "shuffle":
            t.shuffle = not t.shuffle
            self.backend.command("shuffle", t.sid, t.shuffle)
        elif name == "repeat":
            t.repeat = {0: 2, 2: 1, 1: 0}.get(t.repeat or 0, 0)
            self.backend.command("repeat", t.sid, t.repeat)
        elif name == "art":
            winutil.focus_app(process_hint(t.aumid), t.title)
            self.set_expanded(False)

    # ------------------------------------------------------------ snapshot
    def render_mode(self, mode, bg: QImage) -> QImage:
        """Renderiza um modo já assentado (sem animação) sobre um fundo."""
        self.force_mode = mode
        tw, th = self.dims(mode)
        self.w.snap(tw)
        self.h.snap(th)
        self.top.snap(SLIVER_TOP if mode == "sliver" else TOP)
        self.alpha = 1.0
        lm = "empty" if (mode == "expanded" and not self.track) else mode
        for k in self.layer:
            self.layer[k] = 1.0 if k == lm else 0.0
        self.accent = QColor(self.accent_target)
        pix = self.grab()
        out = QImage(bg)
        q = QPainter(out)
        q.drawPixmap(0, 0, pix)
        q.end()
        self.force_mode = None
        return out
