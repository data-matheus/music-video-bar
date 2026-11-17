"""Volume do sistema e medidor de pico (para o visualizador reagir ao som real)."""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class Audio:
    """Fala com o dispositivo de saída padrão via Core Audio (pycaw).

    Revalida o dispositivo a cada poucos segundos, então trocar de fone para
    caixa de som no Windows é acompanhado sozinho.
    """

    RECHECK = 3.0

    def __init__(self):
        self._dev_id = None
        self._vol = None
        self._meter = None
        self._checked = 0.0
        self.device_name = ""

    def _ensure(self):
        now = time.monotonic()
        if self._vol is not None and now - self._checked < self.RECHECK:
            return
        self._checked = now
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IAudioMeterInformation

            dev = AudioUtilities.GetSpeakers()
            if dev is None:
                self._vol = self._meter = None
                return
            raw = getattr(dev, "_dev", dev)
            dev_id = getattr(dev, "id", None) or raw.GetId()
            if dev_id == self._dev_id and self._vol is not None:
                return
            self._dev_id = dev_id
            self.device_name = getattr(dev, "FriendlyName", "") or ""
            self._vol = raw.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
            self._meter = raw.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioMeterInformation)
        except Exception:
            log.debug("sem dispositivo de áudio", exc_info=True)
            self._vol = self._meter = None

    def _safe(self, fn, default=None):
        self._ensure()
        try:
            return fn()
        except Exception:
            self._vol = self._meter = None  # força revalidar
            return default

    def volume(self) -> float | None:
        return self._safe(lambda: float(self._vol.GetMasterVolumeLevelScalar()))

    def muted(self) -> bool:
        return bool(self._safe(lambda: self._vol.GetMute(), False))

    def set_volume(self, v: float):
        v = max(0.0, min(1.0, v))
        self._safe(lambda: self._vol.SetMasterVolumeLevelScalar(v, None))
        if v > 0 and self.muted():
            self.set_muted(False)

    def set_muted(self, m: bool):
        self._safe(lambda: self._vol.SetMute(int(bool(m)), None))

    def _session_meters(self):
        """Medidores por app (Spotify, Chrome...). Mixers virtuais como o
        SteelSeries Sonar zeram o medidor do dispositivo, mas o de cada app
        continua certo. A lista é refeita a cada 2 s."""
        now = time.monotonic()
        if now - getattr(self, "_sess_checked", 0.0) < 2.0:
            return self._sess
        self._sess_checked = now
        self._sess = []
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            for s in AudioUtilities.GetAllSessions():
                if s.Process is None:
                    continue
                try:
                    self._sess.append(s._ctl.QueryInterface(IAudioMeterInformation))
                except Exception:
                    pass
        except Exception:
            log.debug("sem sessões de áudio", exc_info=True)
        return self._sess

    def peak(self) -> float | None:
        if self._meter is None:
            self._ensure()
        best = None
        for m in [self._meter] + list(self._session_meters()):
            if m is None:
                continue
            try:
                v = float(m.GetPeakValue())
            except Exception:
                continue
            best = v if best is None else max(best, v)
        return best


class DemoAudio:
    """Áudio falso para o modo demonstração."""

    device_name = "Alto-falantes (demo)"

    def __init__(self):
        self._v = 0.62
        self._m = False

    def volume(self):
        return self._v

    def muted(self):
        return self._m

    def set_volume(self, v):
        self._v = max(0.0, min(1.0, v))
        self._m = False

    def set_muted(self, m):
        self._m = bool(m)

    def peak(self):
        import math
        t = time.monotonic()
        return 0.0 if self._m else abs(math.sin(t * 2.1)) * 0.5 + abs(math.sin(t * 5.3)) * 0.35
