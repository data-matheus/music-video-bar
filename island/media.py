"""Backend de mídia: lê as sessões do Windows (GSMTC) numa thread própria.

GSMTC (Global System Media Transport Controls) é a mesma API que alimenta o
flyout de volume do Windows. Spotify, Chrome/Edge/Firefox (YouTube, Twitch,
Netflix...), VLC, Media Player etc. publicam suas sessões nela.

Os objetos WinRT só são tocados dentro da thread do backend; a UI recebe
cópias simples (Track/MediaState) pelo sinal `changed`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING

# (trecho do AUMID em minúsculas, nome bonito, trecho do .exe para achar a janela)
_APPS = [
    ("spotify", "Spotify", "spotify"),
    ("msedge", "Edge", "msedge"),
    ("chrome", "Chrome", "chrome"),
    ("308046b0af4a39cb", "Firefox", "firefox"),
    ("firefox", "Firefox", "firefox"),
    ("opera", "Opera", "opera"),
    ("brave", "Brave", "brave"),
    ("vivaldi", "Vivaldi", "vivaldi"),
    ("arc", "Arc", "arc"),
    ("zen", "Zen", "zen"),
    ("vlc", "VLC", "vlc"),
    ("zunemusic", "Media Player", "microsoft.media.player"),
    ("microsoft.media.player", "Media Player", "microsoft.media.player"),
    ("zunevideo", "Filmes e TV", "video.ui"),
    ("applemusic", "Apple Music", "applemusic"),
    ("itunes", "iTunes", "itunes"),
    ("deezer", "Deezer", "deezer"),
    ("tidal", "TIDAL", "tidal"),
    ("amazon", "Amazon Music", "amazon"),
    ("youtube", "YouTube Music", "youtube"),
    ("soundcloud", "SoundCloud", "soundcloud"),
    ("discord", "Discord", "discord"),
    ("foobar", "foobar2000", "foobar"),
    ("aimp", "AIMP", "aimp"),
    ("musicbee", "MusicBee", "musicbee"),
    ("potplayer", "PotPlayer", "potplayer"),
    ("mpc-hc", "MPC-HC", "mpc-hc"),
    ("mpv", "mpv", "mpv"),
    ("plex", "Plex", "plex"),
    ("stremio", "Stremio", "stremio"),
]


def _lookup(aumid: str):
    a = (aumid or "").lower()
    for needle, name, exe in _APPS:
        if needle in a:
            return name, exe
    return None


def friendly_app_name(aumid: str) -> str:
    hit = _lookup(aumid)
    if hit:
        return hit[0]
    a = aumid or "App"
    if "!" in a:
        a = a.split("!")[-1]
    a = a.split("\\")[-1]
    if a.lower().endswith(".exe"):
        a = a[:-4]
    a = a.split(".")[-1] if "." in a else a
    return a[:1].upper() + a[1:] if a else "App"


def process_hint(aumid: str) -> str:
    hit = _lookup(aumid)
    if hit:
        return hit[1]
    return friendly_app_name(aumid).lower()


@dataclass
class Track:
    sid: str
    aumid: str
    app: str
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = False
    position: float = 0.0   # segundos, no instante `stamp`
    duration: float = 0.0
    start: float = 0.0      # início da timeline (para seek)
    stamp: float = 0.0      # time.monotonic() da medição
    can_seek: bool = False
    can_prev: bool = False
    can_next: bool = False
    can_toggle: bool = True
    shuffle: bool | None = None   # None = app não suporta
    repeat: int | None = None     # 0 nada, 1 faixa, 2 lista; None = não suporta
    thumb: bytes | None = field(default=None, repr=False)

    @property
    def key(self):
        return (self.aumid, self.title, self.artist)

    def position_now(self, now: float | None = None) -> float:
        pos = self.position
        if self.playing:
            now = time.monotonic() if now is None else now
            pos += max(0.0, now - self.stamp)
        if self.duration > 0:
            pos = min(pos, self.duration)
        return max(0.0, pos)


@dataclass
class MediaState:
    sessions: list[Track]
    active: Track | None


def choose_active(tracks, current_aumid, pinned, last_active, newly_playing):
    """Decide qual sessão a ilha mostra.

    Regras: sessão fixada pelo usuário vence, até outra começar a tocar;
    depois, a que o Windows marca como atual se estiver tocando; senão
    qualquer uma tocando; senão a última mostrada (pausada continua na tela).
    Retorna (track, pinned_atualizado).
    """
    if pinned:
        p = next((t for t in tracks if t.sid == pinned), None)
        if p is None or (newly_playing and pinned not in newly_playing):
            pinned = None
        else:
            return p, pinned
    cur = next((t for t in tracks if t.aumid == current_aumid), None)
    if cur and cur.playing:
        return cur, pinned
    playing = [t for t in tracks if t.playing]
    if playing:
        fresh = [t for t in playing if t.sid in newly_playing]
        return (fresh or playing)[0], pinned
    last = next((t for t in tracks if t.sid == last_active), None)
    return (last or cur or (tracks[0] if tracks else None)), pinned


class MediaBackend(QObject):
    changed = Signal(object)  # MediaState

    POLL = 0.4

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, name="media-backend", daemon=True)
        self._stop = False
        self._objs = {}          # sid -> sessão WinRT
        self._starts = {}        # sid -> início da timeline
        self._thumbs = {}        # key -> (bytes|None, primeira vez vista)
        self._pinned = None
        self._last_active = None
        self._prev_playing: set[str] = set()

    # ---- API pública (chamada da thread da UI) ----
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True
        self._kick()

    def select(self, sid: str | None):
        self._call(self._select, sid)

    def command(self, name: str, sid: str, *args):
        self._call(self._command, name, sid, *args)

    # ---- interno ----
    def _call(self, fn, *args):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(fn(*args), self._loop)

    def _kick(self):
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("backend de mídia caiu")

    async def _main(self):
        # Importa aqui para o WinRT inicializar o apartamento COM nesta thread,
        # e não na thread da UI (que o Qt inicializa como STA).
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )
        self._wake = asyncio.Event()
        mgr = None
        while not self._stop:
            try:
                if mgr is None:
                    mgr = await SessionManager.request_async()
                self.changed.emit(await self._poll(mgr))
            except Exception:
                log.exception("falha lendo sessões de mídia")
                mgr = None
            try:
                await asyncio.wait_for(self._wake.wait(), self.POLL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _poll(self, mgr) -> MediaState:
        current = mgr.get_current_session()
        current_aumid = current.source_app_user_model_id if current else None
        now_utc = dt.datetime.now(dt.timezone.utc)
        now = time.monotonic()
        tracks: list[Track] = []
        objs, starts, seen = {}, {}, {}
        for s in list(mgr.get_sessions()):
            try:
                t = await self._read_session(s, seen, now_utc, now)
            except Exception:
                log.debug("sessão ilegível", exc_info=True)
                continue
            objs[t.sid] = s
            starts[t.sid] = t.start
            tracks.append(t)
        self._objs, self._starts = objs, starts

        # Sessões zumbis (sem título e paradas) não aparecem.
        tracks = [t for t in tracks if t.title or t.playing]
        playing_now = {t.sid for t in tracks if t.playing}
        newly = playing_now - self._prev_playing
        self._prev_playing = playing_now

        active, self._pinned = choose_active(
            tracks, current_aumid, self._pinned, self._last_active, newly)
        self._last_active = active.sid if active else None
        if log.isEnabledFor(logging.DEBUG):
            log.debug("poll: %s | ativa=%s", [(t.sid, t.playing) for t in tracks],
                      active.sid if active else None)
        return MediaState(sessions=tracks, active=active)

    async def _read_session(self, s, seen, now_utc, now) -> Track:
        aumid = s.source_app_user_model_id or "?"
        n = seen.get(aumid, 0)
        seen[aumid] = n + 1
        sid = aumid if n == 0 else f"{aumid}#{n}"
        t = Track(sid=sid, aumid=aumid, app=friendly_app_name(aumid), stamp=now)

        info = s.get_playback_info()
        if info is not None:
            t.playing = int(info.playback_status) == PLAYING
            c = info.controls
            if c is not None:
                t.can_seek = bool(c.is_playback_position_enabled)
                t.can_prev = bool(c.is_previous_enabled)
                t.can_next = bool(c.is_next_enabled)
                t.can_toggle = bool(c.is_play_pause_toggle_enabled or c.is_play_enabled or c.is_pause_enabled)
                if c.is_shuffle_enabled:
                    t.shuffle = bool(info.is_shuffle_active) if info.is_shuffle_active is not None else False
                if c.is_repeat_enabled:
                    r = info.auto_repeat_mode
                    t.repeat = int(r) if r is not None else 0

        tl = s.get_timeline_properties()
        if tl is not None:
            start = tl.start_time.total_seconds()
            end = tl.end_time.total_seconds()
            pos = tl.position.total_seconds() - start
            dur = max(0.0, end - start)
            lu = tl.last_updated_time
            if t.playing and lu is not None and lu.year > 2000:
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=dt.timezone.utc)
                age = (now_utc - lu).total_seconds()
                if 0 <= age < 6 * 3600:
                    pos += age
            t.start = start
            t.duration = dur
            t.position = max(0.0, min(pos, dur) if dur > 0 else pos)

        try:
            p = await s.try_get_media_properties_async()
        except Exception:
            p = None
        if p is not None:
            t.title = p.title or ""
            t.artist = p.artist or p.album_artist or ""
            t.album = p.album_title or ""
            t.thumb = await self._thumb_for(t.key, p.thumbnail, now)
        return t

    async def _thumb_for(self, key, ref, now):
        cached = self._thumbs.get(key)
        # Nos primeiros segundos de uma faixa a capa ainda pode ser a da
        # anterior (Spotify faz isso), então relemos por um tempinho.
        if cached and (now - cached[1] > 4.0) and cached[0] is not None:
            return cached[0]
        data = None
        if ref is not None:
            try:
                data = await _read_stream(ref)
            except Exception:
                log.debug("capa ilegível", exc_info=True)
        first = cached[1] if cached else now
        if data is None and cached:
            data = cached[0]
        self._thumbs[key] = (data, first)
        if len(self._thumbs) > 40:
            for k in list(self._thumbs)[:-20]:
                self._thumbs.pop(k, None)
        return data

    async def _select(self, sid):
        self._pinned = sid
        self._wake.set()

    async def _command(self, name, sid, *args):
        s = self._objs.get(sid)
        if s is None:
            return
        try:
            if name == "toggle":
                await s.try_toggle_play_pause_async()
            elif name == "next":
                await s.try_skip_next_async()
            elif name == "prev":
                await s.try_skip_previous_async()
            elif name == "seek":
                ticks = int((args[0] + self._starts.get(sid, 0.0)) * 10_000_000)
                await s.try_change_playback_position_async(ticks)
            elif name == "shuffle":
                await s.try_change_shuffle_active_async(bool(args[0]))
            elif name == "repeat":
                from winrt.windows.media import MediaPlaybackAutoRepeatMode
                await s.try_change_auto_repeat_mode_async(MediaPlaybackAutoRepeatMode(int(args[0])))
        except Exception:
            log.exception("comando %s falhou", name)
        await asyncio.sleep(0.12)
        self._wake.set()


async def _read_stream(ref) -> bytes | None:
    from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
    stream = await ref.open_read_async()
    try:
        size = int(stream.size)
        if size <= 0 or size > 16_000_000:
            return None
        buf = Buffer(size)
        res = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        try:
            return bytes(res)
        except TypeError:
            reader = DataReader.from_buffer(res)
            out = bytearray(res.length)
            reader.read_bytes(out)
            return bytes(out)
    finally:
        try:
            stream.close()
        except Exception:
            pass
