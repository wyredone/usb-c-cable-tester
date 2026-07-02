# USB-C Cable Tester GUI

A Windows 10/11 desktop app that infers whether a connected USB-C cable supports **data transfer** (vs charge-only), labels the negotiated **USB generation**, benchmarks **read/write speed**, and surfaces everything through **color-coded status indicators**.

> **How detection works (and its limits):** Windows does not expose raw USB-C cable pin state to software. The app infers cable capability: a data handshake / mounted volume ⇒ *data-capable*; a USB device that only draws power with no data volume ⇒ *charge-only*; a bare cable with no data device on the far end ⇒ *inconclusive*. To judge a cable, plug a **data-capable device** (phone, SSD, flash drive) into the far end.

## Features

### Color-coded status
- **Cable Connected** tile — green (device detected) / red (nothing) 
- **Data Capable** tile — green (data OK) / red (charge-only) / amber (inconclusive)
- **Speed Grade** tile — green (USB 3.x+) / amber (USB 2.0 / slow) / red (test failed)
- **LED-style banner** + **color-matched tray icon**
- Toggleable **audible pass/fail chime**

### Detection & accuracy
- Per-port USB **generation label** (USB 2.0 / 3.x / 3.2 Gen2 / USB4·Thunderbolt) via WMI
- **Far-end device readout**: name + VID/PID
- Best-effort **USB-PD voltage** readout
- Explicit **inconclusive/confidence** state

### Benchmark
- **4K random** + **1M sequential** read/write
- Warm-up pass + **average of 3 runs** (min/avg/max)
- **Auto-scales** test file to free space, **capped ~10s** so it never hangs
- Flags **throttling anomaly** if mid-test speed drops >20%

### Robustness & UX
- Detection fallback chain: WMI `Win32_VolumeChangeEvent` → `psutil` polling → manual **Rescan**
- **Auto-relaunch-as-admin** with amber banner when elevation is missing
- **History table** (timestamp, verdict, speeds) + **CSV export**
- **Dark/Light theme** toggle; remembers window size/position (`usb_c_tester_config.json`)
- **Self-test / calibration** mode using a known-good reference drive
- **Batch mode** for testing several cables in sequence

### Logging / debugging
- **Every launch** writes a fresh log to `logs/usb_c_tester_YYYY-MM-DD_HHMMSS.log`
- Dual sink: human-readable `.log` + structured `.jsonl`
- Captures startup env, global `sys.excepthook` tracebacks, thread exceptions, and all detection/benchmark events
- `--debug` flag echoes log lines to stdout
- Timestamps in **America/Chicago** (MM-DD-YY, HH:MM AM/PM)

## Requirements

- Windows 10/11
- Python 3.11+

Dependencies auto-install on first run (`customtkinter`, `wmi`, `psutil`, `pywin32`, `pystray`, `Pillow`). To install manually:

```powershell
pip install customtkinter psutil wmi pywin32 pystray Pillow
```

## Run

```powershell
python usb_c_tester.py
# with console debug logging:
python usb_c_tester.py --debug
```

## Build a standalone .exe

```powershell
pip install pyinstaller
pyinstaller usb_c_tester.spec
# -> dist/usb_c_tester.exe
```

A **GitHub Actions** workflow (`.github/workflows/build.yml`) auto-builds `dist/usb_c_tester.exe` on push to `main`, on `v*` tags (attached to the release), or via manual dispatch.

## Usage

1. Launch the app (run as admin for full WMI USB event access).
2. Plug a USB-C cable with a **data device** on the far end.
3. Watch the tiles: green = data-capable, red = charge-only, amber = inconclusive.
4. Click **Run Benchmark** to measure read/write speed.
5. Use **Batch Mode** to test multiple cables in sequence; **Export CSV** for records.

## Notes / limitations

- A charge-only cable connected to a charge-only device always reads as charge-only — the app cannot distinguish cable type without a data-capable device on the far end.
- USB generation and PD readouts are best-effort from WMI and depend on driver/vendor exposure.
- Admin privileges are recommended for raw WMI USB event subscriptions.
