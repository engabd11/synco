# Hue Music Sync for Home Assistant

A custom Home Assistant integration that syncs **Philips Hue Entertainment areas**
to music played through **Music Assistant** — a self-hosted replacement for
Samsung's music sync. It streams colour directly to the bridge over the low-latency
Hue Entertainment API (~50 Hz over DTLS), reacts to the **beat** and **frequency
content** of the audio, and lights each bulb differently based on its position in
the area and the frequency band it represents.

> Manage and arrange the lights inside an entertainment area in the **Hue app**.
> This integration consumes the areas that already exist and drives them.

## Features

- **Direct Hue Entertainment streaming** — bypasses normal Zigbee light commands
  for fast, fluid, high-rate updates.
- **Real-time beat + frequency analysis** of the Music Assistant stream (no
  microphone, no extra hardware).
- **Smart, non-uniform choreography** — bass lights thump on the kick, treble
  lights shimmer, colours spread spatially across the area.
- **Colour schemes**, including a vivid palette **extracted from the current
  album cover**, plus Warm / Cool / Neon / Party / Mono / Rainbow.
- **Effect modes**: Pulse, Spectrum, Wave, Ambient.
- Per-area **switch** to activate/deactivate, plus **select/number** entities for
  scheme, effect, followed player, latency offset and intensity — and
  `hue_music_sync.activate` / `deactivate` / `set_options` **services** for automations.

## Requirements

- A **v2 (square) Philips Hue bridge** with at least one Entertainment area
  created in the Hue app. (The round v1 bridge does not support entertainment
  streaming.)
- **Music Assistant** running and connected to Home Assistant.
- Home Assistant with the bundled **ffmpeg**, **numpy** and **cryptography**
  (all standard on HAOS, Container and Supervised installs). No external `openssl`
  binary is required — the DTLS channel is implemented in pure Python.

## Installation (HACS custom repository)

1. HACS → ⋮ → *Custom repositories* → add this repo, category **Integration**.
2. Install **Hue Music Sync**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Hue Music Sync*.
4. Enter the **bridge IP**, **press the link button** on the bridge when prompted,
   then choose which **entertainment areas** to enable.

(Alternatively copy `custom_components/hue_music_sync/` into your HA `config/custom_components/` folder.)

Each enabled area becomes a device with just two controls — kept deliberately
Samsung-simple:

| Entity | Purpose |
| --- | --- |
| `switch.music_sync_<area>` | Activate / deactivate sync |
| `select` Mode | `Album colours`, `Energetic`, `Party`, `Chill` |

Each **Mode** is a curated preset bundling a colour scheme, effect and intensity,
so there are no individual scheme/effect/brightness knobs to fiddle with. The
followed media player auto-detects the one that's playing (override via the
`activate` service if needed).

> **One area at a time per bridge.** A Hue bridge can stream to only one
> entertainment area at a time. Activating a second area on the same bridge
> automatically takes over from the one already running.

### Smooth dimming

Colour is streamed in Hue's native **xy chromaticity + a dedicated brightness
channel** (HueStream colourspace `0x01`) at ~40 Hz, the same model the official
Spotify/Samsung integrations use. Keeping brightness on its own channel (instead
of shrinking RGB magnitudes) lets the bridge map dimming through the bulb's own
smooth curve, so fades don't step at the low end.

## Usage

1. Start playback in Music Assistant.
2. Turn on the area's **Music Sync** switch (or call `hue_music_sync.activate`).
3. Tune **Latency offset** so the flashes line up with the sound (speaker buffering
   means there is always some delay to dial out).

Example automation — start album-art sync when Music Assistant begins playing:

```yaml
automation:
  - alias: Music sync on play
    trigger:
      - platform: state
        entity_id: media_player.living_room
        to: playing
    action:
      - service: hue_music_sync.activate
        target:
          entity_id: switch.music_sync_living_room
        data:
          mode: album
```

## Services

| Service | What it does |
| --- | --- |
| `hue_music_sync.activate` | Start sync for the targeted area(s); optionally set `mode` / `media_player` first |
| `hue_music_sync.deactivate` | Stop sync for the targeted area(s) |
| `hue_music_sync.set_options` | Change `mode` / `media_player` **live**, without restarting sync — e.g. switch the vibe mid-song |

All three target the area's `switch` entity. Example — go full party on the drop:

```yaml
- service: hue_music_sync.set_options
  target:
    entity_id: switch.music_sync_living_room
  data:
    mode: party
```

## Modes

Each mode bundles a colour scheme + choreography + intensity:

- **Album colours** — palette extracted from the current album art; each light
  owns a frequency band by its position (bass lights react to bass, treble to
  highs). The default.
- **Energetic** — neon palette, whole-area beat pulses.
- **Party** — full rainbow palette with a wavefront that sweeps across the area
  on the beat.
- **Chill** — cool palette, slow drift with gentle energy modulation; no hard
  beats.

## Validation spikes

Two self-contained scripts under `scripts/` let you de-risk the moving parts on
your own host before/while using the integration:

- `python scripts/spike_dtls.py --host <bridge-ip> --pair` then `--list` then a
  colour-cycle run — proves the Hue Entertainment DTLS transport works.
- `python scripts/spike_ma.py --url <stream-or-file>` — decodes audio with ffmpeg
  and runs the real analyzer, printing beats / tempo / per-band levels.

## How it works

```
Music Assistant audio ──ffmpeg──▶ PCM ──▶ Analyzer (bands + beat)
                                              │
album cover ──ffmpeg──▶ palette ──▶ Effect engine (per-channel colour)
                                              │
                                   HueStream v2 frames ──DTLS(openssl)──▶ Hue bridge
```

Audio is **position-locked** to the player's reported playback position, so
pauses, seeks and track changes stay aligned.

## Development

The DSP / colour / encoder logic has no Home Assistant dependency and is covered
by a fast unit-test suite that runs without HA:

```bash
pip install pytest numpy
pytest tests/
```

These cover the HueStream frame encoder, palette sampling, album-art k-means and
the analyzer's beat detection.

## Limitations

- Works with **Music Assistant** audio (the chosen beat source). Arbitrary HA
  players without an accessible stream are not supported.
- Perfect lip-sync is not possible due to player buffering — use the latency
  offset to align by ear.
- Requires a **v2** Hue bridge; entertainment streaming is not available on v1.
- The DTLS channel is a self-contained pure-Python DTLS 1.2 PSK implementation
  (built on the bundled `cryptography`), since `python-mbedtls` has no modern
  wheels and the HA container ships no `openssl` CLI. It implements exactly one
  cipher suite (`TLS_PSK_WITH_AES_128_GCM_SHA256`) — enough for the bridge.

## License

MIT
