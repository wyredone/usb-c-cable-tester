# USB-C Cable Tester GUI

A Windows 10/11 desktop app that infers whether a connected USB-C cable supports **data transfer** (vs charge-only), labels the negotiated **USB generation**, benchmarks **read/write speed**, and surfaces everything through **color-coded status indicators**.

> **How detection works (and its limits):** Windows does not expose raw USB-C cable pin state to software. The app infers cable capability: a data handshake / mounted volume ⇒ *data-capable*; a USB device that only draws power with no data volume ⇒ *charge-only*; a bare cable with no data device on the far end ⇒ *inconclusive*. To judge a cable, plug a **data-capable device** (phone, SSD, flash drive) into the far end.

## Features

### Color-coded status
- **Cable Connected** tile — green (device detected) / red (nothing) 
- **Device Type** tile — Mass Storage / MTP / PTP (camera) / USB (no storage)
- **Data Capable** tile — green (data OK, incl. MTP) / red (charge-only) / amber (PTP-only or inconclusive)
- **Speed Grade** tile — green (USB 3.x+) / amber (USB 2.0 / slow) / red (test failed)
- **LED-style banner** + **color-matched tray icon**
- Toggleable **audible pass/fail chime**

### MTP / PTP device support (Autel KM100, Rockchip tools, phones, cameras)
Some devices — including the **Autel KM100** and **Rockchip-based tools** (VID_2207) — connect over **MTP/PTP (Windows Portable Devices)** instead of USB Mass Storage, so they show up like a phone or camera and never get a drive letter. The app detects these through the **WPD shell namespace**:
- Devices are identified by the shell **Type column** (`Portable Device` / `Camera` / `Mobile Phone`, etc.), so DLNA/UPnP **media servers** (Sonos, NAS), redirected **user folders** (Downloads, Pictures), and **network/local drives** are correctly excluded. A USB shell path (`usb#vid_...`) is a fallback trigger when the Type column is blank.
- A device is treated as **MTP** whenever its content tree is browsable — even if the root is empty or lazily populated (common on Rockchip devices) — instead of requiring visible files. Its **VID/PID** is parsed from the shell path.
- If a device enumerates over **MTP** (browsable content tree), that alone proves the **cable carries data** → Data Capable = green "Data OK (MTP)".
- **PTP/camera mode** (no browsable storage) → amber: the cable works, but switch the device to **MTP / File Transfer** mode for file access.
- Because MTP has no drive letter, the speed test uses a **copy-based benchmark** — it times a real file **push (write)** to the device and **pull (read)** back through Explorer's WPD copy engine, reporting the practical MTP transfer rate.
- **Writable-folder targeting with multi-candidate fallback.** Android/Autel MTP roots usually reject top-level writes, and the storage node's name is padded with invisible bidirectional marks that break name matching. The benchmark descends into the storage tree **by folder structure (not by name)** and then **tries each standard user folder in turn** — **Download → Downloads → Pictures → DCIM → Music → Movies → Documents**, then the storage root — until one **actually accepts** the write (verified by re-listing, not just an API call). The folder that succeeded is shown in the log as the **target folder**; each folder gets a fair slice of the time budget so a stubborn one can't starve the fallback.
- **Read-only fallback that trusts bytes, not the reported size.** If every folder refuses writes, the benchmark measures a **pull-only (read)** throughput from an existing file. MTP frequently **reports `Size = 0` for real, multi-megabyte files**, which previously fooled the app into grabbing a tiny `.config` and reporting a bogus 0 MB/s. The fallback now **collects many candidate files across the whole device tree** (ranking reported-in-band files first, then unknown `Size = 0` files, then the rest) and **actually pulls them one by one**, measuring the **real bytes that land on disk**, until one yields at least **0.5 MB** — enough to time reliably. It never trusts the bogus reported size. This is reported as green **"Data OK (MTP, read-only)"** and never deletes your files (only the app's own pushed test file is cleaned up).
- **Read timing hardened.** Each pull waits on a fine (50 ms) poll for the local copy's size to **stabilise** or reach its target, with a per-file cap so one stuck file can't eat the whole time budget, so even quick copies get an accurate elapsed time.
- **Honest verdicts, never a false red.**
  - Write refused but a real read measured → green **"Data OK (MTP, read-only)"** with the read speed.
  - Data moved but every readable file was under the 0.5 MB measurable floor → green **"Data OK (MTP, confirmed)"** with an honest note that the data path is confirmed but the speed wasn't measurable (no invented numbers).
  - Only if the device refuses writes **and** exposes no readable file at all does it show an **amber, informational** "not measurable" result — still stating the **cable is confirmed data-capable**, not a red failure.
- MTP transfer is inherently slower than raw mass storage; the Speed Grade tile does **not** flag a low MTP rate as a bad cable.

### Automatic connect / disconnect detection
- The app watches continuously and reacts the moment a cable/device is **plugged in or removed** — no manual rescan needed.
- **Two WMI event triggers** run together: `Win32_VolumeChangeEvent` (drive-letter mass storage) **and** `Win32_DeviceChangeEvent` (non-volume USB arrivals/removals such as the MTP KM100, phones, and cameras that never get a drive letter).
- A **guaranteed periodic re-scan (~2s)** runs alongside the event watchers, so MTP/WPD devices that raise no watcher event on some systems are still picked up promptly.
- Each cycle diffs the whole device set (volumes + MTP/PTP) and reports explicit **`connected`** / **`disconnected`** lists in the payload.
- On connect/disconnect the GUI **logs the exact device**, **flashes the banner** (green pulse for connect, red for disconnect), and **chimes** (respecting the Chime toggle). The startup snapshot is silent — already-plugged devices aren't reported as fresh connects.

### Detection & accuracy
- Per-port USB **generation label** (USB 2.0 / 3.x / 3.2 Gen2 / USB4·Thunderbolt) via WMI
- **Far-end device readout**: name + VID/PID, with a **human-readable vendor name** resolved from the VID (built-in table covers Rockchip `2207`, the common Autel VIDs, and ~45 other vendors). Drop a `usb_ids.json` next to the app to add or override vendor names.
- Best-effort **USB-PD voltage** readout
- Explicit **inconclusive/confidence** state

### Benchmark
- **4K random** + **1M sequential** read/write
- Warm-up pass + **average of 3 runs** (min/avg/max)
- **Auto-scales** test file to free space, **capped ~10s** so it never hangs
- Flags **throttling anomaly** if mid-test speed drops >20%
- **Size sweep** (mass storage) — runs the benchmark across **4 / 16 / 64 / 256 MB** transfers and renders a **throughput-vs-size chart** (PNG in `logs/`) so you can see how the cable scales with transfer size.
- **Save Report** — exports a one-page **report card PNG** with the verdict (PASS/FAIL), generation, and measured speeds for record-keeping or sharing.

### Device Browser & Backup
A **Browse / Backup** window (enabled whenever a mass-storage or MTP device is selected) lets you explore and copy the connected device's filesystem:
- **Full recursive scan** of the device tree — **mass storage** (real drive letter, via `os.scandir`) **and MTP** (via the WPD shell namespace), with per-entry sizes. A **Recurse into subfolders** toggle controls whether the scan descends the whole tree or lists only the top level.
- **Checkbox tree** — check individual files or whole folders (a folder check cascades to its children); **Select all** / **Clear** helpers and a live "N files selected (size)" readout.
- **Two directions** — **Device → PC (backup)** pulls the checked items to a destination folder you choose, preserving the folder structure; **PC → Device (restore)** copies a chosen PC folder back to the device.
- **Live progress with ETA** — a progress bar plus a status line showing files done, bytes moved / total, transfer rate, **estimated time remaining**, and the current file.
- **Resumable backups** — every backup writes a JSON **manifest** (`backups/manifest_*.json`) and marks each file done as it completes. If a transfer is interrupted (cancel, unplug, crash), click **Resume backup...**, pick the manifest, and it **skips already-copied files** and finishes the rest. A fully completed backup removes its own manifest.
- **Persistent backup logs** — each run appends a timestamped log to `backups/backup_*.log` (start, per-file OK/ERR, and a final summary), separate from the app's main `logs/`.
- All scanning and copying run on a **worker thread** so the UI stays responsive; MTP work opens its own COM apartment on that thread.

### Device selection
- **Target device picker** — a dropdown lists every connected USB-C device (removable USB storage volumes + MTP/PTP portable devices like the KM100). Pick which one to inspect/benchmark; the tiles, banner, and benchmark target all follow your selection.
- The picker **only lists removable/portable devices** — the system drive (C:) and other fixed/internal disks are excluded, so it never mistakes your boot drive for the cable under test.
- Your selection is preserved across re-detection (stable device keys); Batch Mode clears it so the next cable is auto-selected.

### Robustness & UX
- Detection chain: WMI `Win32_VolumeChangeEvent` + `Win32_DeviceChangeEvent` event triggers, a guaranteed ~2s periodic re-scan, `psutil` polling fallback, and a manual **Rescan** button
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
- **Backup activity** is logged separately under `backups/` (per-run `.log` + resumable `.json` manifests)

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
2. Plug a USB-C cable with a **data device** on the far end — the app **auto-detects the connection** (banner flashes green + chime) and **auto-detects removal** (banner flashes red).
3. Watch the tiles: green = data-capable, red = charge-only, amber = inconclusive.
4. Click **Run Benchmark** to measure read/write speed (or **Size Sweep** on mass storage for a throughput-vs-size chart, then **Save Report** for a one-page PNG).
5. Click **Browse / Backup** to explore the device's files, check what you want, and back it up to your PC (or restore a PC folder to the device) with live ETA and resumable transfers.
6. Use **Batch Mode** to test multiple cables in sequence; **Export CSV** for records.

## Notes / limitations

- Only removable/portable devices are shown as targets. Fixed and network drives (including the C: system drive) are deliberately excluded.
- A charge-only cable connected to a charge-only device always reads as charge-only — the app cannot distinguish cable type without a data-capable device on the far end.
- MTP/PTP detection and the copy-based MTP benchmark use the Windows Portable Devices shell namespace and depend on the device's connection mode. If the KM100 offers a "USB Mass Storage / UMS" mode in its own settings, selecting it gives a normal drive letter and the standard (faster) benchmark path is used automatically.
- USB generation and PD readouts are best-effort from WMI and depend on driver/vendor exposure.
- Admin privileges are recommended for raw WMI USB event subscriptions.
