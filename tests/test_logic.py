"""Testes da lógica pura (não tocam em mídia real nem no áudio do sistema).

    .venv\\Scripts\\python -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from island.media import Track, choose_active, friendly_app_name, process_hint  # noqa: E402
from island.ui import Spring, fmt_time  # noqa: E402


def T(sid, playing=False, aumid=None, title="x"):
    return Track(sid=sid, aumid=aumid or sid, app=sid, title=title, playing=playing)


class AppNames(unittest.TestCase):
    def test_known(self):
        self.assertEqual(friendly_app_name("Spotify.exe"), "Spotify")
        self.assertEqual(friendly_app_name("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"), "Spotify")
        self.assertEqual(friendly_app_name("Chrome"), "Chrome")
        self.assertEqual(friendly_app_name("MSEdge"), "Edge")
        self.assertEqual(friendly_app_name("308046B0AF4A39CB"), "Firefox")
        self.assertEqual(friendly_app_name("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic"), "Media Player")

    def test_unknown(self):
        self.assertEqual(friendly_app_name("C:\\Apps\\coolplayer.exe"), "Coolplayer")
        self.assertEqual(friendly_app_name(""), "App")

    def test_process_hint(self):
        self.assertEqual(process_hint("308046B0AF4A39CB"), "firefox")
        self.assertEqual(process_hint("Spotify.exe"), "spotify")


class ChooseActive(unittest.TestCase):
    def test_current_playing_wins(self):
        a, b = T("a", True), T("b", True)
        t, _ = choose_active([a, b], "b", None, None, set())
        self.assertEqual(t.sid, "b")

    def test_prefers_playing_over_paused_current(self):
        a, b = T("a", False), T("b", True)
        t, _ = choose_active([a, b], "a", None, None, set())
        self.assertEqual(t.sid, "b")

    def test_newly_started_wins_among_playing(self):
        a, b = T("a", True), T("b", True)
        t, _ = choose_active([a, b], None, None, None, {"b"})
        self.assertEqual(t.sid, "b")

    def test_paused_keeps_last_shown(self):
        a, b = T("a"), T("b")
        t, _ = choose_active([a, b], "a", None, "b", set())
        self.assertEqual(t.sid, "b")

    def test_pin_holds_until_another_starts(self):
        a, b = T("a", True), T("b", False)
        t, pin = choose_active([a, b], "a", "b", None, set())
        self.assertEqual((t.sid, pin), ("b", "b"))
        t, pin = choose_active([a, b], "a", "b", None, {"a"})
        self.assertEqual((t.sid, pin), ("a", None))

    def test_pin_dropped_when_session_closes(self):
        a = T("a", True)
        t, pin = choose_active([a], "a", "gone", None, set())
        self.assertEqual((t.sid, pin), ("a", None))

    def test_empty(self):
        t, _ = choose_active([], None, None, None, set())
        self.assertIsNone(t)


class Position(unittest.TestCase):
    def test_extrapolates_while_playing(self):
        t = Track("a", "a", "a", playing=True, position=10, duration=100, stamp=50.0)
        self.assertAlmostEqual(t.position_now(55.0), 15.0)

    def test_clamped_to_duration(self):
        t = Track("a", "a", "a", playing=True, position=98, duration=100, stamp=0.0)
        self.assertEqual(t.position_now(10.0), 100.0)

    def test_frozen_when_paused(self):
        t = Track("a", "a", "a", playing=False, position=10, duration=100, stamp=0.0)
        self.assertEqual(t.position_now(99.0), 10.0)


class Misc(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(fmt_time(0), "0:00")
        self.assertEqual(fmt_time(65.9), "1:05")
        self.assertEqual(fmt_time(3725), "1:02:05")

    def test_spring_settles_with_overshoot(self):
        s = Spring(0)
        s.target = 100
        peak = 0
        for _ in range(200):
            s.step(1 / 60)
            peak = max(peak, s.x)
        self.assertEqual(s.x, 100)
        self.assertGreater(peak, 100)  # o "quique"


if __name__ == "__main__":
    unittest.main()
