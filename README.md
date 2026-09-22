# SparkSDR2CAT4OM

A small bridge that lets [CAT4OM](https://www.cat4om.com/) control [SparkSDR](https://www.sparksdr.com/) by making SparkSDR look like a TCI radio.

```
CAT4OM (TCI radio profile) <── TCI / WebSocket ──> sparksdr_tci_bridge.py <── JSON / WebSocket ──> SparkSDR
                                                             └──── Hamlib rigctl / TCP (PTT) ────┘
```

SparkSDR has no TCI interface of its own. It does have two other interfaces:

- a JSON WebSocket API, used for frequency, mode, filter and receivers
- a Hamlib NET rigctl port for each receiver, used for PTT

CAT4OM already supports TCI radios (ExpertSDR/SunSDR style) through handbook files. The bridge translates between the two. CAT4OM only needs the included handbook, `sparksdr-tci.xml`.

> **Status: early beta (v0.1.0).** Tested with SparkSDR 2.0.991.0 (WebSocket protocol 0.1.4), an AirspyHF+ (receive only) and CAT4OM. Frequency and mode control work in both directions. PTT is implemented but hasn't been tested on a transmit-capable radio yet.

## What it does

- Each SparkSDR receiver appears as a TCI transceiver (TRX): receiver ID *N* = TRX *N*, channel 0 (VFO A).
- **Frequency and mode** are synchronised in both directions. Changes made in the SparkSDR window are sent to CAT4OM straight away, because SparkSDR pushes them. There's no polling.
- **PTT:** TCI `TRX:n,true/false` is sent to that receiver's SparkSDR rigctl port as `T 1` / `T 0`.
- **VFO B and split** are held in the bridge, because SparkSDR has no VFO B.
- **Filter:** the passband from SparkSDR is reported to CAT4OM (`RX_FILTER_BAND`). Setting it from CAT4OM isn't possible, because the SparkSDR WebSocket has no command for it.
- **Start/Stop:** mapped to SparkSDR's `setRunning`.
- SparkSDR's repeated duplicate updates are filtered out before they reach TCI clients.
- **Safety:** PTT is released if the last TCI client disconnects or the link to SparkSDR drops.
- The bridge reconnects to SparkSDR automatically.

### What it doesn't do (yet)

- **Audio.** CAT4OM takes radio audio from a Windows audio device, not over TCI, so the bridge handles control only. Use a virtual audio cable (see *Audio* below).
- S-meter, power and SWR telemetry. The SparkSDR WebSocket doesn't provide these.
- CW keying, TUNE, RIT/XIT, drive, AGC, squelch. The handbook leaves these out so CAT4OM doesn't offer them.

## Files

| File | Purpose |
| --- | --- |
| `sparksdr_tci_bridge.py` | The bridge (Python 3, one file). |
| `sparksdr-tci.xml` | CAT4OM radio handbook for the bridge, based on `sunsdr-tci.xml`. |
| `LICENSE` | GNU GPL v3. |

## Requirements

- Python 3.9 or later
- `pip install websockets`
- SparkSDR with **WebSockets enabled** in its settings (default port `4649`). SparkSDR's separate "Remote Server" port isn't used.
- CAT4OM with TCI radio support
- A virtual audio cable (e.g. VB-Audio Virtual Cable) if you want audio in CAT4OM

## Quick start

1. **SparkSDR:** enable WebSockets, start the radio, and create the receiver(s) you want to control.
2. **Bridge:**
   ```
   python sparksdr_tci_bridge.py
   ```
   For PTT, give each receiver's rigctl port. SparkSDR shows it on the receiver.
   ```
   python sparksdr_tci_bridge.py --rigctl 0=51111
   ```
3. **CAT4OM:**
   - Copy `sparksdr-tci.xml` into CAT4OM's handbook folder.
   - Add a radio using the **SparkSDR TCI Bridge** handbook, with TCI host `localhost`, port `40001`, and `trxIndex` set to the SparkSDR receiver ID (usually `0`).
   - Set the radio's audio input to the virtual cable that SparkSDR's audio output goes to.
4. Start the CAT4OM group. The bridge window should show `TCI client connected`.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--spark-url URL` | `ws://localhost:4649/Spark` | SparkSDR WebSocket address |
| `--tci-host HOST` | `0.0.0.0` | Address the TCI server listens on |
| `--tci-port PORT` | `40001` | TCI server port |
| `--rigctl RX=PORT` | none | rigctl port for a receiver (repeatable). With no ports, the bridge reports `RECEIVE_ONLY` and ignores PTT. |
| `--mode-case upper\|lower` | `upper` | Case of TCI mode names. `upper` matches the handbook's ModeMap. |
| `--config FILE` | none | Load settings from a JSON file |
| `--write-config FILE` | none | Write the default settings to a JSON file and exit |
| `-v`, `--verbose` | off | Log every message in both directions |
| `--log-file FILE` | `sparksdr_tci_bridge.log` | Log file (also printed to the console) |

### Config file and mode mapping

`--write-config bridge.json` writes all settings, including the two mode tables:

- `tciToSpark` maps a TCI mode (sent by CAT4OM) to a SparkSDR mode name. Its keys also make up the `MODULATIONS_LIST` sent to CAT4OM.
- `sparkToTci` maps a SparkSDR mode name to a TCI mode. SparkSDR decoder modes (FT8, FT4, WSPR…) are reported as `digu`.

Only `USB` and `DigiU` have been confirmed as SparkSDR names so far. The others (`LSB`, `CW`, `AM`, `FM`, `DigiL`) are best guesses. The bridge logs a warning for any SparkSDR mode it doesn't recognise, so switch through the modes once and add any missing names to the config.

If you add a mode, add the same TCI key to the `ModeMap` in `sparksdr-tci.xml`.

## Audio

CAT4OM's TCI profile uses Windows audio devices for radio audio:

- **RX:** set SparkSDR's audio output to a virtual cable, then pick that cable as the radio's audio input in CAT4OM.
- **TX (untested):** set CAT4OM's audio output for the radio to a second virtual cable, and use that as SparkSDR's TX/mic input.

## TCI details

On connection the bridge sends:

```
PROTOCOL:ExpertSDR3,1.9; DEVICE:SparkSDR; RECEIVE_ONLY:…; TRX_COUNT:n; CHANNELS_COUNT:2;
VFO_LIMITS:…; IF_LIMITS:…; MODULATIONS_LIST:LSB,USB,CW,AM,NFM,DIGL,DIGU;
(per TRX) DDS, IF, VFO, MODULATION, RX_ENABLE, RX_CHANNEL_ENABLE, SPLIT_ENABLE, TRX, TUNE, RX_FILTER_BAND;
START; READY;
```

Commands it handles:

| TCI command | Action |
| --- | --- |
| `VFO:trx,0,f` | SparkSDR `setFrequency` |
| `VFO:trx,1,f` | Stored as VFO B |
| `MODULATION:trx,m` | SparkSDR `setMode` (skipped if unchanged) |
| `TRX:trx,bool` | rigctl `T 1` / `T 0` |
| `SPLIT_ENABLE`, `RX_CHANNEL_ENABLE` | Stored in the bridge and echoed back |
| `DDS`, `RX_FILTER_BAND` | Queries answered; set commands ignored |
| `START` / `STOP` | SparkSDR `setRunning` |
| Anything else | Logged and ignored |

Queries without a value (e.g. `VFO:0,0;`) are answered with the current state.

## SparkSDR WebSocket notes

These were found with a probe script against SparkSDR 2.0.991.0:

- Endpoint: `ws://localhost:4649/Spark`. Messages are JSON with a `cmd` key: `getVersion`, `getRadios`, `getReceivers`, `setFrequency`, `setMode`, `setRunning`, `addReceiver`, `removeReceiver`, `subscribeToAudio`, `subscribeToSpectrum`, `subscribeToSpots`.
- Changes made in the SparkSDR window (tuning, mode, adding or removing receivers) are pushed unsolicited as `ReceiverResponse` / `getReceiversResponse`. They often arrive as repeated duplicates.
- Setting the mode it's already in gets no reply. Unknown commands are silently ignored. The WebSocket has no PTT or TX commands.
- Audio frames (not used by the bridge): 1-byte type (1 = audio), 4-byte receiver ID, then a complete WAV. The WAV is 16-bit PCM, mono, 48 kHz, 512 samples, about 94 frames per second.
- SparkSDR sends a WebSocket ping about every 60 s. The `websockets` library answers it automatically.

Further reading: [SparkSDR WebSocket API wiki](https://github.com/nricciar/sparksdr-websocket-demo/wiki/WebSocket-API).

## Known limitations

- `TRX_COUNT` is sent only when a client connects. Create the SparkSDR receivers before starting the CAT4OM group, or restart the group after adding one.
- Split is held in the bridge only. On a transmitting radio it isn't yet known which receiver SparkSDR transmits on.
- Filter width can't be set from CAT4OM.

## Roadmap

- [ ] Test PTT and TX audio on a transmit-capable radio (e.g. Hermes Lite 2)
- [ ] Confirm all SparkSDR mode names and finish the mode maps
- [ ] Real split support (VFO B → second receiver / TX frequency)
- [ ] Optional TCI RX/TX audio for TCI clients that support it
- [ ] CW via SparkSDR's cwdaemon emulation
- [ ] Single-file Windows executable (PyInstaller)

## Licence

GNU General Public License v3.0. See [LICENSE](LICENSE).

SparkSDR belongs to its author. This project isn't affiliated with or endorsed by SparkSDR.
