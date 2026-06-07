#!/usr/bin/env python3
"""Milestone 1 spike: prove Hue Entertainment DTLS streaming end-to-end.

Exercises the exact transport the integration uses: pairing, CLIP v2 config
discovery, the ``{"action":"start"}`` handover, and a HueStream v2 colour cycle
over the integration's pure-Python DTLS-PSK client (``hue/dtls.py``). Pairing and
discovery use only the stdlib; the streaming part needs ``cryptography`` (already
present in Home Assistant, and easy to ``pip install`` on a desktop on the same
LAN as the bridge).

Usage
-----
1. Press the bridge link button, then mint keys:
       python spike_dtls.py --host 192.168.1.10 --pair
2. List entertainment areas:
       python spike_dtls.py --host 192.168.1.10 --app-key KEY --list
3. Run a 15s colour cycle on an area:
       python spike_dtls.py --host 192.168.1.10 --app-key KEY \
           --client-key HEX --config-id UUID
"""

from __future__ import annotations

import argparse
import colorsys
import ctypes
import importlib.util
import json
import math
import os
import ssl
import sys
import time
import urllib.request

DTLS_PORT = 2100


def _precise_sleep_until(deadline: float) -> None:
    """Sleep until ``deadline`` (perf_counter) with sub-millisecond accuracy.

    Plain time.sleep is quantised to the OS timer tick (~15.6ms on Windows),
    which makes a 50Hz stream lurch. Sleep most of the way, then busy-wait the
    final ~2ms so frames go out on an even cadence the bridge can interpolate.
    """
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.0025:
            time.sleep(remaining - 0.0015)
        # else spin


def _load_dtls_client():
    """Load DtlsPskClient straight from the integration source (no package import)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(
        os.path.dirname(here), "custom_components", "hue_music_sync", "hue", "dtls.py"
    )
    spec = importlib.util.spec_from_file_location("hms_dtls", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DtlsPskClient


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(method: str, url: str, *, headers=None, body=None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ctx(), timeout=10) as resp:
        return json.loads(resp.read().decode())


def pair(host: str) -> None:
    res = _request(
        "POST", f"https://{host}/api",
        body={"devicetype": "hue_music_sync#spike", "generateclientkey": True},
    )
    entry = res[0]
    if "error" in entry:
        print("ERROR:", entry["error"].get("description"), file=sys.stderr)
        if entry["error"].get("type") == 101:
            print("Press the bridge link button, then re-run within 30s.", file=sys.stderr)
        sys.exit(1)
    s = entry["success"]
    print("app_key   :", s["username"])
    print("client_key:", s["clientkey"])


def list_configs(host: str, app_key: str) -> None:
    res = _request(
        "GET", f"https://{host}/clip/v2/resource/entertainment_configuration",
        headers={"hue-application-key": app_key},
    )
    for cfg in res.get("data", []):
        name = cfg.get("metadata", {}).get("name", "?")
        n = len(cfg.get("channels", []))
        print(f"{cfg['id']}  | {name!r}  | channels={n}  | status={cfg.get('status')}")


def set_action(host: str, app_key: str, config_id: str, action: str) -> None:
    _request(
        "PUT", f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}",
        headers={"hue-application-key": app_key}, body={"action": action},
    )


def rgb_to_xy(r: float, g: float, b: float) -> tuple[float, float]:
    """Standard Hue RGB -> xy chromaticity (with sRGB gamma + Wide RGB D65)."""
    def gam(c: float) -> float:
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    r, g, b = gam(r), gam(g), gam(b)
    x_ = r * 0.649926 + g * 0.103455 + b * 0.197109
    y_ = r * 0.234327 + g * 0.743075 + b * 0.022673
    z_ = r * 0.000000 + g * 0.053077 + b * 1.035763
    total = x_ + y_ + z_
    if total <= 0:
        return 0.0, 0.0
    return x_ / total, y_ / total


def build_frame(config_id: str, seq: int, colourspace: int, triplets) -> bytes:
    """``triplets`` = list of (cid, v0, v1, v2) with each value normalised 0..1."""
    frame = bytearray(b"HueStream" + b"\x02\x00")
    frame.append(seq & 0xFF)
    frame += b"\x00\x00" + bytes([colourspace]) + b"\x00"  # reserved, cs, reserved
    frame += config_id.encode("ascii")
    for cid, v0, v1, v2 in triplets:
        frame.append(cid & 0xFF)
        frame += int(max(0.0, min(1.0, v0)) * 65535).to_bytes(2, "big")
        frame += int(max(0.0, min(1.0, v1)) * 65535).to_bytes(2, "big")
        frame += int(max(0.0, min(1.0, v2)) * 65535).to_bytes(2, "big")
    return bytes(frame)


def _shape(norm: float, floor: float, gamma: float) -> float:
    """Map a 0..1 sweep to a sendable brightness: gamma curve, then min floor."""
    return floor + (1.0 - floor) * (max(0.0, norm) ** gamma)


def _frame_channels(
    pattern: str, t: float, channel_ids: list[int], gamma: float = 1.0, floor: float = 0.0
) -> list:
    """Compute (cid, r, g, b, bri) per channel: full-brightness colour + brightness.

    Keeping colour and brightness separate lets the xy+brightness colourspace
    dim via the bulb's native brightness channel. ``floor``/``gamma`` shape the
    brightness sweep.
    """
    n = max(1, len(channel_ids))
    out = []
    if pattern == "breathe":
        bri = _shape(0.5 - 0.5 * math.cos(2 * math.pi * 0.4 * t), floor, gamma)
        r, g, b = colorsys.hsv_to_rgb(0.62, 1.0, 1.0)  # blue
        for cid in channel_ids:
            out.append((cid, r, g, b, bri))
    elif pattern == "hue":
        for i, cid in enumerate(channel_ids):
            hue = ((t * 0.12) + i / n) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            out.append((cid, r, g, b, 1.0))
    elif pattern == "beat":
        # Fast musical pulses (~110 BPM) with colour changing each beat: the
        # real use case. Rapid brightness changes hide the bulb's level steps.
        bpm = 110.0
        period = 60.0 / bpm
        ph = (t % period) / period
        bri = _shape(math.exp(-ph * 6.0), floor, gamma)
        hue = (int(t / period) * 0.16) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        for cid in channel_ids:
            out.append((cid, r, g, b, bri))
    else:  # "cycle": both dimming and hue together
        bri = _shape(0.5 + 0.5 * math.sin(2 * math.pi * 0.5 * t), floor, gamma)
        for i, cid in enumerate(channel_ids):
            hue = ((t * 0.08) + i / n) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            out.append((cid, r, g, b, bri))
    return out


def _to_triplets(channels, space: str):
    """Convert (cid,r,g,b,bri) -> protocol triplets for the chosen colourspace."""
    triplets = []
    for cid, r, g, b, bri in channels:
        if space == "xy":
            x, y = rgb_to_xy(r, g, b)
            triplets.append((cid, x, y, bri))  # xy + dedicated brightness
        else:  # rgb: bake brightness into the channels
            triplets.append((cid, r * bri, g * bri, b * bri))
    return triplets


def run_cycle(
    host: str, app_key: str, client_key: str, config_id: str,
    seconds: float, pattern: str = "cycle", gamma: float = 1.0, floor: float = 0.0,
    space: str = "xy", fps: int = 40,
) -> None:
    DtlsPskClient = _load_dtls_client()
    colourspace = 0x01 if space == "xy" else 0x00

    res = _request(
        "GET", f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}",
        headers={"hue-application-key": app_key},
    )
    channel_ids = [ch["channel_id"] for ch in res["data"][0]["channels"]]
    print(f"Streaming to {len(channel_ids)} channels: {channel_ids}")

    set_action(host, app_key, config_id, "start")
    time.sleep(0.3)

    client = DtlsPskClient(host, DTLS_PORT, app_key.encode(), bytes.fromhex(client_key))
    print("Performing DTLS handshake...")
    try:
        client.connect()
    except Exception as err:  # noqa: BLE001
        print("DTLS handshake failed:", err, file=sys.stderr)
        set_action(host, app_key, config_id, "stop")
        sys.exit(1)

    print(f"DTLS up. Pattern={pattern!r} space={space!r} fps={fps} (Ctrl+C to stop early)...")
    interval = 1.0 / fps
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)  # 1ms timer resolution on Windows
    except Exception:  # noqa: BLE001 - non-Windows / unavailable
        pass

    start = time.perf_counter()
    next_t = start
    seq = 0
    try:
        while time.perf_counter() - start < seconds:
            t = time.perf_counter() - start
            channels = _frame_channels(pattern, t, channel_ids, gamma, floor)
            triplets = _to_triplets(channels, space)
            seq = (seq + 1) & 0xFF
            client.send(build_frame(config_id, seq, colourspace, triplets))
            next_t += interval
            _precise_sleep_until(next_t)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:  # noqa: BLE001
            pass
        client.close()
        set_action(host, app_key, config_id, "stop")
        print("Stopped. Lights should restore to their previous state.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--app-key")
    p.add_argument("--client-key")
    p.add_argument("--config-id")
    p.add_argument("--pair", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--pattern", choices=["cycle", "breathe", "hue", "beat"], default="cycle")
    p.add_argument("--gamma", type=float, default=1.0)
    p.add_argument("--floor", type=float, default=0.0)
    p.add_argument("--space", choices=["xy", "rgb"], default="xy")
    p.add_argument("--fps", type=int, default=40)
    a = p.parse_args()

    if a.pair:
        pair(a.host)
    elif a.list:
        if not a.app_key:
            p.error("--list requires --app-key")
        list_configs(a.host, a.app_key)
    elif a.app_key and a.client_key and a.config_id:
        run_cycle(
            a.host, a.app_key, a.client_key, a.config_id,
            a.seconds, a.pattern, a.gamma, a.floor, a.space, a.fps,
        )
    else:
        p.error("streaming requires --app-key, --client-key and --config-id")


if __name__ == "__main__":
    main()
