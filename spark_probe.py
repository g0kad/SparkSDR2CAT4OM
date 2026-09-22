#!/usr/bin/env python3
"""
spark_probe.py - discovery probe for SparkSDR's WebSocket + rigctl interfaces.

Purpose: answer the open questions before designing the SparkSDR -> TCI bridge
for CAT4OM:
  1. Does the WebSocket answer, and what do getVersion/getRadios/getReceivers return?
  2. Does SparkSDR PUSH state changes when you tune/change mode in its UI,
     or only reply to requests?
  3. What is the binary audio frame format (header, container, rate, sample type)?
  4. Do setFrequency/setMode round-trip? (optional, --write)
  5. What does the per-receiver rigctl port report, and does PTT work? (optional)

Requirements:  Python 3.9+,  pip install websockets
In SparkSDR:   enable WebSockets in settings; note the rigctl port shown on the
               receiver you want to test.

Examples:
  python spark_probe.py
  python spark_probe.py --rx 0 --rigctl-port 51111
  python spark_probe.py --rx 0 --rigctl-port 51111 --write
  python spark_probe.py --rx 0 --rigctl-port 51111 --ptt      (TRANSMITS ~1 s!)

Everything is logged to spark_probe_<timestamp>.log, and captured audio is
written to spark_audio_<timestamp>.wav plus a few raw frames in ./frames/.
"""

import argparse
import asyncio
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = f"spark_probe_{STAMP}.log"
_log = open(LOG_PATH, "w", encoding="utf-8")


def log(msg=""):
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {msg}"
    print(line)
    _log.write(line + "\n")
    _log.flush()


def banner(title):
    log("")
    log("=" * 70)
    log(title)
    log("=" * 70)


# --------------------------------------------------------------------------
# WebSocket helpers
# --------------------------------------------------------------------------

async def request(ws, payload, wait=1.5, show_binary=False):
    """Send a JSON command and log every text message received within `wait` s."""
    txt = json.dumps(payload)
    log(f">> {txt}")
    await ws.send(txt)
    replies = []
    end = time.monotonic() + wait
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if isinstance(msg, (bytes, bytearray)):
            if show_binary:
                log(f"<< [binary {len(msg)} bytes] type={msg[0] if msg else '?'}")
            continue
        log(f"<< {msg}")
        try:
            replies.append(json.loads(msg))
        except json.JSONDecodeError:
            pass
    if not replies:
        log("   (no text reply)")
    return replies


def find_cmd(replies, name):
    for r in replies:
        if isinstance(r, dict) and r.get("cmd", "").lower() == name.lower():
            return r
    return None


# --------------------------------------------------------------------------
# Binary audio analysis
# --------------------------------------------------------------------------

def parse_wav(b):
    """Return dict with fmt fields and PCM bytes if b is a RIFF/WAVE blob."""
    if len(b) < 12 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    info = {"container": "WAV"}
    pos = 12
    while pos + 8 <= len(b):
        cid = b[pos:pos + 4]
        size = struct.unpack_from("<I", b, pos + 4)[0]
        body = b[pos + 8: pos + 8 + size]
        if cid == b"fmt ":
            tag, ch, rate, brate, align, bits = struct.unpack_from("<HHIIHH", body, 0)
            info.update(format_tag=tag, channels=ch, rate=rate, bits=bits,
                        block_align=align)
        elif cid == b"data":
            info["pcm"] = body
            # some streamers write size 0/0xFFFFFFFF; fall back to remainder
            if size in (0, 0xFFFFFFFF) or len(body) < size:
                info["pcm"] = b[pos + 8:]
        pos += 8 + size + (size & 1)
    return info


def sniff(payload):
    if payload[:4] == b"RIFF":
        return "WAV (RIFF)"
    if payload[:4] == b"OggS":
        return "Ogg (Opus/Vorbis?)"
    if payload[:4] == b"fLaC":
        return "FLAC"
    if payload[:3] == b"ID3" or (len(payload) > 1 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0):
        return "MP3"
    return "unknown / raw"


def write_wav(path, tag, ch, rate, bits, pcm):
    align = ch * bits // 8
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, tag, ch, rate, rate * align, align, bits))
        f.write(b"data" + struct.pack("<I", len(pcm)))
        f.write(pcm)


async def capture_audio(ws, rx, seconds, save_frames=5):
    banner(f"AUDIO: subscribeToAudio RxID={rx} for {seconds}s")
    os.makedirs("frames", exist_ok=True)
    await ws.send(json.dumps({"cmd": "subscribeToAudio", "RxID": rx, "Enable": True}))
    log(f">> subscribeToAudio RxID={rx} Enable=true")

    frames, total_payload, pcm_parts = 0, 0, []
    fmt = None
    first_t = last_t = None
    types_seen = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        now = time.monotonic()
        if isinstance(msg, str):
            log(f"<< (text during audio) {msg[:300]}")
            continue
        b = bytes(msg)
        dtype = b[0] if b else -1
        types_seen[dtype] = types_seen.get(dtype, 0) + 1
        if dtype != 1:
            continue
        frames += 1
        first_t = first_t or now
        last_t = now
        header, payload = b[:5], b[5:]
        total_payload += len(payload)
        if frames <= save_frames:
            with open(os.path.join("frames", f"frame_{frames:02d}.bin"), "wb") as f:
                f.write(b)
            rx_be = struct.unpack(">i", header[1:5])[0]
            rx_le = struct.unpack("<i", header[1:5])[0]
            log(f"   frame {frames}: total={len(b)}B header={header.hex()} "
                f"rxid(BE)={rx_be} rxid(LE)={rx_le} payload={len(payload)}B "
                f"container={sniff(payload)} first32={payload[:32].hex()}")
        w = parse_wav(payload)
        if w and "pcm" in w:
            if fmt is None:
                fmt = w
                log(f"   WAV fmt: tag={w.get('format_tag')} (1=PCM int, 3=IEEE float) "
                    f"channels={w.get('channels')} rate={w.get('rate')} bits={w.get('bits')}")
            pcm_parts.append(w["pcm"])

    await ws.send(json.dumps({"cmd": "subscribeToAudio", "RxID": rx, "Enable": False}))
    log(f">> subscribeToAudio RxID={rx} Enable=false")

    log(f"   binary message types seen: {types_seen}")
    if not frames:
        log("   !! No audio frames received. Is the radio running and the receiver ID correct?")
        return
    dur = (last_t - first_t) if last_t and first_t and last_t > first_t else 0
    log(f"   audio frames: {frames} in {dur:.2f}s -> {frames / dur if dur else 0:.1f} frames/s, "
        f"avg payload {total_payload / frames:.0f}B, {total_payload / dur if dur else 0:.0f} B/s")
    if fmt and pcm_parts:
        pcm = b"".join(pcm_parts)
        align = fmt["channels"] * fmt["bits"] // 8
        per_frame = len(pcm) / len(pcm_parts) / align
        log(f"   samples per frame ~{per_frame:.0f}; "
            f"implied rate from wall clock ~{per_frame * frames / dur if dur else 0:.0f} Hz")
        out = f"spark_audio_{STAMP}.wav"
        write_wav(out, fmt["format_tag"], fmt["channels"], fmt["rate"], fmt["bits"], pcm)
        log(f"   wrote {out} ({len(pcm)} bytes PCM) - play it to confirm it sounds right")
    else:
        log("   payload is not WAV - inspect frames/*.bin (first bytes logged above)")


# --------------------------------------------------------------------------
# rigctl helpers
# --------------------------------------------------------------------------

def rigctl(host, port, cmd, timeout=0.8):
    try:
        with socket.create_connection((host, port), timeout=2) as s:
            s.sendall((cmd + "\n").encode())
            s.settimeout(timeout)
            data = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
            return data.decode(errors="replace").strip()
    except OSError as e:
        return f"!! {e}"


def rigctl_probe(host, port, do_ptt):
    banner(f"RIGCTL on {host}:{port}")
    for cmd, what in [("f", "get freq"), ("m", "get mode/passband"), ("v", "get VFO"),
                      ("t", "get PTT"), ("s", "get split"), ("\\get_powerstat", "power state"),
                      ("\\chk_vfo", "chk_vfo")]:
        log(f">> {cmd:<16} ({what})")
        log(f"<< {rigctl(host, port, cmd)!r}")
    if do_ptt:
        log("!! PTT TEST: keying for ~1 s. Use a dummy load / minimum power.")
        log(f">> T 1  -> {rigctl(host, port, 'T 1')!r}")
        time.sleep(0.3)
        log(f">> t    -> {rigctl(host, port, 't')!r}")
        time.sleep(0.7)
        log(f">> T 0  -> {rigctl(host, port, 'T 0')!r}")
        log(f">> t    -> {rigctl(host, port, 't')!r}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def main(a):
    url = f"ws://{a.host}:{a.port}{a.path}"
    banner(f"CONNECT {url}")
    try:
        ws = await websockets.connect(url, max_size=None, ping_interval=20, open_timeout=5)
    except Exception as e:
        log(f"!! WebSocket connect failed: {e!r}")
        log("   Check WebSockets are enabled in SparkSDR settings, and the port/path.")
        ws = None

    if ws:
        async with ws:
            banner("BASIC QUERIES")
            await request(ws, {"cmd": "getVersion"})
            await request(ws, {"cmd": "getRadios"})
            recv = await request(ws, {"cmd": "getReceivers"})
            r = find_cmd(recv, "getReceiversResponse")
            receivers = r.get("Receivers", []) if r else []
            rx = a.rx if a.rx is not None else (receivers[0]["ID"] if receivers else 0)
            orig = next((x for x in receivers if x.get("ID") == rx), None)
            log(f"   using receiver ID {rx}; original state: {orig}")

            if a.guess:
                banner("UNDOCUMENTED COMMAND GUESSES (read-only)")
                for c in ["getFeatures", "getTransmit", "getPTT", "getTx", "getState",
                          "getTransmitters", "getVfo", "subscribeToReceivers"]:
                    await request(ws, {"cmd": c}, wait=0.8)

            banner(f"PASSIVE WATCH {a.watch}s - NOW tune, change mode, add a receiver, "
                   f"key TX in the SparkSDR UI")
            log("   (any text message below arrived without being requested = push events)")
            end = time.monotonic() + a.watch
            pushed = 0
            while time.monotonic() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=end - time.monotonic())
                except asyncio.TimeoutError:
                    break
                if isinstance(m, str):
                    pushed += 1
                    log(f"<< {m[:500]}")
            log(f"   unsolicited text messages: {pushed} "
                f"({'push events exist' if pushed else 'looks request/response only -> bridge must poll'})")

            if a.write and orig:
                banner("WRITE ROUND-TRIP (setFrequency / setMode, then restore)")
                f0, m0 = int(orig["Frequency"]), orig["Mode"]
                await request(ws, {"cmd": "setFrequency", "ID": rx, "Frequency": f0 + 1000})
                await request(ws, {"cmd": "setMode", "ID": rx, "Mode": "USB" if m0 != "USB" else "LSB"})
                await request(ws, {"cmd": "getReceivers"})
                await request(ws, {"cmd": "setFrequency", "ID": rx, "Frequency": f0})
                await request(ws, {"cmd": "setMode", "ID": rx, "Mode": m0})

            if a.audio > 0:
                await capture_audio(ws, rx, a.audio)

    if a.rigctl_port:
        rigctl_probe(a.host, a.rigctl_port, a.ptt)

    banner("DONE")
    log(f"Log: {os.path.abspath(LOG_PATH)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Probe SparkSDR WebSocket and rigctl interfaces")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=4649, help="WebSocket port (default 4649)")
    p.add_argument("--path", default="/Spark", help="WebSocket path (default /Spark)")
    p.add_argument("--rx", type=int, default=None, help="receiver ID (default: first)")
    p.add_argument("--watch", type=int, default=20, help="seconds to watch for pushed events")
    p.add_argument("--audio", type=int, default=5, help="seconds of audio to capture (0=skip)")
    p.add_argument("--write", action="store_true", help="test setFrequency/setMode and restore")
    p.add_argument("--guess", action="store_true", help="try some undocumented read-only cmds")
    p.add_argument("--rigctl-port", type=int, default=None, help="receiver's rigctl TCP port")
    p.add_argument("--ptt", action="store_true", help="key PTT for ~1 s via rigctl (TRANSMITS)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        log("interrupted")
