# usb_c_tester.py
"""
USB-C Cable Tester GUI
Windows 10/11 desktop app that infers whether a connected USB-C cable supports
data transfer (vs charge-only), labels the negotiated USB generation, benchmarks
read/write speed, and surfaces everything through color-coded status indicators.

Detection is inference-based: Windows does not expose raw cable pin state, so a
data handshake / mounted volume => data-capable cable; power-only device => charge
only; a bare cable with no data device on the far end => inconclusive.
"""

# ----------------------------------------------------------------------------
# Dependency auto-installer (subprocess pip in try/except)
# ----------------------------------------------------------------------------
import importlib
import subprocess
import sys


def _ensure(pkg_import, pip_name=None):
    pip_name = pip_name or pkg_import
    try:
        return importlib.import_module(pkg_import)
    except Exception:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--upgrade", pip_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return importlib.import_module(pkg_import)
        except Exception:
            return None


customtkinter = _ensure("customtkinter")
psutil = _ensure("psutil")
_ensure("win32api", "pywin32")
wmi = _ensure("wmi")
pystray = _ensure("pystray")
PIL = _ensure("PIL", "Pillow")

# ----------------------------------------------------------------------------
# Standard library
# ----------------------------------------------------------------------------
import ctypes
import json
import os
import queue
import random
import tempfile
import threading
import time
import traceback
import winsound
from datetime import datetime
from tkinter import ttk

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("America/Chicago")
except Exception:
    _TZ = None

if customtkinter is None or PIL is None:
    print("Fatal: required GUI packages could not be installed.")
    sys.exit(1)

import customtkinter as ctk  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

APP_NAME = "USB-C Cable Tester"
APP_VERSION = "1.0.0"
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "usb_c_tester_config.json")
HISTORY_CSV = os.path.join(APP_DIR, "usb_c_tester_history.csv")
MAX_LOGS = 20

# Color palette for status tiles / banner
COLOR = {
    "idle": "#4a4a4a",
    "green": "#1f9d55",
    "green_hi": "#27c06a",
    "red": "#c0392b",
    "red_hi": "#e0503f",
    "amber": "#d38b12",
    "amber_hi": "#f0a52a",
}


# ----------------------------------------------------------------------------
# Time helpers (America/Chicago, MM-DD-YY HH:MM AM/PM)
# ----------------------------------------------------------------------------
def now_ct():
    return datetime.now(_TZ) if _TZ else datetime.now()


def stamp_line():
    return now_ct().strftime("%m-%d-%y, %I:%M %p")


def stamp_file():
    return now_ct().strftime("%Y-%m-%d_%H%M%S")


# ----------------------------------------------------------------------------
# Launch-time error log (dual sink: human .log + structured .jsonl)
# ----------------------------------------------------------------------------
class Logger:
    def __init__(self, debug=False):
        self.debug = debug
        os.makedirs(LOG_DIR, exist_ok=True)
        base = f"usb_c_tester_{stamp_file()}"
        self.log_path = os.path.join(LOG_DIR, base + ".log")
        self.jsonl_path = os.path.join(LOG_DIR, base + ".jsonl")
        self._lock = threading.Lock()
        self._prune()
        self.event("INFO", "logger_init", {"log": self.log_path})

    def _prune(self):
        try:
            files = sorted(
                [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)],
                key=os.path.getmtime,
            )
            logs = [f for f in files if f.endswith((".log", ".jsonl"))]
            while len(logs) > MAX_LOGS * 2:
                old = logs.pop(0)
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass

    def event(self, level, msg, extra=None):
        line = f"[{stamp_line()}] [{level}] {msg}"
        if extra:
            line += " | " + json.dumps(extra, default=str)
        rec = {
            "ts": now_ct().isoformat(),
            "level": level,
            "msg": msg,
            "extra": extra or {},
        }
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
            except Exception:
                pass
        if self.debug:
            print(line)

    def exc(self, where, err):
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        self.event("ERROR", f"exception in {where}", {"error": str(err), "traceback": tb})


# ----------------------------------------------------------------------------
# Admin / elevation
# ----------------------------------------------------------------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    try:
        params = " ".join([f'"{a}"' for a in sys.argv])
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Config persistence
# ----------------------------------------------------------------------------
def load_config():
    default = {
        "theme": "Dark",
        "geometry": "980x760",
        "chime": True,
        "test_size_mb": 256,
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            default.update(json.load(f))
    except Exception:
        pass
    return default


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# USB / volume detection (WMI event -> psutil poll -> manual rescan)
# ----------------------------------------------------------------------------
class DetectionEngine(threading.Thread):
    """Watches for volume/USB changes and reports events onto a queue."""

    # How often to force a full re-scan (MTP + volumes) even when no volume
    # event fires. MTP/WPD devices (KM100, phones) never raise
    # Win32_VolumeChangeEvent, so without this they'd only be seen on startup
    # or a manual rescan. 2s keeps connect/disconnect feeling instant.
    POLL_INTERVAL = 2.0

    def __init__(self, out_queue, logger):
        super().__init__(daemon=True)
        self.q = out_queue
        self.log = logger
        self._stop = threading.Event()
        self._known = set()          # removable volume device keys
        self._known_mtp = tuple()    # sorted MTP/PTP device-name signature
        # Human-readable label per device key, for connect/disconnect logging.
        self._known_labels = {}

    def stop(self):
        self._stop.set()

    def snapshot_volumes(self):
        """Return only REMOVABLE volumes (USB storage), keyed by mountpoint.

        Every fixed disk on Windows reports 'rw' in opts, so filtering on 'rw'
        wrongly matched the C: system drive. We now include a volume only when
        it is genuinely removable -- detected via the Win32 drive type
        (DRIVE_REMOVABLE = 2) with a psutil 'removable' opts fallback -- and we
        always exclude the system drive.
        """
        vols = {}
        if psutil is None:
            return vols
        system_drive = (os.environ.get("SystemDrive", "C:") + "\\").upper()
        try:
            for p in psutil.disk_partitions(all=False):
                mp = p.mountpoint
                if not mp:
                    continue
                if mp.upper().rstrip("\\") == system_drive.rstrip("\\"):
                    continue  # never treat the system drive as a test target
                opts = (p.opts or "").lower()
                is_removable = "removable" in opts
                # Win32 drive-type check is authoritative when available.
                try:
                    import ctypes as _c
                    dtype = _c.windll.kernel32.GetDriveTypeW(_c.c_wchar_p(mp))
                    # 2 = DRIVE_REMOVABLE, 6 = DRIVE_RAMDISK. Exclude 3 (FIXED),
                    # 4 (REMOTE/network), 5 (CDROM).
                    if dtype == 2:
                        is_removable = True
                    elif dtype in (3, 4, 5):
                        is_removable = False
                except Exception:
                    pass
                if is_removable:
                    vols[p.device] = mp
        except Exception as e:
            self.log.exc("snapshot_volumes", e)
        return vols

    def enumerate_usb(self):
        """Return list of far-end USB device dicts via WMI (VID/PID/name/speed)."""
        devices = []
        if wmi is None:
            return devices
        try:
            import pythoncom

            pythoncom.CoInitialize()
            c = wmi.WMI()
            for dev in c.Win32_PnPEntity():
                did = (dev.DeviceID or "")
                if "USB" not in did.upper():
                    continue
                vid = pid = None
                up = did.upper()
                if "VID_" in up:
                    try:
                        vid = up.split("VID_")[1][:4]
                    except Exception:
                        pass
                if "PID_" in up:
                    try:
                        pid = up.split("PID_")[1][:4]
                    except Exception:
                        pass
                devices.append(
                    {
                        "name": dev.Name or "Unknown",
                        "vid": vid,
                        "pid": pid,
                        "device_id": did,
                    }
                )
        except Exception as e:
            self.log.exc("enumerate_usb", e)
        finally:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
        return devices

    def usb_generation(self):
        """Best-effort negotiated USB generation label from WMI."""
        label = "Unknown"
        if wmi is None:
            return label
        try:
            import pythoncom

            pythoncom.CoInitialize()
            c = wmi.WMI()
            best = 0
            for hub in c.Win32_USBHub():
                name = (hub.Name or "").lower()
                if "3.2" in name or "3.1 gen 2" in name or "10gbps" in name:
                    best = max(best, 3)
                elif "3." in name or "superspeed" in name:
                    best = max(best, 2)
                elif "2.0" in name or "enhanced" in name:
                    best = max(best, 1)
                if "thunderbolt" in name or "usb4" in name:
                    best = max(best, 4)
            label = {
                0: "Unknown",
                1: "USB 2.0",
                2: "USB 3.x (SuperSpeed)",
                3: "USB 3.2 Gen2 (10Gbps)",
                4: "USB4 / Thunderbolt",
            }[best]
        except Exception as e:
            self.log.exc("usb_generation", e)
        finally:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
        return label

    def pd_wattage(self):
        """Best-effort USB-PD / charging wattage where exposed."""
        if wmi is None:
            return None
        try:
            import pythoncom

            pythoncom.CoInitialize()
            c = wmi.WMI()
            for b in c.Win32_Battery():
                if getattr(b, "DesignVoltage", None):
                    return round(float(b.DesignVoltage) / 1000.0, 1)
        except Exception:
            pass
        finally:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
        return None

    # Shell "type" strings (GetDetailsOf column 1) that identify a real
    # portable/USB device. Everything else in "This PC" -- System Folder
    # (redirected user folders), Media Server (DLNA/Sonos/NAS), Local Disk,
    # Network Drive -- is NOT a cable-attached device and must be excluded.
    PORTABLE_TYPES = ("portable device", "portable media player", "camera",
                      "scanner", "mobile phone")

    @staticmethod
    def _vidpid_from_path(path):
        """Pull VID/PID out of a WPD shell path like ...usb#vid_2207&pid_0001#..."""
        vid = pid = None
        up = (path or "").upper()
        if "VID_" in up:
            try:
                vid = up.split("VID_")[1][:4]
            except Exception:
                pass
        if "PID_" in up:
            try:
                pid = up.split("PID_")[1][:4]
            except Exception:
                pass
        return vid, pid

    def enumerate_mtp(self):
        """Enumerate MTP/PTP (Windows Portable Devices) via the Shell namespace.

        MTP/PTP devices (phones, cameras, the Autel KM100, Rockchip tools) never
        receive a drive letter -- Windows exposes them through the WPD shell
        namespace instead, so psutil / Win32_VolumeChangeEvent cannot see them.
        This walks "This PC" and returns portable-device nodes. A device
        enumerating over MTP at all proves the cable carries data.

        Identification uses the shell TYPE column (GetDetailsOf col 1), which
        reads e.g. 'Portable Device' for a phone/KM100/Rockchip and 'Media
        Server' / 'System Folder' / 'Local Disk' for things we must ignore.
        Relying on 'no drive letter' alone wrongly grabbed DLNA media servers
        (Sonos/NAS) and redirected user folders (Downloads, Pictures).

        Returns a list of dicts: {name, type, shell_path, vid, pid}.
        type is 'MTP' when the device exposes a browsable content tree, else
        'PTP' (camera-style, image-only / no browsable storage).
        """
        devices = []
        try:
            import win32com.client
            import pythoncom

            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("Shell.Application")
            # 17 = ssfDRIVES ("This PC"): contains drives AND portable devices.
            this_pc = shell.NameSpace(17)
            if this_pc is None:
                return devices
            for item in this_pc.Items():
                try:
                    path = getattr(item, "Path", "") or ""
                    # A real drive is "<letter>:..." -- path[0] must be a
                    # letter. WPD shell paths start "::{GUID}..." whose
                    # path[1] is also ":", so checking path[1] alone wrongly
                    # excluded portable devices (e.g. Rockchip sm2031).
                    has_drive_letter = (len(path) >= 2 and path[1] == ":"
                                        and path[0].isalpha())
                    if has_drive_letter:
                        continue  # mass-storage volume -- handled elsewhere

                    # Authoritative filter: the shell TYPE column.
                    type_str = ""
                    try:
                        type_str = (this_pc.GetDetailsOf(item, 1) or "").strip()
                    except Exception:
                        type_str = ""
                    tl = type_str.lower()
                    is_portable = any(t in tl for t in self.PORTABLE_TYPES)
                    # Fallback: a WPD/USB shell path is a portable device even if
                    # the type column is blank on some Windows builds.
                    if not is_portable:
                        pl = path.lower()
                        if "usb#vid_" in pl or "wpdbusenum" in pl:
                            is_portable = True
                    if not is_portable:
                        continue  # media server / system folder / network -- skip

                    name = getattr(item, "Name", "Portable Device")
                    vid, pid = self._vidpid_from_path(path)

                    # MTP vs PTP: a device that exposes a browsable content tree
                    # is MTP. Do NOT require populated content -- many devices
                    # (Rockchip, some phones) report an empty/lazy root until
                    # opened, yet are fully browsable = MTP. Only a device whose
                    # folder cannot be obtained at all is treated as PTP/camera.
                    dev_type = "PTP"
                    try:
                        folder = item.GetFolder
                        if folder is not None:
                            dev_type = "MTP"
                    except Exception:
                        dev_type = "PTP"
                    devices.append(
                        {
                            "name": name,
                            "type": dev_type,
                            "shell_path": path,
                            "vid": vid,
                            "pid": pid,
                        }
                    )
                except Exception as e:
                    self.log.exc("enumerate_mtp_item", e)
        except Exception as e:
            self.log.exc("enumerate_mtp", e)
        finally:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
        return devices

    def _mtp_signature(self, mtp_devices):
        return tuple(sorted(d["name"] for d in mtp_devices))

    def run(self):
        # Seed known state WITHOUT emitting so the first _diff_and_emit(force)
        # reports everything already present as an initial "connected" set.
        self._known = set()
        self._known_mtp = tuple()
        self._known_labels = {}
        self.log.event("INFO", "detection_started")
        use_wmi = wmi is not None
        vol_watcher = None
        dev_watcher = None
        if use_wmi:
            try:
                import pythoncom

                pythoncom.CoInitialize()
                c = wmi.WMI()
                # Volume events fire for drive-letter (mass-storage) changes.
                vol_watcher = c.Win32_VolumeChangeEvent.watch_for()
                # Device events ALSO fire for non-volume USB arrivals/removals
                # (MTP/WPD devices such as the KM100, phones, cameras), giving
                # us a fast trigger the volume watcher never sees.
                try:
                    dev_watcher = c.Win32_DeviceChangeEvent.watch_for()
                    self.log.event("INFO", "wmi_device_watcher_active")
                except Exception as de:
                    self.log.exc("wmi_device_watch_init", de)
                    dev_watcher = None
                self.log.event("INFO", "wmi_watcher_active")
            except Exception as e:
                self.log.exc("wmi_watch_init", e)
                use_wmi = False

        # Emit one initial state so the GUI reflects reality on startup.
        self._diff_and_emit(force=True)
        timed_out_exc = getattr(wmi, "x_wmi_timed_out", None) if wmi else None

        def _is_timeout(exc):
            if timed_out_exc and isinstance(exc, timed_out_exc):
                return True
            return "timed_out" in type(exc).__name__.lower()

        last_poll = 0.0
        while not self._stop.is_set():
            try:
                if use_wmi and vol_watcher is not None:
                    event_seen = False
                    # Fast USB device-change trigger (MTP arrivals/removals).
                    if dev_watcher is not None:
                        try:
                            if dev_watcher(timeout_ms=200) is not None:
                                event_seen = True
                        except Exception as we:
                            if not _is_timeout(we):
                                raise
                    # Volume-change trigger (mass-storage drive letters).
                    try:
                        if vol_watcher(timeout_ms=500) is not None:
                            event_seen = True
                    except Exception as we:
                        if not _is_timeout(we):
                            raise

                    now = time.time()
                    # Emit on any event, OR on the guaranteed periodic re-scan
                    # so MTP/WPD devices (invisible to both watchers on some
                    # systems) are still picked up within POLL_INTERVAL.
                    if event_seen or (now - last_poll) >= self.POLL_INTERVAL:
                        self._diff_and_emit()
                        last_poll = now
                else:
                    self._diff_and_emit()
                    time.sleep(self.POLL_INTERVAL)
            except Exception as e:
                self.log.exc("detection_loop", e)
                time.sleep(self.POLL_INTERVAL)

        if use_wmi:
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass
        self.log.event("INFO", "detection_stopped")

    def rescan(self):
        self._diff_and_emit(force=True)

    def _diff_and_emit(self, force=False):
        current = self.snapshot_volumes()
        cur_keys = set(current.keys())
        added = cur_keys - self._known
        removed = self._known - cur_keys
        self._known = cur_keys

        # MTP/WPD devices (KM100, phones, cameras) also count as a change.
        mtp_devices = self.enumerate_mtp()
        mtp_sig = self._mtp_signature(mtp_devices)
        mtp_changed = mtp_sig != self._known_mtp
        self._known_mtp = mtp_sig

        # Build a unified {key: label} map of everything currently present so
        # we can diff the whole device set (volumes + MTP/PTP) and report
        # explicit connect/disconnect events, not just volume-letter changes.
        cur_labels = {}
        for dev, mp in current.items():
            cur_labels["vol:" + str(dev)] = "USB Storage \u2014 {} ({})".format(mp, dev)
        for m in mtp_devices:
            tag = "MTP" if m.get("type") == "MTP" else "PTP/camera"
            cur_labels["mtp:" + m["name"]] = "{} ({})".format(m["name"], tag)

        prev_labels = self._known_labels
        connected_keys = set(cur_labels) - set(prev_labels)
        disconnected_keys = set(prev_labels) - set(cur_labels)
        connected = [cur_labels[k] for k in sorted(connected_keys)]
        disconnected = [prev_labels[k] for k in sorted(disconnected_keys)]
        self._known_labels = cur_labels

        # Emit only on an actual change or an explicit force (initial/rescan).
        # Never emit repeatedly just because nothing is present -- that would
        # flood the queue and re-run WMI/WPD every poll cycle.
        if force or added or removed or mtp_changed or connected or disconnected:
            usb = self.enumerate_usb()
            payload = {
                "volumes": current,
                "added": list(added),
                "removed": list(removed),
                "connected": connected,
                "disconnected": disconnected,
                "usb_devices": usb,
                "mtp_devices": mtp_devices,
                "generation": self.usb_generation(),
                "pd_voltage": self.pd_wattage(),
            }
            if connected:
                self.log.event("INFO", "device_connected", {"devices": connected})
            if disconnected:
                self.log.event("INFO", "device_disconnected", {"devices": disconnected})
            self.q.put(payload)


# ----------------------------------------------------------------------------
# Benchmark (4K random + 1M sequential, warm-up + 3 runs, capped, throttling)
# ----------------------------------------------------------------------------
class Benchmark:
    def __init__(self, logger):
        self.log = logger

    def _free_mb(self, mountpoint):
        try:
            u = psutil.disk_usage(mountpoint)
            return u.free / (1024 * 1024)
        except Exception:
            return 0

    def run(self, mountpoint, target_mb, progress_cb):
        free_mb = self._free_mb(mountpoint)
        size_mb = int(max(16, min(target_mb, free_mb * 0.5)))
        block_1m = 1024 * 1024
        block_4k = 4096
        tmp = os.path.join(mountpoint, f".usb_bench_{stamp_file()}.tmp")
        result = {"size_mb": size_mb, "runs": []}
        deadline = time.time() + 10.0  # cap ~10s
        try:
            # Warm-up pass (1 small write, discarded)
            progress_cb("Warm-up pass...")
            with open(tmp, "wb", buffering=0) as f:
                f.write(os.urandom(block_1m))
                f.flush()
                os.fsync(f.fileno())
            os.remove(tmp)

            write_speeds, read_speeds = [], []
            rand4k_speeds = []
            for i in range(3):
                if time.time() > deadline:
                    self.log.event("WARN", "benchmark_time_cap_hit", {"run": i})
                    break
                progress_cb(f"Sequential run {i + 1}/3 (write)...")
                data = os.urandom(block_1m)
                t0 = time.time()
                written = 0
                with open(tmp, "wb", buffering=0) as f:
                    while written < size_mb * 1024 * 1024:
                        f.write(data)
                        written += block_1m
                        if time.time() > deadline:
                            break
                    f.flush()
                    os.fsync(f.fileno())
                wt = time.time() - t0
                w_mbps = (written / (1024 * 1024)) / wt if wt > 0 else 0
                write_speeds.append(w_mbps)

                progress_cb(f"Sequential run {i + 1}/3 (read)...")
                t0 = time.time()
                read = 0
                with open(tmp, "rb", buffering=0) as f:
                    while True:
                        chunk = f.read(block_1m)
                        if not chunk:
                            break
                        read += len(chunk)
                        if time.time() > deadline:
                            break
                rt = time.time() - t0
                r_mbps = (read / (1024 * 1024)) / rt if rt > 0 else 0
                read_speeds.append(r_mbps)

                # 4K random read sample
                progress_cb(f"4K random run {i + 1}/3...")
                t0 = time.time()
                ops = 0
                fsize = os.path.getsize(tmp)
                with open(tmp, "rb", buffering=0) as f:
                    end = time.time() + 1.0
                    while time.time() < end:
                        off = random.randint(0, max(0, fsize - block_4k))
                        f.seek(off)
                        f.read(block_4k)
                        ops += 1
                dt = time.time() - t0
                rnd_mbps = (ops * block_4k / (1024 * 1024)) / dt if dt > 0 else 0
                rand4k_speeds.append(rnd_mbps)

                result["runs"].append(
                    {"write": w_mbps, "read": r_mbps, "rand4k": rnd_mbps}
                )

            def stats(v):
                return (
                    {"min": min(v), "avg": sum(v) / len(v), "max": max(v)}
                    if v
                    else {"min": 0, "avg": 0, "max": 0}
                )

            result["write"] = stats(write_speeds)
            result["read"] = stats(read_speeds)
            result["rand4k"] = stats(rand4k_speeds)

            # Throttling anomaly: mid-test drop > 20%
            anomaly = None
            if len(write_speeds) >= 2 and write_speeds[0] > 0:
                drop = (write_speeds[0] - min(write_speeds)) / write_speeds[0]
                if drop > 0.20:
                    anomaly = f"Write speed dropped {drop * 100:.0f}% mid-test (possible throttling)."
            result["anomaly"] = anomaly
            self.log.event("INFO", "benchmark_complete", result)
            return result
        except Exception as e:
            self.log.exc("benchmark", e)
            result["error"] = str(e)
            return result
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception as e:
                self.log.exc("benchmark_cleanup", e)

    # Max seconds to wait for a pushed file to appear before treating the write
    # as refused (leaves budget for the read-only fallback). Overridable.
    MTP_WRITE_WAIT_S = 12.0

    # Preferred writable subfolders on an Android/MTP device, in priority
    # order. Android MTP roots often reject writes at the top level but accept
    # them in these standard user folders (confirmed writable via Explorer).
    MTP_WRITE_DIRS = ("Download", "Downloads", "Pictures", "DCIM", "Music",
                      "Movies", "Documents")

    def _mtp_first_storage(self, device_folder):
        """Return the device's storage folder, descending one level if the
        device root just contains a single storage node (e.g. Android's
        'Internal shared storage', whose name is padded with bidi marks).

        Returns (folder, name) or (device_folder, "<root>") if no descent.
        """
        try:
            items = device_folder.Items()
        except Exception:
            return device_folder, "<root>"
        # If there's exactly one child and it's a folder, that's the storage
        # root -- descend into it. Match on IsFolder, NOT on name (the name is
        # wrapped in invisible LTR/RTL marks and is not reliably comparable).
        try:
            if items.Count == 1 and getattr(items.Item(0), "IsFolder", False):
                node = items.Item(0)
                sub = node.GetFolder
                if sub is not None:
                    return sub, "storage"
        except Exception:
            pass
        return device_folder, "<root>"

    def _mtp_write_candidates(self, device_folder):
        """Return an ORDERED list of (folder, human_path) candidates to try a
        test write into, best first. Android/Autel devices reject writes in
        some folders and accept them in others, so run_mtp walks this list
        until one actually accepts the file rather than trusting a single pick.
        """
        storage, _ = self._mtp_first_storage(device_folder)
        candidates = []
        seen = set()

        def _add(folder, label):
            if folder is None:
                return
            key = id(folder)
            if key in seen:
                return
            seen.add(key)
            candidates.append((folder, label))

        # Preferred user folders inside storage, in priority order.
        try:
            names = {}
            for it in storage.Items():
                try:
                    if getattr(it, "IsFolder", False):
                        names[getattr(it, "Name", "").strip()] = it
                except Exception:
                    pass
            for want in self.MTP_WRITE_DIRS:
                it = names.get(want)
                if it is not None:
                    try:
                        sub = it.GetFolder
                        if sub is not None:
                            _add(sub, want)
                    except Exception:
                        pass
        except Exception:
            pass

        # Then the storage root itself, then the device root, as last resorts.
        _add(storage, "storage root")
        _add(device_folder, "device root")
        return candidates

    def _resolve_mtp_write_folder(self, device_folder):
        """Backward-compatible single-pick helper: returns the first write
        candidate (folder, human_path).
        """
        cands = self._mtp_write_candidates(device_folder)
        if cands:
            return cands[0]
        storage, _ = self._mtp_first_storage(device_folder)
        return storage, "storage root"

    # Ideal size band for a pull-only read benchmark: big enough to time
    # accurately, small enough to finish fast.
    MTP_READ_MIN_BYTES = 1 * 1024 * 1024        # 1 MB
    MTP_READ_MAX_BYTES = 128 * 1024 * 1024      # 128 MB

    def _find_existing_file(self, folder, deadline, depth=0, maxdepth=4,
                            _best=None):
        """Depth-first search for the BEST existing readable file on the device
        for a pull-only (read) benchmark when writes are refused.

        MTP frequently reports Size==0 for uncached entries, and tiny files
        (e.g. a '.config') read in well under the timer resolution and produce
        a bogus 0 MB/s. So instead of returning the first file we see, we scan
        the tree and keep the LARGEST file, strongly preferring one inside the
        ideal size band. Returns the best shell item found, or None.

        _best is an internal [item, size, in_band] accumulator shared across
        the recursion.
        """
        if _best is None:
            _best = [None, -1, False]
        if time.time() > deadline or depth > maxdepth:
            return _best[0]
        try:
            items = folder.Items()
        except Exception:
            return _best[0]
        subfolders = []
        for it in items:
            if time.time() > deadline:
                break
            try:
                if getattr(it, "IsFolder", False):
                    subfolders.append(it)
                    continue
                sz = 0
                try:
                    sz = int(getattr(it, "Size", 0) or 0)
                except Exception:
                    sz = 0
                in_band = self.MTP_READ_MIN_BYTES <= sz <= self.MTP_READ_MAX_BYTES
                # Ranking: an in-band file always beats a not-in-band file;
                # within the same class, bigger (capped at max) is better. A
                # size-0/unknown file is only a last resort.
                better = False
                cand_sz = sz
                if in_band and not _best[2]:
                    better = True
                elif in_band and _best[2] and sz > _best[1]:
                    better = True
                elif (not in_band) and (not _best[2]):
                    cand_sz = sz if sz <= self.MTP_READ_MAX_BYTES else 0
                    if cand_sz > _best[1]:
                        better = True
                if better:
                    _best[0], _best[1], _best[2] = it, cand_sz, in_band
                # Early exit: a solidly in-band file is good enough.
                if in_band:
                    return _best[0]
            except Exception:
                continue
        for sf in subfolders:
            if time.time() > deadline:
                break
            try:
                child = sf.GetFolder
                if child is not None:
                    self._find_existing_file(child, deadline, depth + 1,
                                             maxdepth, _best)
                    if _best[2]:   # found an in-band file somewhere below
                        return _best[0]
            except Exception:
                continue
        return _best[0]

    def run_mtp(self, mtp_name, target_mb, progress_cb):
        """Copy-based transfer benchmark for an MTP device (Autel/Rockchip,
        phones, etc.).

        MTP devices have no drive letter, so a raw file benchmark is impossible.
        Instead we time a real copy of a temp file TO the device (push/write)
        into a known-writable user folder (Download/Pictures/...) and back FROM
        it (pull/read) through the WPD shell namespace via Explorer's CopyHere.
        This yields the practical MTP transfer rate.

        If the device refuses writes everywhere, we fall back to a PULL-ONLY
        measurement using an existing file already on the device, so a read
        throughput is still reported. Only if neither works do we report that
        throughput was not measurable (the cable is still proven data-capable).
        """
        result = {"size_mb": 0, "mode": "MTP", "runs": []}
        size_mb = int(max(4, min(target_mb, 32)))
        result["size_mb"] = size_mb
        local_tmp = os.path.join(tempfile.gettempdir(), "usb_mtp_" + stamp_file() + ".bin")
        deadline = time.time() + 25.0
        write_folder = None
        pushed_name = None
        FLAGS = 16 + 4 + 512  # yes-to-all + no-progress-ui + no-confirm-mkdir
        try:
            import win32com.client
            import pythoncom

            pythoncom.CoInitialize()
            shell = win32com.client.Dispatch("Shell.Application")
            this_pc = shell.NameSpace(17)
            if this_pc is None:
                result["error"] = "Could not open This PC namespace."
                return result

            device_item = None
            for item in this_pc.Items():
                if getattr(item, "Name", "") == mtp_name:
                    device_item = item
                    break
            if device_item is None:
                result["error"] = "MTP device not found: " + str(mtp_name)
                return result

            device_folder = device_item.GetFolder
            candidates = self._mtp_write_candidates(device_folder)
            if not candidates:
                storage, _ = self._mtp_first_storage(device_folder)
                candidates = [(storage, "storage root")]
            result["target_path"] = candidates[0][1]

            progress_cb("Preparing MTP test file...")
            with open(local_tmp, "wb") as f:
                f.write(os.urandom(size_mb * 1024 * 1024))
            pushed_name = os.path.basename(local_tmp)
            src_folder = shell.NameSpace(os.path.dirname(local_tmp))
            src_item = src_folder.ParseName(pushed_name)

            write_mbps = 0
            # Try each candidate folder until one actually ACCEPTS the write.
            # Give each candidate a slice of the budget so a stubborn folder
            # doesn't starve the read-only fallback. The last candidate gets
            # whatever is left.
            n = len(candidates)
            per = max(3.0, self.MTP_WRITE_WAIT_S / n)
            tried = []
            for idx, (cand_folder, cand_path) in enumerate(candidates):
                progress_cb(f"MTP push (write) to '{cand_path}'...")
                t0 = time.time()
                cand_deadline = min(deadline - 6.0, t0 + per)
                try:
                    cand_folder.CopyHere(src_item, FLAGS)
                    appeared = False
                    while time.time() < cand_deadline:
                        try:
                            if cand_folder.ParseName(pushed_name) is not None:
                                appeared = True
                                break
                        except Exception:
                            pass
                        time.sleep(0.25)
                    wt = time.time() - t0
                    if appeared and wt > 0:
                        write_mbps = size_mb / wt
                        write_folder = cand_folder
                        result["target_path"] = cand_path
                        break
                    tried.append(cand_path)
                except Exception as e:
                    tried.append(f"{cand_path} ({e})")
                if time.time() > deadline - 6.0:
                    break
            if write_mbps == 0:
                result["write_note"] = (
                    "Device refused the write (tried: %s) -- trying read-only."
                    % ", ".join(tried[:5])
                )

            # --- Pull (read) ---
            progress_cb("MTP pull (read) in progress...")
            read_mbps = 0
            read_note = None
            read_confirmed = False   # True if ANY bytes were pulled back
            pull_dir = tempfile.mkdtemp(prefix="usb_mtp_pull_")
            dst_folder = shell.NameSpace(pull_dir)

            # Case A: our pushed file made it -> pull it back (round-trip).
            src_on_device = None
            expected_mb = size_mb
            if write_mbps > 0:
                try:
                    src_on_device = write_folder.ParseName(pushed_name)
                except Exception:
                    src_on_device = None

            # Case B: write failed -> find an existing file on the device to
            # measure read throughput anyway (pull-only fallback).
            pulled_name = pushed_name
            if src_on_device is None:
                existing = self._find_existing_file(device_folder, deadline)
                if existing is not None:
                    src_on_device = existing
                    pulled_name = getattr(existing, "Name", "pulled.bin")
                    try:
                        esz = int(getattr(existing, "Size", 0) or 0)
                        expected_mb = max(0.001, esz / (1024 * 1024)) if esz else None
                    except Exception:
                        expected_mb = None
                    read_note = "Read-only: pulled existing file '%s'." % pulled_name[:40]

            if src_on_device is not None:
                t0 = time.time()
                dst_folder.CopyHere(src_on_device, FLAGS)
                pulled = os.path.join(pull_dir, pulled_name)
                # Poll finely and wait for the local file size to STABILISE
                # (two identical reads) or hit the expected size, so small/fast
                # copies still get an accurate elapsed time.
                last_sz = -1
                stable = 0
                got_bytes = 0
                while time.time() < deadline:
                    if os.path.exists(pulled):
                        cur = os.path.getsize(pulled)
                        if expected_mb is not None and \
                                cur >= expected_mb * 1024 * 1024 * 0.98:
                            got_bytes = cur
                            break
                        if cur > 0 and cur == last_sz:
                            stable += 1
                            if stable >= 2:   # size held across polls -> done
                                got_bytes = cur
                                break
                        else:
                            stable = 0
                        last_sz = cur
                    time.sleep(0.05)
                rt = time.time() - t0
                if got_bytes == 0 and os.path.exists(pulled):
                    got_bytes = os.path.getsize(pulled)
                got_mb = got_bytes / (1024 * 1024)
                # A file smaller than ~0.5 MB copies faster than we can time
                # reliably; report it honestly instead of a bogus 0/near-0.
                if got_bytes:
                    read_confirmed = True
                if got_bytes and got_bytes < 512 * 1024:
                    read_mbps = 0
                    read_note = (
                        (read_note or "") +
                        " File too small (%.0f KB) to measure read speed "
                        "reliably; data path confirmed." % (got_bytes / 1024.0)
                    ).strip()
                elif rt > 0 and got_mb > 0:
                    read_mbps = got_mb / rt
                try:
                    if os.path.exists(pulled):
                        os.remove(pulled)
                except Exception:
                    pass
            try:
                os.rmdir(pull_dir)
            except Exception:
                pass

            if read_note:
                result["read_note"] = read_note

            result["write"] = {"min": write_mbps, "avg": write_mbps, "max": write_mbps}
            result["read"] = {"min": read_mbps, "avg": read_mbps, "max": read_mbps}
            result["rand4k"] = {"min": 0, "avg": 0, "max": 0}
            result["anomaly"] = None
            result["read_confirmed"] = read_confirmed
            if write_mbps == 0 and read_mbps == 0:
                if read_confirmed:
                    # We DID move data (a small file), just couldn't time it.
                    result["note"] = (
                        "MTP data transfer confirmed, but the available file was "
                        "too small to measure a reliable speed. Cable is "
                        "data-capable."
                    )
                else:
                    result["error"] = (
                        "MTP throughput not measurable (device refused writes and "
                        "no readable file found). Cable is still confirmed "
                        "data-capable."
                    )
            self.log.event("INFO", "benchmark_mtp_complete", result)
            return result
        except Exception as e:
            self.log.exc("benchmark_mtp", e)
            result["error"] = str(e)
            return result
        finally:
            try:
                if os.path.exists(local_tmp):
                    os.remove(local_tmp)
            except Exception:
                pass
            # Clean up our pushed test file from the device (never delete the
            # user's existing files used for the read-only fallback).
            try:
                if write_folder is not None and pushed_name:
                    leftover = write_folder.ParseName(pushed_name)
                    if leftover is not None:
                        leftover.InvokeVerb("delete")
            except Exception:
                pass
            try:
                import pythoncom

                pythoncom.CoUninitialize()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Tray icon (color-matched)
# ----------------------------------------------------------------------------
def make_icon_image(hex_color):
    img = Image.new("RGB", (64, 64), "#1a1a1a")
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=hex_color)
    return img


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class StatusTile(ctk.CTkFrame):
    def __init__(self, master, title):
        super().__init__(master, corner_radius=12, fg_color=COLOR["idle"])
        self.title = title
        self.grid_columnconfigure(0, weight=1)
        self.lbl_title = ctk.CTkLabel(
            self, text=title, font=("Segoe UI", 14, "bold"), text_color="#ffffff"
        )
        self.lbl_title.grid(row=0, column=0, padx=12, pady=(12, 2), sticky="w")
        self.lbl_value = ctk.CTkLabel(
            self, text="Idle", font=("Segoe UI", 20, "bold"), text_color="#ffffff"
        )
        self.lbl_value.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="w")

    def set(self, state, value):
        self.configure(fg_color=COLOR.get(state, COLOR["idle"]))
        self.lbl_value.configure(text=value)


class App(ctk.CTk):
    def __init__(self, logger, cfg):
        super().__init__()
        self.log = logger
        self.cfg = cfg
        self.q = queue.Queue()
        self.detector = None
        self.bench = Benchmark(logger)
        self.current_mount = None
        self.data_capable = False
        self.mtp_target = None  # name of connected MTP device (e.g. KM100)
        self.last_payload = {}
        self._first_detection = True  # suppress flash/chime for startup state
        self.tray = None

        ctk.set_appearance_mode(cfg.get("theme", "Dark"))
        ctk.set_default_color_theme("blue")
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry(cfg.get("geometry", "980x760"))
        self.minsize(900, 700)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._start_detection()
        self.after(200, self._drain_queue)
        self.log.event("INFO", "gui_ready")

    # -- UI construction -----------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # Header + admin banner
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text=f"{APP_NAME}", font=("Segoe UI", 22, "bold")
        ).grid(row=0, column=0, padx=16, pady=10, sticky="w")

        self.theme_switch = ctk.CTkSegmentedButton(
            header, values=["Dark", "Light"], command=self._toggle_theme
        )
        self.theme_switch.set(self.cfg.get("theme", "Dark"))
        self.theme_switch.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        if not is_admin():
            warn = ctk.CTkFrame(self, fg_color=COLOR["amber"], corner_radius=0)
            warn.grid(row=1, column=0, sticky="ew")
            warn.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                warn,
                text="Not running as admin — WMI USB events may be limited.",
                text_color="#000000",
                font=("Segoe UI", 12, "bold"),
            ).grid(row=0, column=0, padx=12, pady=6, sticky="w")
            ctk.CTkButton(
                warn, text="Relaunch as Admin", width=150,
                command=self._relaunch,
            ).grid(row=0, column=1, padx=12, pady=6, sticky="e")

        # LED banner
        self.banner = ctk.CTkFrame(self, fg_color=COLOR["idle"], corner_radius=12, height=60)
        self.banner.grid(row=2, column=0, sticky="ew", padx=16, pady=(12, 6))
        self.banner.grid_columnconfigure(0, weight=1)
        self.banner_lbl = ctk.CTkLabel(
            self.banner, text="Waiting for cable / device...",
            font=("Segoe UI", 18, "bold"), text_color="#ffffff",
        )
        self.banner_lbl.grid(row=0, column=0, padx=16, pady=16)

        # Status tiles
        tiles = ctk.CTkFrame(self, fg_color="transparent")
        tiles.grid(row=3, column=0, sticky="ew", padx=16, pady=6)
        for i in range(4):
            tiles.grid_columnconfigure(i, weight=1)
        self.tile_conn = StatusTile(tiles, "Cable Connected")
        self.tile_type = StatusTile(tiles, "Device Type")
        self.tile_data = StatusTile(tiles, "Data Capable")
        self.tile_speed = StatusTile(tiles, "Speed Grade")
        self.tile_conn.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.tile_type.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        self.tile_data.grid(row=0, column=2, padx=6, pady=6, sticky="nsew")
        self.tile_speed.grid(row=0, column=3, padx=6, pady=6, sticky="nsew")

        # Info row: far-end device, generation, PD
        info = ctk.CTkFrame(self)
        info.grid(row=4, column=0, sticky="ew", padx=16, pady=6)
        info.grid_columnconfigure((0, 1, 2), weight=1)
        self.lbl_device = ctk.CTkLabel(info, text="Far-end device: —", anchor="w")
        self.lbl_gen = ctk.CTkLabel(info, text="USB generation: —", anchor="w")
        self.lbl_pd = ctk.CTkLabel(info, text="PD voltage: —", anchor="w")
        self.lbl_device.grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.lbl_gen.grid(row=0, column=1, padx=10, pady=8, sticky="w")
        self.lbl_pd.grid(row=0, column=2, padx=10, pady=8, sticky="w")

        # Device picker row
        picker = ctk.CTkFrame(self)
        picker.grid(row=5, column=0, sticky="ew", padx=16, pady=(6, 0))
        picker.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(picker, text="Target device:", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, padx=(10, 8), pady=8, sticky="w"
        )
        self.device_var = ctk.StringVar(value="(no USB-C devices detected)")
        self.device_menu = ctk.CTkOptionMenu(
            picker, variable=self.device_var, values=["(no USB-C devices detected)"],
            command=self._on_device_selected, width=420,
        )
        self.device_menu.grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        # Maps the human label shown in the menu -> device descriptor dict.
        self.device_options = {}
        self.selected_key = None  # stable key of the user's chosen device

        # Controls
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=6, column=0, sticky="ew", padx=16, pady=6)
        self.btn_bench = ctk.CTkButton(
            controls, text="Run Benchmark", command=self._run_benchmark,
            fg_color=COLOR["idle"], state="disabled", width=160,
        )
        self.btn_bench.grid(row=0, column=0, padx=6)
        self.btn_batch = ctk.CTkButton(
            controls, text="Batch Mode (next cable)", command=self._batch_mark, width=180
        )
        self.btn_batch.grid(row=0, column=1, padx=6)
        self.btn_rescan = ctk.CTkButton(
            controls, text="Rescan", command=self._rescan, width=100
        )
        self.btn_rescan.grid(row=0, column=2, padx=6)
        self.btn_selftest = ctk.CTkButton(
            controls, text="Self-Test (calibrate)", command=self._self_test, width=170
        )
        self.btn_selftest.grid(row=0, column=3, padx=6)
        self.chime_var = ctk.BooleanVar(value=self.cfg.get("chime", True))
        ctk.CTkCheckBox(controls, text="Chime", variable=self.chime_var).grid(
            row=0, column=4, padx=6
        )

        # History table
        hist_frame = ctk.CTkFrame(self)
        hist_frame.grid(row=7, column=0, sticky="nsew", padx=16, pady=6)
        self.grid_rowconfigure(7, weight=1)
        hist_frame.grid_columnconfigure(0, weight=1)
        hist_frame.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(hist_frame, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Test History", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, padx=8, pady=6, sticky="w"
        )
        ctk.CTkButton(top, text="Export CSV", width=110, command=self._export_csv).grid(
            row=0, column=1, padx=8, pady=6, sticky="e"
        )
        cols = ("time", "verdict", "generation", "write", "read", "rand4k")
        self.tree = ttk.Treeview(hist_frame, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (150, 130, 170, 110, 110, 110)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="center")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # Log strip
        self.logbox = ctk.CTkTextbox(self, height=110)
        self.logbox.grid(row=8, column=0, sticky="ew", padx=16, pady=(6, 12))
        self._log_ui(f"{APP_NAME} v{APP_VERSION} started at {stamp_line()}")

    # -- helpers -------------------------------------------------------------
    def _log_ui(self, text):
        self.logbox.insert("end", f"[{stamp_line()}] {text}\n")
        self.logbox.see("end")

    def _chime(self, ok):
        if not self.chime_var.get():
            return
        try:
            winsound.MessageBeep(
                winsound.MB_OK if ok else winsound.MB_ICONHAND
            )
        except Exception:
            pass

    def _toggle_theme(self, value):
        ctk.set_appearance_mode(value)
        self.cfg["theme"] = value

    def _relaunch(self):
        self._log_ui("Relaunching as admin...")
        if relaunch_as_admin():
            self.on_close()

    def _set_banner(self, state, text):
        self.banner.configure(fg_color=COLOR.get(state, COLOR["idle"]))
        self.banner_lbl.configure(text=text)
        if self.tray:
            try:
                self.tray.icon = make_icon_image(COLOR.get(state, COLOR["idle"]))
            except Exception:
                pass

    def _flash_banner(self, state, times=4, interval=140):
        """Briefly pulse the banner to a highlight color to draw the eye to a
        connect/disconnect event, then let the next detection restore it."""
        hi = COLOR.get(state + "_hi", COLOR.get(state, COLOR["idle"]))
        base = COLOR.get(state, COLOR["idle"])

        def step(n):
            try:
                self.banner.configure(fg_color=hi if n % 2 == 0 else base)
            except Exception:
                return
            if n > 0:
                self.after(interval, lambda: step(n - 1))

        step(times)

    # -- detection lifecycle -------------------------------------------------
    def _start_detection(self):
        self.detector = DetectionEngine(self.q, self.log)
        self.detector.start()

    def _rescan(self):
        self._log_ui("Manual rescan requested.")
        if self.detector:
            threading.Thread(target=self.detector.rescan, daemon=True).start()

    def _drain_queue(self):
        try:
            while True:
                payload = self.q.get_nowait()
                self._apply_detection(payload)
        except queue.Empty:
            pass
        self.after(300, self._drain_queue)

    def _build_device_list(self, p):
        """Normalize the payload into a unified selectable device list.

        Each entry: {key, label, kind, mount|mtp_name, type}
        kind is 'mass_storage' | 'mtp' | 'ptp'. 'key' is stable across polls so
        the user's selection survives re-detection.
        """
        devices = []
        for dev, mp in p.get("volumes", {}).items():
            devices.append({
                "key": "vol:" + str(dev),
                "label": f"USB Storage \u2014 {mp} ({dev})",
                "kind": "mass_storage",
                "mount": mp,
                "mtp_name": None,
            })
        for m in p.get("mtp_devices", []):
            kind = "mtp" if m.get("type") == "MTP" else "ptp"
            tag = "MTP" if kind == "mtp" else "PTP/camera"
            devices.append({
                "key": "mtp:" + m["name"],
                "label": f"{m['name']} ({tag})",
                "kind": kind,
                "mount": None,
                "mtp_name": m["name"],
            })
        return devices

    def _apply_detection(self, p):
        self.last_payload = p
        gen = p.get("generation", "Unknown")
        pdv = p.get("pd_voltage")
        usb = p.get("usb_devices", [])

        devices = self._build_device_list(p)
        self.device_options = {d["label"]: d for d in devices}

        # Cable connected tile (any far-end presence)
        if usb or devices:
            self.tile_conn.set("green", "Detected")
        else:
            self.tile_conn.set("red", "Nothing")

        # Populate the picker, preserving the current selection when possible.
        if devices:
            labels = [d["label"] for d in devices]
            self.device_menu.configure(values=labels, state="normal")
            # Keep prior selection if its key still exists; else pick first.
            cur = None
            if self.selected_key:
                cur = next((d for d in devices if d["key"] == self.selected_key), None)
            if cur is None:
                cur = devices[0]
                self.selected_key = cur["key"]
            self.device_var.set(cur["label"])
        else:
            self.selected_key = None
            self.device_menu.configure(
                values=["(no USB-C devices detected)"], state="disabled"
            )
            self.device_var.set("(no USB-C devices detected)")

        # Speed grade tile from negotiated generation
        if gen == "USB 2.0":
            self.tile_speed.set("amber", gen)
        elif gen == "Unknown":
            self.tile_speed.set("idle", "Unknown")
        else:
            self.tile_speed.set("green", gen)

        # Info labels (still show the far-end device summary)
        mtp = p.get("mtp_devices", [])
        if mtp:
            m0 = mtp[0]
            self.lbl_device.configure(
                text=f"Far-end device: {m0['name'][:40]} ({m0.get('type')})"
            )
        elif usb:
            d = usb[0]
            self.lbl_device.configure(
                text=f"Far-end device: {d['name'][:40]} (VID {d.get('vid')}/PID {d.get('pid')})"
            )
        else:
            self.lbl_device.configure(text="Far-end device: \u2014")
        self.lbl_gen.configure(text=f"USB generation: {gen}")
        self.lbl_pd.configure(text=f"PD voltage: {pdv if pdv else '\u2014'} V")

        # Explicit connect / disconnect events (unified: volumes + MTP/PTP).
        # These flash the banner and chime so a plug/unplug is obvious even
        # when the user isn't watching the tiles.
        connected = p.get("connected", [])
        disconnected = p.get("disconnected", [])
        for lbl in connected:
            self._log_ui(f"Connected: {lbl}")
        for lbl in disconnected:
            self._log_ui(f"Disconnected: {lbl}")
        # Skip the flash/chime for the initial startup snapshot -- those
        # devices were already plugged in, not freshly connected.
        if self._first_detection:
            self._first_detection = False
        elif connected:
            self._flash_banner("green")
            self._chime(True)
        elif disconnected:
            # Only chime a disconnect if nothing new arrived in the same cycle.
            self._flash_banner("red")
            self._chime(False)

        # Update tiles/banner/benchmark target for the selected device.
        self._reflect_selection()

    def _on_device_selected(self, label):
        dev = self.device_options.get(label)
        if dev:
            self.selected_key = dev["key"]
            self._log_ui(f"Selected target: {label}")
        self._reflect_selection()

    def _reflect_selection(self):
        """Set Device Type / Data Capable tiles, banner, and benchmark target
        based on the currently selected device (not a hardcoded first volume)."""
        label = self.device_var.get()
        dev = self.device_options.get(label)

        if dev is None:
            self.data_capable = False
            self.current_mount = None
            self.mtp_target = None
            self.tile_type.set("idle", "\u2014")
            usb = self.last_payload.get("usb_devices", []) if hasattr(self, "last_payload") else []
            if usb:
                self.tile_data.set("red", "Charge-only")
                self._set_banner("red", "Charge-only (device present, no data volume)")
            else:
                self.tile_data.set("amber", "Inconclusive")
                self._set_banner("amber", "Inconclusive \u2014 plug a data device on the far end")
            self.btn_bench.configure(state="disabled", fg_color=COLOR["idle"])
            return

        kind = dev["kind"]
        if kind == "mass_storage":
            self.data_capable = True
            self.current_mount = dev["mount"]
            self.mtp_target = None
            self.tile_type.set("green", "Mass Storage")
            self.tile_data.set("green", "Data OK")
            self.btn_bench.configure(state="normal", fg_color=COLOR["green"])
            self._set_banner("green", f"Data-capable cable \u2014 volume at {dev['mount']}")
        elif kind == "mtp":
            self.data_capable = True
            self.current_mount = None
            self.mtp_target = dev["mtp_name"]
            self.tile_type.set("green", "MTP")
            self.tile_data.set("green", "Data OK (MTP)")
            self.btn_bench.configure(state="normal", fg_color=COLOR["green"])
            self._set_banner("green", f"Data-capable cable \u2014 MTP device '{dev['mtp_name']}'")
        else:  # ptp
            self.data_capable = True
            self.current_mount = None
            self.mtp_target = None
            self.tile_type.set("amber", "PTP (camera)")
            self.tile_data.set("amber", "Data (PTP only)")
            self.btn_bench.configure(state="disabled", fg_color=COLOR["idle"])
            self._set_banner("amber", f"PTP/camera mode '{dev['mtp_name']}' \u2014 cable carries data, no file storage. Switch device to MTP/File Transfer.")

    # -- benchmark -----------------------------------------------------------
    def _run_benchmark(self, calibrate=False):
        # Route to the mass-storage benchmark or the MTP copy benchmark.
        if not self.current_mount and not self.mtp_target:
            self._log_ui("No data volume or MTP device to benchmark.")
            return
        self.btn_bench.configure(state="disabled", fg_color=COLOR["amber"], text="Testing...")
        mount = self.current_mount
        mtp_name = self.mtp_target
        gen = self.lbl_gen.cget("text").replace("USB generation: ", "")

        def worker():
            def prog(msg):
                self.after(0, lambda: self._log_ui(msg))

            if mount:
                res = self.bench.run(mount, self.cfg.get("test_size_mb", 256), prog)
            else:
                self.after(0, lambda: self._log_ui(f"MTP benchmark on '{mtp_name}' (copy-based)."))
                res = self.bench.run_mtp(mtp_name, self.cfg.get("test_size_mb", 256), prog)
            self.after(0, lambda: self._benchmark_done(res, gen, calibrate))

        threading.Thread(target=worker, daemon=True).start()

    def _benchmark_done(self, res, gen, calibrate):
        is_mtp = res.get("mode") == "MTP"
        # For MTP, a "not measurable" result is NOT a cable failure -- the cable
        # is still data-capable. Show it as amber/informational, not red.
        if res.get("error"):
            if is_mtp:
                self.tile_speed.set("amber", "MTP: not measurable")
                self.btn_bench.configure(state="normal", fg_color=COLOR["green"], text="Run Benchmark")
                self._log_ui(res["error"])
                if res.get("target_path"):
                    self._log_ui(f"Target folder tried: {res['target_path']}")
                if res.get("write_note"):
                    self._log_ui(res["write_note"])
                if res.get("read_note"):
                    self._log_ui(res["read_note"])
                self._chime(True)
                row = (stamp_line(), "Data OK (MTP)", gen + " / MTP", "n/a", "n/a", "n/a")
                self.tree.insert("", 0, values=row)
                self._append_history(row)
                return
            self.tile_speed.set("red", "Test failed")
            self.btn_bench.configure(state="normal", fg_color=COLOR["red"], text="Run Benchmark")
            self._log_ui(f"Benchmark error: {res['error']}")
            self._chime(False)
            return
        w = res["write"]["avg"]
        r = res["read"]["avg"]
        rnd = res["rand4k"]["avg"]
        verdict = "Data OK (MTP)" if is_mtp else "Data OK"
        state = "green"
        if is_mtp:
            # MTP is inherently slower; don't red/amber-flag it as a bad cable.
            gen = gen + " / MTP"
            if res.get("target_path"):
                self._log_ui(f"MTP target folder: {res['target_path']}")
            if res.get("write_note"):
                self._log_ui(res["write_note"])
            if res.get("read_note"):
                self._log_ui(res["read_note"])
            if res.get("note"):
                self._log_ui(res["note"])
            # Data confirmed but not timeable (e.g. only a tiny file to pull).
            if w == 0 and r == 0 and res.get("read_confirmed"):
                verdict = "Data OK (MTP, confirmed)"
                self.tile_speed.set("green", "MTP data OK")
                self.btn_bench.configure(state="normal", fg_color=COLOR["green"], text="Run Benchmark")
                self._log_ui("MTP data transfer confirmed (speed not measurable).")
                self._chime(True)
                row = (stamp_line(), verdict, gen, "n/a", "n/a", "n/a")
                self.tree.insert("", 0, values=row)
                self._append_history(row)
                return
            # Pull-only fallback: write refused but read measured.
            if w == 0 and r > 0:
                verdict = "Data OK (MTP, read-only)"
                self.tile_speed.set("green", f"{r:.0f} MB/s read (RO)")
                self.btn_bench.configure(state="normal", fg_color=COLOR["green"], text="Run Benchmark")
                self._log_ui(f"Read {r:.0f} MB/s (write refused; pull-only)")
                self._chime(True)
                row = (stamp_line(), verdict, gen, "n/a", f"{r:.0f}", "0.0")
                self.tree.insert("", 0, values=row)
                self._append_history(row)
                return
        elif r < 40:
            state, verdict = "amber", "Slow (USB 2.0 class)"
        self.tile_speed.set(state, f"{r:.0f} MB/s read")
        self.btn_bench.configure(state="normal", fg_color=COLOR["green"], text="Run Benchmark")
        self._log_ui(
            f"Write {w:.0f} | Read {r:.0f} | 4K {rnd:.1f} MB/s"
            + (f" | {res['anomaly']}" if res.get("anomaly") else "")
        )
        if res.get("anomaly"):
            self._log_ui("ANOMALY: " + res["anomaly"])
        self._chime(True)

        row = (
            stamp_line(),
            verdict,
            gen,
            f"{w:.0f}",
            f"{r:.0f}",
            f"{rnd:.1f}",
        )
        self.tree.insert("", 0, values=row)
        self._append_history(row)
        if calibrate:
            self.cfg["calibration_read"] = r
            save_config(self.cfg)
            self._log_ui(f"Calibration saved: reference read {r:.0f} MB/s")

    def _self_test(self):
        self._log_ui("Self-test: benchmarking current volume as reference.")
        self._run_benchmark(calibrate=True)

    def _batch_mark(self):
        self._log_ui("Batch mode: remove this cable and connect the next one.")
        # Clear the current selection so the next detected device is auto-picked.
        self.selected_key = None
        self.tile_conn.set("idle", "Idle")
        self.tile_type.set("idle", "Idle")
        self.tile_data.set("idle", "Idle")
        self.tile_speed.set("idle", "Idle")
        self._set_banner("idle", "Batch: waiting for next cable...")

    # -- history persistence -------------------------------------------------
    def _append_history(self, row):
        try:
            new = not os.path.exists(HISTORY_CSV)
            with open(HISTORY_CSV, "a", encoding="utf-8", newline="") as f:
                if new:
                    f.write("time,verdict,generation,write_mbps,read_mbps,rand4k_mbps\n")
                f.write(",".join(str(x) for x in row) + "\n")
        except Exception as e:
            self.log.exc("append_history", e)

    def _export_csv(self):
        self._log_ui(f"History CSV at: {HISTORY_CSV}")

    # -- shutdown ------------------------------------------------------------
    def on_close(self):
        try:
            self.cfg["geometry"] = self.geometry()
            self.cfg["chime"] = bool(self.chime_var.get())
            save_config(self.cfg)
        except Exception:
            pass
        if self.detector:
            self.detector.stop()
        self.log.event("INFO", "app_closing")
        self.destroy()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    debug = "--debug" in sys.argv
    logger = Logger(debug=debug)
    logger.event(
        "INFO",
        "launch",
        {
            "app": APP_NAME,
            "version": APP_VERSION,
            "python": sys.version,
            "platform": sys.platform,
            "admin": is_admin(),
            "wmi": wmi is not None,
            "psutil": psutil is not None,
        },
    )

    def hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.event("FATAL", "uncaught_exception", {"traceback": tb})
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        def thook(args):
            logger.exc("thread:" + str(args.thread), args.exc_value)
        threading.excepthook = thook

    cfg = load_config()
    try:
        app = App(logger, cfg)
        app.mainloop()
    except Exception as e:
        logger.exc("main", e)
        raise


if __name__ == "__main__":
    main()
