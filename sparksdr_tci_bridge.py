#!/usr/bin/env python3
"""
sparksdr_tci_bridge.py - make SparkSDR look like a TCI radio (control only).

    CAT4OM (TCI adapter)  <--TCI/WebSocket-->  this bridge  <--JSON/WebSocket-->  SparkSDR
                                                    \\--rigctl TCP (PTT)--/

- SparkSDR receiver ID N is exposed as TCI TRX N, channel 0 (VFO A).
- VFO B (channel 1) and split are held in the bridge (SparkSDR has no VFO B).
- PTT goes to the receiver's SparkSDR rigctl port (configure with --rigctl N=PORT).
- Audio is NOT handled: route SparkSDR audio via a virtual audio cable and pick
  that device in CAT4OM's radio audio settings.

Requirements: Python 3.9+, pip install websockets
Run:          python sparksdr_tci_bridge.py --rigctl 0=51111
Config file:  optional JSON (see --write-config) for mode maps and ports.
"""

import argparse
import asyncio
import json
import logging
import sys

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

VERSION = "0.0.1-alpha"
APP_TITLE = f"SparkSDR2CAT4OM alpha {VERSION.split('-')[0]}"
log = logging.getLogger("bridge")

DEFAULT_CONFIG = {
    "sparkUrl": "ws://localhost:4649/Spark",
    "tciHost": "0.0.0.0",
    "tciPort": 40001,
    "deviceName": "SparkSDR",
    "protocolLine": "ExpertSDR3,1.9",
    # "upper" matches the ModeMap keys in sparksdr-tci.xml; "lower" is TCI-spec style
    "modeCase": "upper",
    "rigctlHost": "localhost",
    "rigctlPorts": {},            # {"0": 51111, "1": 51112}
    "vfoLimits": [10000, 2000000000],
    "ifLimits": [-48000, 48000],
    # TCI modulation -> SparkSDR mode name (used for SET and for MODULATIONS_LIST)
    "tciToSpark": {
        "lsb": "LSB", "usb": "USB", "cw": "CW", "am": "AM",
        "nfm": "FM", "digl": "DigiL", "digu": "DigiU",
    },
    # SparkSDR mode name -> TCI modulation (case-insensitive; unknown -> fallback)
    "sparkToTci": {
        "LSB": "lsb", "USB": "usb", "CW": "cw", "CWL": "cw", "CWU": "cw",
        "AM": "am", "SAM": "am", "FM": "nfm", "NFM": "nfm",
        "DigiL": "digl", "DigiU": "digu",
        "FT8": "digu", "FT4": "digu", "WSPR": "digu", "FST4": "digu",
        "FST4W": "digu", "JS8": "digu",
    },
    "unknownModeFallback": "usb",
}


class RxState:
    def __init__(self, rid):
        self.id = rid
        self.freq = None
        self.mode = None          # SparkSDR name
        self.low = None
        self.high = None
        self.vfo_b = None
        self.split = False
        self.ptt = False
        self.rx_ch1 = False


class RigCtl:
    """Minimal persistent rigctl client for PTT."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.reader = self.writer = None
        self.lock = asyncio.Lock()

    async def _ensure(self):
        if self.writer is None or self.writer.is_closing():
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), 3)

    async def cmd(self, line):
        async with self.lock:
            try:
                await self._ensure()
                self.writer.write((line + "\n").encode())
                await self.writer.drain()
                reply = await asyncio.wait_for(self.reader.readline(), 2)
                return reply.decode(errors="replace").strip()
            except Exception as e:
                if self.writer:
                    self.writer.close()
                self.writer = None
                return f"ERR {e!r}"


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rx = {}                      # id -> RxState
        self.clients = set()
        self.spark = None
        self.spark_ready = asyncio.Event()
        self.radio_name = None
        self.warned_modes = set()
        self.rigctl = {int(k): RigCtl(cfg["rigctlHost"], int(v))
                       for k, v in cfg["rigctlPorts"].items()}
        self.t2s = {k.lower(): v for k, v in cfg["tciToSpark"].items()}
        self.s2t = {k.lower(): v.lower() for k, v in cfg["sparkToTci"].items()}

    # ---------------- mode mapping ----------------
    def tci_mode(self, m):
        return m.upper() if self.cfg["modeCase"] == "upper" else m.lower()

    def spark_to_tci(self, spark_mode):
        t = self.s2t.get((spark_mode or "").lower())
        if t is None:
            if spark_mode not in self.warned_modes:
                self.warned_modes.add(spark_mode)
                log.warning("Unmapped SparkSDR mode %r -> %s (add it to sparkToTci)",
                            spark_mode, self.cfg["unknownModeFallback"])
            t = self.cfg["unknownModeFallback"]
        return self.tci_mode(t)

    # ---------------- TCI output ----------------
    async def send(self, ws, text):
        try:
            await ws.send(text)
            log.debug("TCI -> %s", text)
        except Exception:
            pass

    async def broadcast(self, text):
        log.debug("TCI => %s", text)
        for ws in list(self.clients):
            await self.send(ws, text)

    def trx_count(self):
        return (max(self.rx) + 1) if self.rx else 1

    def rx_or_new(self, trx):
        return self.rx.setdefault(trx, RxState(trx))

    def state_lines(self, r):
        f = r.freq or 0
        out = [f"DDS:{r.id},{f};", f"IF:{r.id},0,0;", f"IF:{r.id},1,0;",
               f"VFO:{r.id},0,{f};", f"VFO:{r.id},1,{r.vfo_b or f};",
               f"MODULATION:{r.id},{self.spark_to_tci(r.mode)};",
               f"RX_ENABLE:{r.id},true;",
               f"RX_CHANNEL_ENABLE:{r.id},1,{str(r.rx_ch1).lower()};",
               f"SPLIT_ENABLE:{r.id},{str(r.split).lower()};",
               f"TRX:{r.id},{str(r.ptt).lower()};", f"TUNE:{r.id},false;"]
        if r.low is not None:
            out.append(f"RX_FILTER_BAND:{r.id},{int(r.low)},{int(r.high)};")
        return out

    async def handshake(self, ws):
        # give SparkSDR a moment so the first client sees real state
        try:
            await asyncio.wait_for(self.spark_ready.wait(), 3)
        except asyncio.TimeoutError:
            log.warning("SparkSDR not connected yet; sending placeholder state")
        c = self.cfg
        modes = ",".join(self.tci_mode(m) for m in self.t2s)
        lines = [f"PROTOCOL:{c['protocolLine']};", f"DEVICE:{c['deviceName']};",
                 f"RECEIVE_ONLY:{'false' if self.rigctl else 'true'};",
                 f"TRX_COUNT:{self.trx_count()};", "CHANNELS_COUNT:2;",
                 f"VFO_LIMITS:{c['vfoLimits'][0]},{c['vfoLimits'][1]};",
                 f"IF_LIMITS:{c['ifLimits'][0]},{c['ifLimits'][1]};",
                 f"MODULATIONS_LIST:{modes};"]
        for rid in range(self.trx_count()):
            lines += self.state_lines(self.rx_or_new(rid))
        lines += ["START;", "READY;"]
        for ln in lines:
            await self.send(ws, ln)

    # ---------------- SparkSDR side ----------------
    async def spark_send(self, obj):
        if self.spark is None:
            log.warning("SparkSDR not connected; dropped %s", obj)
            return
        txt = json.dumps(obj)
        log.debug("SPARK -> %s", txt)
        try:
            await self.spark.send(txt)
        except Exception as e:
            log.warning("SparkSDR send failed: %r", e)

    async def apply_receiver(self, d):
        rid = int(d["ID"])
        r = self.rx_or_new(rid)
        freq = int(round(float(d.get("Frequency", r.freq or 0))))
        mode = d.get("Mode", r.mode)
        low, high = d.get("FilterLow", r.low), d.get("FilterHigh", r.high)
        if freq != r.freq:
            r.freq = freq
            await self.broadcast(f"DDS:{rid},{freq};")
            await self.broadcast(f"VFO:{rid},0,{freq};")
        if mode != r.mode:
            r.mode = mode
            await self.broadcast(f"MODULATION:{rid},{self.spark_to_tci(mode)};")
        if (low, high) != (r.low, r.high) and low is not None:
            r.low, r.high = low, high
            await self.broadcast(f"RX_FILTER_BAND:{rid},{int(low)},{int(high)};")

    async def handle_spark(self, msg):
        try:
            d = json.loads(msg)
        except json.JSONDecodeError:
            return
        cmd = d.get("cmd", "")
        if cmd == "ReceiverResponse":
            await self.apply_receiver(d)
        elif cmd == "getReceiversResponse":
            ids = set()
            for item in d.get("Receivers", []):
                ids.add(int(item["ID"]))
                await self.apply_receiver(item)
            gone = set(self.rx) - ids
            for g in gone:
                log.info("SparkSDR receiver %d removed", g)
                self.rx.pop(g, None)
            if not self.spark_ready.is_set():
                log.info("SparkSDR receivers: %s", sorted(ids))
                self.spark_ready.set()
        elif cmd == "getRadiosResponse":
            radios = d.get("Radios", [])
            self.radio_name = radios[0]["Name"] if radios else None
            log.info("SparkSDR radios: %s", [(x.get("ID"), x.get("Name"), x.get("Running")) for x in radios])
        elif cmd == "getVersionResponse":
            log.info("SparkSDR %s, WebSocket protocol %s", d.get("HostVersion"), d.get("ProtocolVersion"))

    async def spark_loop(self):
        url = self.cfg["sparkUrl"]
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=20,
                                              open_timeout=5) as ws:
                    self.spark = ws
                    log.info("Connected to SparkSDR at %s", url)
                    for c in ("getVersion", "getRadios", "getReceivers"):
                        await self.spark_send({"cmd": c})
                    async for msg in ws:
                        if isinstance(msg, str):
                            log.debug("SPARK <- %s", msg)
                            await self.handle_spark(msg)
            except Exception as e:
                log.warning("SparkSDR connection: %r - retrying in 3 s", e)
            finally:
                self.spark = None
                self.spark_ready.clear()
                await self.release_all_ptt("SparkSDR disconnected")
            await asyncio.sleep(3)

    # ---------------- PTT ----------------
    async def set_ptt(self, trx, on):
        r = self.rx_or_new(trx)
        rc = self.rigctl.get(trx)
        if rc is None:
            log.warning("PTT %s on TRX %d ignored: no rigctl port configured", on, trx)
            await self.broadcast(f"TRX:{trx},false;")
            return
        reply = await rc.cmd(f"T {1 if on else 0}")
        ok = reply.startswith("RPRT 0")
        log.info("PTT %s TRX %d -> rigctl %r", "ON" if on else "OFF", trx, reply)
        r.ptt = on if ok else False
        await self.broadcast(f"TRX:{trx},{str(r.ptt).lower()};")

    async def release_all_ptt(self, why):
        for trx, r in self.rx.items():
            if r.ptt:
                log.warning("Releasing PTT on TRX %d (%s)", trx, why)
                await self.set_ptt(trx, False)

    # ---------------- TCI input ----------------
    async def handle_tci(self, ws, raw):
        name, _, argstr = raw.partition(":")
        name = name.strip().upper()
        args = [a.strip() for a in argstr.split(",")] if argstr else []

        def trx():
            return int(args[0]) if args else 0

        if name == "VFO" and len(args) >= 2:
            t, ch = trx(), int(args[1])
            r = self.rx_or_new(t)
            if len(args) >= 3:
                f = int(float(args[2]))
                if ch == 0:
                    await self.spark_send({"cmd": "setFrequency", "ID": t, "Frequency": f})
                else:
                    r.vfo_b = f
                    await self.broadcast(f"VFO:{t},1,{f};")
            else:
                f = r.freq if ch == 0 else (r.vfo_b or r.freq)
                await self.send(ws, f"VFO:{t},{ch},{f or 0};")
        elif name == "DDS":
            r = self.rx_or_new(trx())
            if len(args) >= 2:
                log.info("DDS set ignored (SparkSDR has no separate centre): %s", raw)
            await self.send(ws, f"DDS:{r.id},{r.freq or 0};")
        elif name == "MODULATION":
            t = trx()
            r = self.rx_or_new(t)
            if len(args) >= 2:
                sm = self.t2s.get(args[1].lower())
                if sm is None:
                    log.warning("Unsupported TCI modulation %r", args[1])
                    await self.send(ws, f"MODULATION:{t},{self.spark_to_tci(r.mode)};")
                elif sm.lower() == (r.mode or "").lower():
                    await self.send(ws, f"MODULATION:{t},{self.spark_to_tci(r.mode)};")
                else:
                    await self.spark_send({"cmd": "setMode", "ID": t, "Mode": sm})
            else:
                await self.send(ws, f"MODULATION:{t},{self.spark_to_tci(r.mode)};")
        elif name == "TRX":
            t = trx()
            if len(args) >= 2:
                await self.set_ptt(t, args[1].lower() == "true")
            else:
                await self.send(ws, f"TRX:{t},{str(self.rx_or_new(t).ptt).lower()};")
        elif name == "SPLIT_ENABLE":
            t = trx()
            r = self.rx_or_new(t)
            if len(args) >= 2:
                r.split = args[1].lower() == "true"
                await self.broadcast(f"SPLIT_ENABLE:{t},{str(r.split).lower()};")
            else:
                await self.send(ws, f"SPLIT_ENABLE:{t},{str(r.split).lower()};")
        elif name == "RX_CHANNEL_ENABLE" and len(args) >= 2:
            t, ch = trx(), int(args[1])
            r = self.rx_or_new(t)
            if len(args) >= 3 and ch == 1:
                r.rx_ch1 = args[2].lower() == "true"
            en = "true" if ch == 0 else str(r.rx_ch1).lower()
            await self.broadcast(f"RX_CHANNEL_ENABLE:{t},{ch},{en};")
        elif name == "RX_FILTER_BAND":
            r = self.rx_or_new(trx())
            if len(args) >= 3:
                log.info("Filter set not supported by SparkSDR WebSocket: %s", raw)
            if r.low is not None:
                await self.send(ws, f"RX_FILTER_BAND:{r.id},{int(r.low)},{int(r.high)};")
        elif name in ("START", "STOP"):
            await self.spark_send({"cmd": "setRunning", "ID": 0, "Running": name == "START"})
            await self.broadcast(f"{name};")
        elif name == "TUNE":
            t = trx()
            if len(args) >= 2 and args[1].lower() == "true":
                log.warning("TUNE not supported; ignoring")
            await self.send(ws, f"TUNE:{t},false;")
        elif name in ("TRX_COUNT", "DEVICE", "PROTOCOL", "MODULATIONS_LIST"):
            await self.handshake(ws)
        else:
            log.info("Unsupported TCI command ignored: %s", raw)

    async def tci_handler(self, ws, *_):
        peer = getattr(ws, "remote_address", "?")
        log.info("TCI client connected: %s", peer)
        self.clients.add(ws)
        try:
            await self.handshake(ws)
            async for msg in ws:
                if not isinstance(msg, str):
                    continue          # TX audio etc. not supported
                for part in msg.split(";"):
                    part = part.strip()
                    if part:
                        log.debug("TCI <- %s;", part)
                        try:
                            await self.handle_tci(ws, part)
                        except Exception as e:
                            log.warning("Bad TCI command %r: %r", part, e)
        except Exception as e:
            log.info("TCI client error: %r", e)
        finally:
            self.clients.discard(ws)
            log.info("TCI client disconnected: %s", peer)
            if not self.clients:
                await self.release_all_ptt("last TCI client disconnected")

    async def run(self):
        c = self.cfg
        async with websockets.serve(self.tci_handler, c["tciHost"], c["tciPort"],
                                    max_size=None, ping_interval=20):
            log.info("%s listening on ws://%s:%d", APP_TITLE,
                     c["tciHost"], c["tciPort"])
            if c["rigctlPorts"]:
                log.info("PTT via rigctl: %s", c["rigctlPorts"])
            else:
                log.info("No rigctl ports configured: RECEIVE_ONLY, PTT disabled")
            await self.spark_loop()


def main():
    p = argparse.ArgumentParser(description="SparkSDR -> TCI bridge for CAT4OM")
    p.add_argument("--config", help="JSON config file (overrides defaults)")
    p.add_argument("--write-config", metavar="FILE", help="write default config and exit")
    p.add_argument("--spark-url")
    p.add_argument("--tci-port", type=int)
    p.add_argument("--tci-host")
    p.add_argument("--rigctl", action="append", default=[], metavar="RX=PORT",
                   help="rigctl port for a receiver, e.g. --rigctl 0=51111 (repeatable)")
    p.add_argument("--mode-case", choices=["upper", "lower"])
    p.add_argument("-v", "--verbose", action="store_true", help="log every message")
    p.add_argument("--log-file", default="sparksdr_tci_bridge.log")
    p.add_argument("--version", action="version", version=APP_TITLE)
    a = p.parse_args()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(APP_TITLE)
        except Exception:
            pass

    if a.write_config:
        with open(a.write_config, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"wrote {a.write_config}")
        return

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if a.config:
        with open(a.config) as f:
            cfg.update(json.load(f))
    if a.spark_url: cfg["sparkUrl"] = a.spark_url
    if a.tci_port: cfg["tciPort"] = a.tci_port
    if a.tci_host: cfg["tciHost"] = a.tci_host
    if a.mode_case: cfg["modeCase"] = a.mode_case
    for item in a.rigctl:
        rx, _, port = item.partition("=")
        cfg["rigctlPorts"][str(int(rx))] = int(port)

    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers = [logging.StreamHandler()]
    if a.log_file:
        handlers.append(logging.FileHandler(a.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format=fmt, handlers=handlers)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(Bridge(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # keep the window open when started by double-click from the .exe
        print(f"\nFatal error: {e!r}")
        if getattr(sys, "frozen", False):
            input("Press Enter to close...")
        raise
