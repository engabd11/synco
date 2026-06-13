"""Highlight selection: the apartment-sync look.

Only the beats that stand out in their passage fire (rank-based, so a flat
four-to-the-floor still hits every kick), the very biggest hits take the whole
room, per-lamp colours are distinct, and the colour layout re-deals on
highlights instead of continuously washing.
"""

from __future__ import annotations

import colorsys

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS, ROLE_BASS, ROLE_VOCAL
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _quiet() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.2, "bass": 0.2, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.4,
    )


def _grid(predicted: bool, phase: float = 0.3, accent: float = 0.8,
          beat_in_bar: int = 0) -> BeatGrid:
    return BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5, phase=phase,
        time_to_next_beat=(1.0 - phase) * 0.5, next_beat_t=0.0,
        bar_phase=(beat_in_bar + phase) / 4.0, predicted_beat=predicted,
        accent=accent, accent_now=accent, beat_in_bar=beat_in_bar,
    )


# The accent pattern of a "dynamic mix": one standout hit per bar, one medium,
# two ordinary. Phases stay below the wave-anticipation window so flashes are
# the only brightness events under test.
_PATTERN = (1.0, 0.3, 0.55, 0.3)


def _play_beat(eng: EffectEngine, accent: float, beat_in_bar: int):
    """One beat: a pre frame, the tick frame, then decay frames. Returns
    (pre_out, tick_out)."""
    pre = eng.render(_quiet(), _DT, beatgrid=_grid(False, accent=accent,
                                                   beat_in_bar=beat_in_bar))
    tick = eng.render(_quiet(), _DT, beatgrid=_grid(True, accent=accent,
                                                    beat_in_bar=beat_in_bar))
    for _ in range(20):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, accent=accent,
                                                 beat_in_bar=beat_in_bar))
    return pre, tick


def _fill_window(eng: EffectEngine, bars: int = 6) -> None:
    for _ in range(bars):
        for b, acc in enumerate(_PATTERN):
            _play_beat(eng, acc, b)


def test_extreme_fires_only_the_standout_beats():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)

    # Ordinary beat (accent 0.3, off-downbeat): the room stays dark.
    pre, tick = _play_beat(eng, 0.3, 1)
    for cid in pre:
        assert max(tick[cid]) < max(pre[cid]) + 0.05

    # Standout beat (accent 1.0, the bar's "one"): the bass lights slam.
    pre, tick = _play_beat(eng, 1.0, 0)
    bass = [c for c, r in eng.roles.items() if r == ROLE_BASS]
    assert any(max(tick[c]) > max(pre[c]) + 0.3 for c in bass)


def test_medium_beats_do_not_outrank_the_highlights():
    # Accent 0.55 sits above the floor but below the passage's top quartile:
    # in Extreme it must stay (nearly) dark too — selectivity is by rank.
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)
    pre, tick = _play_beat(eng, 0.55, 2)
    for cid in pre:
        assert max(tick[cid]) < max(pre[cid]) + 0.08


def test_full_room_moment_takes_the_vocal_lights_too():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert vocal  # Extreme's role mix includes a vocal light on 5 channels

    # An ordinary highlight stays role-separated: vocal lights stay dim.
    pre, tick = _play_beat(eng, 0.85, 0)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert all(max(tick[c]) < 0.5 for c in vocal)

    # A passage-topping hit (accent >= full_room_accent) slams every light.
    pre, tick = _play_beat(eng, 0.95, 0)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert all(max(tick[c]) > max(pre[c]) + 0.3 for c in vocal)


def test_colour_holds_between_highlights_and_jumps_on_them():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)

    before = eng.colour_phase
    _play_beat(eng, 0.3, 1)  # ordinary beat: colour holds (drift only)
    hold_delta = eng.colour_phase - before

    before = eng.colour_phase
    _play_beat(eng, 1.0, 0)  # highlight: the layout re-deals
    jump_delta = eng.colour_phase - before

    assert jump_delta > hold_delta + 0.5 * MODE_PARAMS[SyncMode.EXTREME].colour_jump


def test_extreme_distributes_distinct_hues_across_the_room():
    from hue_music_sync.color.palette import get_palette
    from hue_music_sync.const import ColorScheme

    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    eng.set_scheme(ColorScheme.RAINBOW)
    out = None
    for _ in range(10):
        out = eng.render(_quiet(), _DT, beatgrid=_grid(False))
    hues = set()
    for c in out.values():
        m = max(c)
        if m > 1e-6:
            h = colorsys.rgb_to_hsv(c[0] / m, c[1] / m, c[2] / m)[0]
            hues.add(round(h * 6) % 6)
    # Golden-ratio spacing: 5 lamps land in several distinct hue families,
    # not one slice of a smooth gradient.
    assert len(hues) >= 3
