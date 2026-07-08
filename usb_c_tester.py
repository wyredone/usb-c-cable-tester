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
from tkinter import ttk, filedialog, messagebox

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
# USB-IF vendor lookup (offline).  Maps a 4-hex VID -> vendor name so devices
# show a friendly maker instead of a bare number.  A compact built-in table
# covers common vendors (and the ones relevant to automotive/dev hardware);
# it can be extended at runtime by dropping a `usb_ids.json` next to the app
# ({"2207": "Fuzhou Rockchip Electronics Co., Ltd", ...}) -- entries there
# override/extend the built-ins.
# ----------------------------------------------------------------------------
USB_VENDORS = {
    "0403": "FTDI",
    "0424": "Microchip / SMSC",
    "045e": "Microsoft",
    "046d": "Logitech",
    "04a9": "Canon",
    "04b8": "Epson",
    "04e8": "Samsung",
    "0502": "Acer",
    "05ac": "Apple",
    "05e3": "Genesys Logic (USB hub)",
    "0630": "Autel",
    "0644": "TEAC",
    "067b": "Prolific",
    "0951": "Kingston",
    "0b05": "ASUS",
    "0bda": "Realtek",
    "0c45": "Microdia",
    "0e0f": "VMware",
    "1004": "LG Electronics",
    "10c4": "Silicon Labs (CP210x)",
    "1050": "Yubico",
    "12d1": "Huawei",
    "13fe": "Kingston / Phison",
    "1532": "Razer",
    "152d": "JMicron (USB-SATA bridge)",
    "1546": "u-blox",
    "18d1": "Google",
    "1a40": "Terminus (USB hub)",
    "1a86": "QinHeng (CH340/CH341)",
    "1b1c": "Corsair",
    "1bcf": "Sunplus / Sonix",
    "1d6b": "Linux Foundation (root hub)",
    "2109": "VIA Labs (USB hub)",
    "2207": "Fuzhou Rockchip Electronics Co., Ltd",
    "22b8": "Motorola",
    "2717": "Xiaomi",
    "273f": "Autel Intelligent Technology",
    "2833": "Oculus / Meta",
    "29a9": "Autel",
    "2e04": "HMD Global (Nokia)",
    "8087": "Intel",
    "8564": "Transcend",
    "90c3": "MediaTek",
    "18a5": "Verbatim",
}


def _load_vendor_overrides():
    """Merge an optional external usb_ids.json (VID->name) over the built-ins."""
    path = os.path.join(APP_DIR, "usb_ids.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        USB_VENDORS[k.lower().replace("0x", "").strip()] = v
    except Exception:
        pass


def vendor_name(vid):
    """Return a friendly vendor name for a 4-hex VID string, or None."""
    if not vid:
        return None
    key = str(vid).lower().replace("0x", "").strip()
    return USB_VENDORS.get(key)


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
                        "vendor": vendor_name(vid),
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
                            "vendor": vendor_name(vid),
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
# Report / chart rendering (Pillow).  No external chart libs -- we draw the
# throughput-vs-size curve and the pass/fail report card by hand so the app
# stays a single dependency-light file.
# ----------------------------------------------------------------------------
def _fit_font(size):
    """Best-effort TrueType font; fall back to Pillow's bitmap default."""
    from PIL import ImageFont
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_size(draw, text, font):
    try:
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    except Exception:
        return draw.textsize(text, font=font)


def render_sweep_chart(sweep, out_path, title="Throughput vs Transfer Size"):
    """Render a line chart of write/read MB/s across transfer sizes.

    sweep: list of {"size_mb", "write", "read"}. Returns out_path.
    """
    W, H = 900, 520
    ml, mr, mt, mb = 90, 40, 70, 70          # plot margins
    img = Image.new("RGB", (W, H), "#1b1b1b")
    d = ImageDraw.Draw(img)
    f_title = _fit_font(26)
    f_lbl = _fit_font(15)
    f_sm = _fit_font(13)

    d.text((ml, 22), title, fill="#ffffff", font=f_title)
    px0, py0 = ml, mt
    px1, py1 = W - mr, H - mb

    pts = [s for s in sweep if isinstance(s.get("size_mb"), (int, float))]
    max_speed = max([1.0] + [max(s["write"], s["read"]) for s in pts]) if pts else 1.0
    max_speed *= 1.15
    n = len(pts)

    # axes
    d.line([(px0, py0), (px0, py1)], fill="#888888", width=2)
    d.line([(px0, py1), (px1, py1)], fill="#888888", width=2)

    # y gridlines / labels (5 steps)
    for i in range(6):
        yv = max_speed * i / 5.0
        y = py1 - (py1 - py0) * (i / 5.0)
        d.line([(px0, y), (px1, y)], fill="#333333", width=1)
        lab = f"{yv:.0f}"
        tw, th = _text_size(d, lab, f_sm)
        d.text((px0 - 10 - tw, y - th / 2), lab, fill="#bbbbbb", font=f_sm)
    d.text((px0 - 60, py0 - 34), "MB/s", fill="#dddddd", font=f_lbl)

    def xy(idx, speed):
        x = px0 if n <= 1 else px0 + (px1 - px0) * (idx / (n - 1))
        y = py1 - (py1 - py0) * (speed / max_speed)
        return x, y

    # x labels
    for idx, s in enumerate(pts):
        x, _ = xy(idx, 0)
        lab = f"{s['size_mb']} MB"
        tw, th = _text_size(d, lab, f_sm)
        d.text((x - tw / 2, py1 + 8), lab, fill="#bbbbbb", font=f_sm)

    def plot(series_key, color):
        last = None
        for idx, s in enumerate(pts):
            x, y = xy(idx, s.get(series_key, 0) or 0)
            if last is not None:
                d.line([last, (x, y)], fill=color, width=3)
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=color)
            last = (x, y)

    plot("write", "#f0a52a")   # amber = write
    plot("read", "#27c06a")    # green = read

    # legend
    lx, ly = px1 - 180, py0 + 6
    d.rectangle([lx, ly, lx + 16, ly + 12], fill="#27c06a")
    d.text((lx + 22, ly - 2), "Read", fill="#dddddd", font=f_sm)
    d.rectangle([lx, ly + 22, lx + 16, ly + 34], fill="#f0a52a")
    d.text((lx + 22, ly + 20), "Write", fill="#dddddd", font=f_sm)

    img.save(out_path, "PNG")
    return out_path


def render_report_card(info, out_path):
    """Render a cable pass/fail report card PNG.

    info keys: verdict, verdict_color ('green'|'amber'|'red'), device,
    generation, write, read, rand4k, mode, timestamp, notes (list[str]).
    Returns out_path.
    """
    W, H = 900, 560
    img = Image.new("RGB", (W, H), "#1b1b1b")
    d = ImageDraw.Draw(img)
    f_h = _fit_font(30)
    f_v = _fit_font(40)
    f_k = _fit_font(17)
    f_val = _fit_font(24)
    f_sm = _fit_font(14)

    band = COLOR.get(info.get("verdict_color", "idle"), COLOR["idle"])
    d.rectangle([0, 0, W, 96], fill=band)
    d.text((30, 20), "USB-C Cable Test Report", fill="#ffffff", font=f_h)
    verdict = info.get("verdict", "\u2014")
    d.text((30, 116), verdict, fill=band, font=f_v)

    rows = [
        ("Far-end device", info.get("device", "\u2014")),
        ("USB generation", info.get("generation", "\u2014")),
        ("Mode", info.get("mode", "\u2014")),
        ("Timestamp", info.get("timestamp", "\u2014")),
    ]
    y = 190
    for k, v in rows:
        d.text((30, y), k, fill="#9a9a9a", font=f_k)
        d.text((300, y - 2), str(v), fill="#ffffff", font=f_k)
        y += 34

    # speed boxes
    y += 6
    boxes = [
        ("Write", info.get("write"), "#f0a52a"),
        ("Read", info.get("read"), "#27c06a"),
        ("4K rnd", info.get("rand4k"), "#4aa3df"),
    ]
    bx = 30
    for label, val, col in boxes:
        d.rectangle([bx, y, bx + 260, y + 110], outline=col, width=3)
        d.text((bx + 16, y + 12), label, fill="#bbbbbb", font=f_k)
        txt = "n/a" if val is None else f"{val}"
        d.text((bx + 16, y + 44), txt, fill=col, font=f_val)
        d.text((bx + 16, y + 82), "MB/s" if val is not None else "", fill="#888888", font=f_sm)
        bx += 285

    # notes
    ny = y + 130
    for note in (info.get("notes") or [])[:4]:
        d.text((30, ny), "\u2022 " + note, fill="#cccccc", font=f_sm)
        ny += 22

    d.text((30, H - 30), "Generated by USB-C Cable Tester v" + APP_VERSION,
           fill="#666666", font=f_sm)
    img.save(out_path, "PNG")
    return out_path


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

    # Default sizes (MB) for the multi-size throughput sweep.
    SWEEP_SIZES = (4, 16, 64, 256)

    def run_sweep(self, mountpoint, sizes=None, progress_cb=None):
        """Run a multi-size sequential write/read sweep on a mass-storage volume.

        Small transfers are dominated by per-op overhead and OS caching; large
        transfers expose sustained media speed and any cache cliff. Running a
        range of sizes therefore reveals the throughput-vs-size curve, which is
        the honest picture of a cable+device pair. Returns:
            {"sweep": [{"size_mb", "write", "read"}...], "sizes": [...]}
        or {"error": ...} on failure.
        """
        if progress_cb is None:
            progress_cb = lambda _m: None
        sizes = list(sizes or self.SWEEP_SIZES)
        free_mb = self._free_mb(mountpoint)
        # Never let a single sample exceed half the free space.
        cap = max(4, int(free_mb * 0.5)) if free_mb else max(sizes)
        sizes = [s for s in sizes if s <= cap] or [min(sizes)]
        block_1m = 1024 * 1024
        result = {"sweep": [], "sizes": sizes}
        # Overall time cap so a slow stick can't hang the UI thread's worker.
        deadline = time.time() + 40.0
        try:
            for idx, size_mb in enumerate(sizes):
                if time.time() > deadline:
                    self.log.event("WARN", "sweep_time_cap_hit", {"size": size_mb})
                    break
                tmp = os.path.join(mountpoint, f".usb_sweep_{stamp_file()}_{size_mb}.tmp")
                data = os.urandom(block_1m)
                # --- write ---
                progress_cb(f"Sweep {idx + 1}/{len(sizes)}: {size_mb} MB write...")
                t0 = time.time()
                written = 0
                with open(tmp, "wb", buffering=0) as f:
                    while written < size_mb * block_1m:
                        f.write(data)
                        written += block_1m
                        if time.time() > deadline:
                            break
                    f.flush()
                    os.fsync(f.fileno())
                wt = time.time() - t0
                w_mbps = (written / block_1m) / wt if wt > 0 else 0
                # --- read (drop OS cache influence by reopening unbuffered) ---
                progress_cb(f"Sweep {idx + 1}/{len(sizes)}: {size_mb} MB read...")
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
                r_mbps = (read / block_1m) / rt if rt > 0 else 0
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                result["sweep"].append(
                    {"size_mb": size_mb, "write": w_mbps, "read": r_mbps}
                )
            self.log.event("INFO", "sweep_complete", result)
            return result
        except Exception as e:
            self.log.exc("run_sweep", e)
            result["error"] = str(e)
            return result

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

    def _collect_candidate_files(self, folder, deadline, limit=40, depth=0,
                                 maxdepth=5, out=None):
        """Collect up to `limit` candidate files from the device tree for the
        pull-only read benchmark.

        MTP frequently reports Size==0 for real files, so we CANNOT trust the
        reported size to pick a good file. Instead we gather many candidates
        and let the caller actually pull them (largest reported first, but
        Size==0 files are still tried) until one yields enough real bytes to
        time. Returns a list of (shell_item, reported_size, name).
        """
        top = out is None
        if out is None:
            out = []
        if time.time() > deadline or depth > maxdepth or len(out) >= limit:
            return out
        try:
            items = folder.Items()
        except Exception:
            return out
        subfolders = []
        for it in items:
            if time.time() > deadline or len(out) >= limit:
                break
            try:
                if getattr(it, "IsFolder", False):
                    subfolders.append(it)
                    continue
                try:
                    sz = int(getattr(it, "Size", 0) or 0)
                except Exception:
                    sz = 0
                nm = getattr(it, "Name", "file")
                out.append((it, sz, nm))
            except Exception:
                continue
        for sf in subfolders:
            if time.time() > deadline or len(out) >= limit:
                break
            try:
                child = sf.GetFolder
                if child is not None:
                    self._collect_candidate_files(
                        child, deadline, limit, depth + 1, maxdepth, out)
            except Exception:
                continue
        if top:
            # Order: reported in-band first (best), then larger reported sizes,
            # then Size==0 (unknown -- worth trying), tiny known-small last.
            def rank(t):
                _, sz, _ = t
                if self.MTP_READ_MIN_BYTES <= sz <= self.MTP_READ_MAX_BYTES:
                    return (0, -sz)
                if sz == 0:
                    return (1, 0)                     # unknown size, try it
                if sz > self.MTP_READ_MAX_BYTES:
                    return (2, -sz)                   # big, but capped by timer
                return (3, -sz)                       # known-small, last resort
            out.sort(key=rank)
        return out

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
            # Per-candidate slice. Floor is normally 3s so a slow-but-willing
            # folder isn't cut off, but never exceed the total write budget --
            # this lets tests shrink MTP_WRITE_WAIT_S to run fast.
            per = min(self.MTP_WRITE_WAIT_S, max(3.0, self.MTP_WRITE_WAIT_S / n))
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

            # A single robust pull helper: copy one shell item into pull_dir,
            # wait for the local file to finish, and return the real bytes and
            # elapsed seconds. Because MTP lies about Size, we trust ONLY the
            # bytes that actually land on disk.
            def _pull_and_measure(item, name, expect_mb):
                target = os.path.join(pull_dir, name)
                # Clear any stale copy so we measure a fresh pull.
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except Exception:
                    pass
                t0 = time.time()
                try:
                    dst_folder.CopyHere(item, FLAGS)
                except Exception:
                    return 0, 0.0, target
                last_sz = -1
                stable = 0
                got = 0
                # Per-file cap so one stuck file can't eat the whole budget.
                fdeadline = min(deadline, t0 + 15.0)
                while time.time() < fdeadline:
                    if os.path.exists(target):
                        cur = os.path.getsize(target)
                        if expect_mb is not None and \
                                cur >= expect_mb * 1024 * 1024 * 0.98:
                            got = cur
                            break
                        if cur > 0 and cur == last_sz:
                            stable += 1
                            if stable >= 2:
                                got = cur
                                break
                        else:
                            stable = 0
                        last_sz = cur
                    time.sleep(0.05)
                rt = time.time() - t0
                if got == 0 and os.path.exists(target):
                    got = os.path.getsize(target)
                return got, rt, target

            # Case A: our pushed file made it -> pull it back (round-trip).
            MEASURABLE = 512 * 1024   # need >=0.5 MB pulled to time reliably
            if write_mbps > 0:
                try:
                    src_on_device = write_folder.ParseName(pushed_name)
                except Exception:
                    src_on_device = None
                if src_on_device is not None:
                    got_bytes, rt, target = _pull_and_measure(
                        src_on_device, pushed_name, size_mb)
                    if got_bytes:
                        read_confirmed = True
                    if got_bytes >= MEASURABLE and rt > 0:
                        read_mbps = (got_bytes / (1024 * 1024)) / rt
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except Exception:
                        pass

            # Case B: no measurable round-trip read yet -> walk the device for
            # existing files and PULL them (largest reported first, Size==0
            # included) until one yields enough real bytes to time. This beats
            # MTP's bogus Size==0 that previously made us grab a 0 KB '.config'.
            if read_mbps == 0:
                progress_cb("Read-only: scanning device for a file to measure...")
                candidates = self._collect_candidate_files(device_folder, deadline)
                tried_names = []
                best_small = None   # (bytes, name) largest sub-threshold pull
                for item, rep_sz, nm in candidates:
                    if time.time() > deadline - 2.0:
                        break
                    safe_nm = nm if nm else "pulled.bin"
                    expect = (rep_sz / (1024 * 1024)) if rep_sz else None
                    got_bytes, rt, target = _pull_and_measure(item, safe_nm, expect)
                    tried_names.append(safe_nm[:30])
                    if got_bytes:
                        read_confirmed = True
                    if got_bytes >= MEASURABLE and rt > 0:
                        read_mbps = (got_bytes / (1024 * 1024)) / rt
                        pulled_name = safe_nm
                        read_note = ("Read-only: measured pull of existing file "
                                     "'%s' (%.1f MB)." % (safe_nm[:40],
                                                          got_bytes / 1048576.0))
                        try:
                            if os.path.exists(target):
                                os.remove(target)
                        except Exception:
                            pass
                        break
                    # keep track of the largest tiny file as a last-resort proof
                    if got_bytes and (best_small is None or got_bytes > best_small[0]):
                        best_small = (got_bytes, safe_nm)
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except Exception:
                        pass
                if read_mbps == 0 and best_small is not None:
                    read_note = (
                        "Read-only: pulled '%s' but the largest available file "
                        "was only %.0f KB -- too small to measure a reliable "
                        "speed; data path confirmed." % (
                            best_small[1][:40], best_small[0] / 1024.0)
                    )
                elif read_mbps == 0 and not read_confirmed:
                    read_note = ("Read-only: no readable file found on device "
                                 "(tried %d)." % len(tried_names))

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
# Device filesystem: recursive tree walk + copy engine (backup / restore)
# ----------------------------------------------------------------------------
class FSNode:
    """One entry in a device/PC filesystem tree.

    For MTP, `path` is the '/'-joined chain of Names from the device root
    (never a real OS path -- MTP has no drive letter), and `size` may be the
    bogus MTP-reported size (often 0). For mass storage, `path` is a real OS
    absolute path and `size` is the true byte count from os.stat.

    `_shell` holds the COM shell item for MTP nodes so the copy engine can
    pull/push them via Explorer's CopyHere; it is None for mass-storage nodes.
    """

    __slots__ = ("name", "path", "is_folder", "size", "children", "kind",
                 "_shell")

    def __init__(self, name, path, is_folder, size=0, kind="mass", shell=None):
        self.name = name
        self.path = path
        self.is_folder = is_folder
        self.size = int(size or 0)
        self.children = []          # list[FSNode]
        self.kind = kind            # "mtp" or "mass"
        self._shell = shell         # COM item (MTP only)

    def add(self, child):
        self.children.append(child)
        return child

    def iter_files(self):
        """Yield all file (non-folder) descendants, depth-first."""
        for c in self.children:
            if c.is_folder:
                for f in c.iter_files():
                    yield f
            else:
                yield c

    def count(self):
        """(n_files, n_folders, total_reported_bytes) over the whole subtree."""
        nf = nd = 0
        tot = 0
        for c in self.children:
            if c.is_folder:
                nd += 1
                sf, sd, st = c.count()
                nf += sf
                nd += sd
                tot += st
            else:
                nf += 1
                tot += c.size
        return nf, nd, tot

    def to_dict(self):
        return {
            "name": self.name,
            "path": self.path,
            "is_folder": self.is_folder,
            "size": self.size,
            "kind": self.kind,
            "children": [c.to_dict() for c in self.children],
        }


class DeviceFS:
    """Recursive filesystem walker for connected devices.

    Two backends:
      * mass storage  -> os.scandir / os.stat (real drive letter or mount)
      * MTP           -> Shell.Application WPD namespace (no drive letter)

    A walk returns a single root FSNode whose children are the top-level
    entries. Locked scope is a FULL scan up front, so `recurse=True` descends
    the entire tree; `recurse=False` lists only the top level.
    """

    # Safety rails so a pathological device can't hang or explode the tree.
    MAX_ENTRIES = 200000
    MAX_DEPTH = 40

    def __init__(self, logger):
        self.log = logger
        self._count = 0

    # ---- mass storage --------------------------------------------------
    def walk_mass(self, mountpoint, recurse=True, progress_cb=None):
        progress_cb = progress_cb or (lambda m: None)
        root_name = mountpoint.rstrip("\\/") or mountpoint
        root = FSNode(root_name, mountpoint, True, 0, kind="mass")
        self._count = 0
        try:
            self._walk_mass_into(root, recurse, 0, progress_cb)
        except Exception as e:
            self.log.exc("walk_mass", e)
        nf, nd, tot = root.count()
        self.log.event("INFO", "devfs_walk_mass",
                       {"mount": mountpoint, "files": nf, "folders": nd,
                        "bytes": tot, "recurse": recurse})
        return root

    def _walk_mass_into(self, node, recurse, depth, progress_cb):
        if depth > self.MAX_DEPTH or self._count >= self.MAX_ENTRIES:
            return
        try:
            entries = list(os.scandir(node.path))
        except Exception:
            return
        # Folders first, then files, both alphabetical -- stable GUI order.
        entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False),
                                    e.name.lower()))
        for e in entries:
            if self._count >= self.MAX_ENTRIES:
                break
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except Exception:
                is_dir = False
            if is_dir:
                child = node.add(FSNode(e.name, e.path, True, 0, kind="mass"))
                self._count += 1
                if self._count % 500 == 0:
                    progress_cb("Scanning %s (%d entries)..."
                                % (node.name, self._count))
                if recurse:
                    self._walk_mass_into(child, recurse, depth + 1, progress_cb)
            else:
                try:
                    sz = e.stat(follow_symlinks=False).st_size
                except Exception:
                    sz = 0
                node.add(FSNode(e.name, e.path, False, sz, kind="mass"))
                self._count += 1

    # ---- MTP -----------------------------------------------------------
    def walk_mtp(self, mtp_name, recurse=True, progress_cb=None):
        progress_cb = progress_cb or (lambda m: None)
        root = FSNode(mtp_name, mtp_name, True, 0, kind="mtp")
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            try:
                shell = win32com.client.Dispatch("Shell.Application")
                this_pc = shell.NameSpace(17)
                device_item = None
                if this_pc is not None:
                    for item in this_pc.Items():
                        if getattr(item, "Name", "") == mtp_name:
                            device_item = item
                            break
                if device_item is None:
                    self.log.event("WARN", "devfs_walk_mtp_notfound",
                                   {"name": mtp_name})
                    return root
                device_folder = device_item.GetFolder
                self._count = 0
                self._walk_mtp_into(root, device_folder, recurse, 0, progress_cb)
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        except Exception as e:
            self.log.exc("walk_mtp", e)
        nf, nd, tot = root.count()
        self.log.event("INFO", "devfs_walk_mtp",
                       {"name": mtp_name, "files": nf, "folders": nd,
                        "bytes": tot, "recurse": recurse})
        return root

    def _walk_mtp_into(self, node, folder, recurse, depth, progress_cb):
        if depth > self.MAX_DEPTH or self._count >= self.MAX_ENTRIES:
            return
        try:
            items = folder.Items()
        except Exception:
            return
        files = []
        folders = []
        for it in items:
            if self._count >= self.MAX_ENTRIES:
                break
            try:
                is_dir = bool(getattr(it, "IsFolder", False))
            except Exception:
                is_dir = False
            nm = getattr(it, "Name", "item")
            if is_dir:
                folders.append((it, nm))
            else:
                try:
                    sz = int(getattr(it, "Size", 0) or 0)
                except Exception:
                    sz = 0
                files.append((it, nm, sz))
        # Folders first (alpha), then files (alpha) for a stable tree.
        folders.sort(key=lambda t: t[1].lower())
        files.sort(key=lambda t: t[1].lower())
        for it, nm in folders:
            child_path = node.path + "/" + nm if node.path else nm
            child = node.add(FSNode(nm, child_path, True, 0, kind="mtp",
                                    shell=it))
            self._count += 1
            if self._count % 200 == 0:
                progress_cb("Scanning device (%d entries)..." % self._count)
            if recurse:
                try:
                    sub = it.GetFolder
                    if sub is not None:
                        self._walk_mtp_into(child, sub, recurse, depth + 1,
                                            progress_cb)
                except Exception:
                    pass
        for it, nm, sz in files:
            child_path = node.path + "/" + nm if node.path else nm
            node.add(FSNode(nm, child_path, False, sz, kind="mtp", shell=it))
            self._count += 1


BACKUP_LOG_DIR = os.path.join(APP_DIR, "backups")


class CopyJob:
    """A resumable copy plan: a flat list of file tasks plus the folders that
    must exist. Persisted as a JSON manifest so an interrupted backup can be
    resumed (locked scope: resume interrupted backups).

    direction:
        "device_to_pc"  -> pull from device to a local destination (backup)
        "pc_to_device"  -> push from local source to the device (restore)
    """

    MANIFEST_VERSION = 1

    def __init__(self, direction, device_name, dest_root, tasks, dirs,
                 total_bytes, manifest_path):
        self.direction = direction
        self.device_name = device_name
        self.dest_root = dest_root
        self.tasks = tasks          # list[dict]: src, rel, size, done, bytes
        self.dirs = dirs            # list[str] rel dir paths to pre-create
        self.total_bytes = int(total_bytes or 0)
        self.manifest_path = manifest_path
        self.created = stamp_line()

    def to_dict(self):
        return {
            "manifest_version": self.MANIFEST_VERSION,
            "direction": self.direction,
            "device_name": self.device_name,
            "dest_root": self.dest_root,
            "created": self.created,
            "total_bytes": self.total_bytes,
            "dirs": self.dirs,
            "tasks": self.tasks,
        }

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.manifest_path), exist_ok=True)
            tmp = self.manifest_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp, self.manifest_path)
        except Exception:
            pass

    @classmethod
    def load(cls, manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        job = cls(d["direction"], d.get("device_name", ""), d["dest_root"],
                  d["tasks"], d.get("dirs", []), d.get("total_bytes", 0),
                  manifest_path)
        job.created = d.get("created", job.created)
        return job

    def remaining_bytes(self):
        return sum(t["size"] - t.get("bytes", 0)
                   for t in self.tasks if not t.get("done"))

    def done_bytes(self):
        return sum(t.get("bytes", 0) for t in self.tasks)


class CopyEngine:
    """Executes a CopyJob device<->PC with progress, ETA, resume, and a
    persistent per-backup log file. Runs synchronously; the GUI drives it on a
    worker thread and receives updates via progress_cb.

    progress_cb receives a dict each tick:
        {phase, file, done_files, total_files, done_bytes, total_bytes,
         rate_bps, eta_s, percent}
    """

    CHUNK = 1024 * 1024   # 1 MB for mass-storage copies

    def __init__(self, logger):
        self.log = logger
        self._cancel = False
        self._logf = None

    def cancel(self):
        self._cancel = True

    # ---- planning ------------------------------------------------------
    def plan_backup(self, device_name, selected_nodes, dest_root,
                    manifest_path=None):
        """Build a CopyJob to pull selected FSNodes (device -> PC).

        `selected_nodes` are FSNode file/folder roots the user checked. The
        relative path under dest_root mirrors each node's tree position using
        its `path` (MTP '/'-joined or mass OS path made relative to its root).
        """
        tasks = []
        dirs = set()
        total = 0

        def rel_for(node, base_prefix):
            # Build a clean relative path from the node's own path.
            p = node.path.replace("\\", "/")
            if base_prefix and p.startswith(base_prefix):
                p = p[len(base_prefix):]
            return p.strip("/")

        for root_node in selected_nodes:
            # base_prefix strips everything above the selected node's parent so
            # the selection's own name becomes the top of the backup tree.
            parent = root_node.path.replace("\\", "/").rsplit("/", 1)[0] \
                if "/" in root_node.path.replace("\\", "/") else ""
            base_prefix = parent + "/" if parent else ""
            if root_node.is_folder:
                dirs.add(rel_for(root_node, base_prefix))
                for f in root_node.iter_files():
                    rel = rel_for(f, base_prefix)
                    d = os.path.dirname(rel)
                    if d:
                        dirs.add(d)
                    tasks.append({
                        "kind": f.kind,
                        "src": f.path,
                        "rel": rel,
                        "size": int(f.size or 0),
                        "done": False,
                        "bytes": 0,
                    })
                    total += int(f.size or 0)
            else:
                rel = rel_for(root_node, base_prefix)
                d = os.path.dirname(rel)
                if d:
                    dirs.add(d)
                tasks.append({
                    "kind": root_node.kind,
                    "src": root_node.path,
                    "rel": rel,
                    "size": int(root_node.size or 0),
                    "done": False,
                    "bytes": 0,
                })
                total += int(root_node.size or 0)

        if manifest_path is None:
            manifest_path = os.path.join(
                BACKUP_LOG_DIR, "manifest_%s.json" % stamp_file())
        return CopyJob("device_to_pc", device_name, dest_root, tasks,
                       sorted(d for d in dirs if d), total, manifest_path)

    # ---- execution -----------------------------------------------------
    def run(self, job, progress_cb=None, mtp_pull=None):
        """Execute (or resume) a CopyJob. `mtp_pull(src_path, dest_full)` is a
        caller-provided function that performs one MTP file pull and returns
        the real bytes written (needed because MTP requires the live COM shell,
        which the GUI owns). For mass-storage tasks we copy directly here.
        Returns a summary dict.
        """
        progress_cb = progress_cb or (lambda d: None)
        self._cancel = False
        self._open_log(job)
        total_files = len(job.tasks)
        total_bytes = job.total_bytes
        done_files = sum(1 for t in job.tasks if t.get("done"))
        done_bytes = job.done_bytes()
        start = time.time()
        base_done = done_bytes
        errors = 0

        # Pre-create destination directories (skips ones already there).
        try:
            os.makedirs(job.dest_root, exist_ok=True)
            for rd in job.dirs:
                os.makedirs(os.path.join(job.dest_root, rd), exist_ok=True)
        except Exception as e:
            self._logline("mkdir error: %s" % e)

        self._logline("START %s '%s' -> '%s' (%d files, %s)"
                      % (job.direction, job.device_name, job.dest_root,
                         total_files, _human_bytes(total_bytes)))

        for i, t in enumerate(job.tasks):
            if self._cancel:
                self._logline("CANCELLED after %d/%d files" % (i, total_files))
                break
            if t.get("done"):
                continue
            dest_full = os.path.join(job.dest_root, t["rel"])
            try:
                os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            except Exception:
                pass
            got = 0
            try:
                if t["kind"] == "mass":
                    got = self._copy_mass(t["src"], dest_full, job,
                                          base_done, start, total_bytes,
                                          total_files, i, progress_cb)
                else:
                    # MTP file: delegate the actual pull to the GUI-owned shell.
                    if mtp_pull is None:
                        raise RuntimeError("no MTP pull handler")
                    got = int(mtp_pull(t["src"], dest_full) or 0)
                t["bytes"] = got
                t["done"] = True
                done_files += 1
                done_bytes = base_done + sum(
                    x.get("bytes", 0) for x in job.tasks
                    if x.get("done") and x is not t) + got
                self._logline("OK  %s (%s)" % (t["rel"], _human_bytes(got)))
            except Exception as e:
                errors += 1
                t["done"] = False
                self._logline("ERR %s : %s" % (t["rel"], e))
            # Persist manifest every few files so a crash is resumable.
            if (i + 1) % 5 == 0:
                job.save()
            self._emit(progress_cb, job, "file", t["rel"], done_files,
                       total_files, base_done, start, total_bytes)

        job.save()
        elapsed = time.time() - start
        moved = job.done_bytes()
        rate = (moved - base_done) / elapsed if elapsed > 0 else 0
        summary = {
            "direction": job.direction,
            "dest_root": job.dest_root,
            "total_files": total_files,
            "done_files": done_files,
            "errors": errors,
            "moved_bytes": moved,
            "elapsed_s": elapsed,
            "rate_bps": rate,
            "cancelled": self._cancel,
            "complete": done_files >= total_files and not self._cancel,
            "manifest": job.manifest_path,
        }
        self._logline("DONE files=%d/%d errors=%d moved=%s in %.1fs (%s/s)"
                      % (done_files, total_files, errors, _human_bytes(moved),
                         elapsed, _human_bytes(rate)))
        # A fully complete job can drop its manifest so it isn't offered for
        # resume; an incomplete one keeps it.
        if summary["complete"]:
            try:
                os.remove(job.manifest_path)
            except Exception:
                pass
        self._close_log()
        self.log.event("INFO", "backup_complete", summary)
        return summary

    def _copy_mass(self, src, dest_full, job, base_done, start, total_bytes,
                   total_files, idx, progress_cb):
        got = 0
        with open(src, "rb") as fsrc, open(dest_full, "wb") as fdst:
            while True:
                if self._cancel:
                    break
                buf = fsrc.read(self.CHUNK)
                if not buf:
                    break
                fdst.write(buf)
                got += len(buf)
                # Live per-chunk progress for big files.
                cur_done = base_done + sum(
                    x.get("bytes", 0) for x in job.tasks if x.get("done")) + got
                self._emit(progress_cb, job, "copying",
                           os.path.basename(dest_full), None, total_files,
                           base_done, start, total_bytes, extra_done=cur_done)
        return got

    # ---- progress / ETA ------------------------------------------------
    def _emit(self, progress_cb, job, phase, fname, done_files, total_files,
              base_done, start, total_bytes, extra_done=None):
        if extra_done is not None:
            done_bytes = extra_done
        else:
            done_bytes = job.done_bytes()
        elapsed = time.time() - start
        moved = max(0, done_bytes - base_done)
        rate = moved / elapsed if elapsed > 0.25 else 0
        remain = max(0, total_bytes - done_bytes)
        eta = (remain / rate) if rate > 0 else None
        pct = (done_bytes / total_bytes * 100.0) if total_bytes else 0
        try:
            progress_cb({
                "phase": phase,
                "file": fname,
                "done_files": done_files,
                "total_files": total_files,
                "done_bytes": done_bytes,
                "total_bytes": total_bytes,
                "rate_bps": rate,
                "eta_s": eta,
                "percent": pct,
            })
        except Exception:
            pass

    # ---- persistent log ------------------------------------------------
    def _open_log(self, job):
        try:
            os.makedirs(BACKUP_LOG_DIR, exist_ok=True)
            path = os.path.join(BACKUP_LOG_DIR, "backup_%s.log" % stamp_file())
            self._logf = open(path, "a", encoding="utf-8")
            self._log_path = path
        except Exception:
            self._logf = None

    def _logline(self, msg):
        line = "[%s] %s" % (stamp_line(), msg)
        if self._logf:
            try:
                self._logf.write(line + "\n")
                self._logf.flush()
            except Exception:
                pass

    def _close_log(self):
        if self._logf:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None


def _human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % int(n)
        n /= 1024.0


def _human_eta(seconds):
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


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
        self.btn_sweep = ctk.CTkButton(
            controls, text="Size Sweep", command=self._run_sweep,
            fg_color=COLOR["idle"], state="disabled", width=120,
        )
        self.btn_sweep.grid(row=0, column=1, padx=6)
        self.btn_report = ctk.CTkButton(
            controls, text="Save Report", command=self._save_report,
            fg_color=COLOR["idle"], state="disabled", width=120,
        )
        self.btn_report.grid(row=0, column=2, padx=6)
        self.btn_batch = ctk.CTkButton(
            controls, text="Batch (next cable)", command=self._batch_mark, width=150
        )
        self.btn_batch.grid(row=0, column=3, padx=6)
        self.btn_rescan = ctk.CTkButton(
            controls, text="Rescan", command=self._rescan, width=100
        )
        self.btn_rescan.grid(row=0, column=4, padx=6)
        self.btn_browse = ctk.CTkButton(
            controls, text="Browse / Backup", command=self._open_device_browser,
            fg_color=COLOR["idle"], state="disabled", width=150,
        )
        self.btn_browse.grid(row=0, column=5, padx=6)
        self.btn_selftest = ctk.CTkButton(
            controls, text="Self-Test", command=self._self_test, width=110
        )
        self.btn_selftest.grid(row=0, column=6, padx=6)
        self.chime_var = ctk.BooleanVar(value=self.cfg.get("chime", True))
        ctk.CTkCheckBox(controls, text="Chime", variable=self.chime_var).grid(
            row=0, column=7, padx=6
        )
        # Holds the most recent benchmark result + context for report/export.
        self.last_result = None
        self.last_gen = None
        self.last_device_label = None
        self.last_sweep = None

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
            ven = m0.get("vendor")
            vtag = f" \u2014 {ven}" if ven else (
                f" (VID {m0.get('vid')})" if m0.get("vid") else "")
            self.lbl_device.configure(
                text=f"Far-end device: {m0['name'][:40]} ({m0.get('type')}){vtag}"
            )
        elif usb:
            d = usb[0]
            ven = d.get("vendor")
            vtag = f" \u2014 {ven}" if ven else ""
            self.lbl_device.configure(
                text=f"Far-end device: {d['name'][:40]} "
                     f"(VID {d.get('vid')}/PID {d.get('pid')}){vtag}"
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
            self.btn_sweep.configure(state="disabled", fg_color=COLOR["idle"])
            self.btn_browse.configure(state="disabled", fg_color=COLOR["idle"])
            return

        kind = dev["kind"]
        # Browse/Backup works for anything with a real filesystem: mass storage
        # (drive letter) and MTP (WPD namespace). PTP/camera exposes no files.
        if kind in ("mass_storage", "mtp"):
            self.btn_browse.configure(state="normal", fg_color=COLOR["green"])
        else:
            self.btn_browse.configure(state="disabled", fg_color=COLOR["idle"])
        if kind == "mass_storage":
            self.data_capable = True
            self.current_mount = dev["mount"]
            self.mtp_target = None
            self.tile_type.set("green", "Mass Storage")
            self.tile_data.set("green", "Data OK")
            self.btn_bench.configure(state="normal", fg_color=COLOR["green"])
            # Size sweep needs real random-access file I/O -> mass storage only.
            self.btn_sweep.configure(state="normal", fg_color=COLOR["green"])
            self._set_banner("green", f"Data-capable cable \u2014 volume at {dev['mount']}")
        elif kind == "mtp":
            self.data_capable = True
            self.current_mount = None
            self.mtp_target = dev["mtp_name"]
            self.tile_type.set("green", "MTP")
            self.tile_data.set("green", "Data OK (MTP)")
            self.btn_bench.configure(state="normal", fg_color=COLOR["green"])
            self.btn_sweep.configure(state="disabled", fg_color=COLOR["idle"])
            self._set_banner("green", f"Data-capable cable \u2014 MTP device '{dev['mtp_name']}'")
        else:  # ptp
            self.data_capable = True
            self.current_mount = None
            self.mtp_target = None
            self.tile_type.set("amber", "PTP (camera)")
            self.tile_data.set("amber", "Data (PTP only)")
            self.btn_bench.configure(state="disabled", fg_color=COLOR["idle"])
            self.btn_sweep.configure(state="disabled", fg_color=COLOR["idle"])
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
        # Remember the latest result so "Save Report" can render it.
        self.last_result = res
        self.last_gen = gen
        self.last_device_label = self.lbl_device.cget("text").replace(
            "Far-end device: ", "")
        self.btn_report.configure(state="normal", fg_color=COLOR["green"])
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

    # -- size sweep (feature 6) ---------------------------------------------
    def _run_sweep(self):
        if not self.current_mount:
            self._log_ui("Size sweep needs a mass-storage volume (drive letter).")
            return
        self.btn_sweep.configure(state="disabled", fg_color=COLOR["amber"], text="Sweeping...")
        mount = self.current_mount
        gen = self.lbl_gen.cget("text").replace("USB generation: ", "")

        def worker():
            def prog(msg):
                self.after(0, lambda: self._log_ui(msg))
            res = self.bench.run_sweep(mount, progress_cb=prog)
            self.after(0, lambda: self._sweep_done(res, gen))

        threading.Thread(target=worker, daemon=True).start()

    def _sweep_done(self, res, gen):
        self.btn_sweep.configure(state="normal", fg_color=COLOR["green"], text="Size Sweep")
        if res.get("error"):
            self._log_ui("Sweep error: " + res["error"])
            return
        sweep = res.get("sweep", [])
        self.last_sweep = sweep
        if not sweep:
            self._log_ui("Sweep produced no samples.")
            return
        for s in sweep:
            self._log_ui(
                "Sweep %4d MB:  write %6.1f MB/s   read %6.1f MB/s"
                % (s["size_mb"], s["write"], s["read"])
            )
        # Render the throughput-vs-size curve to a PNG next to the logs.
        try:
            out = os.path.join(LOG_DIR, f"sweep_{stamp_file()}.png")
            os.makedirs(LOG_DIR, exist_ok=True)
            render_sweep_chart(sweep, out, title=f"Throughput vs Size ({gen})")
            self._log_ui("Saved sweep chart: " + out)
            try:
                os.startfile(out)  # noqa: E1101 (Windows)
            except Exception:
                pass
        except Exception as e:
            self._log_ui("Could not render sweep chart: " + str(e))

    # -- report card (feature 11) -------------------------------------------
    def _save_report(self):
        res = self.last_result
        if not res:
            self._log_ui("Run a benchmark first, then Save Report.")
            return
        # Derive a plain-English verdict + color from the last result.
        is_mtp = res.get("mode") == "MTP"
        w = res.get("write", {}).get("avg", 0)
        r = res.get("read", {}).get("avg", 0)
        rnd = res.get("rand4k", {}).get("avg", 0)
        notes = []
        for k in ("target_path", "write_note", "read_note", "note", "error", "anomaly"):
            v = res.get(k)
            if v:
                notes.append(str(v))
        if res.get("error") and not is_mtp:
            verdict, color = "FAIL", "red"
        elif is_mtp:
            if w == 0 and r == 0:
                verdict, color = "PASS (MTP, data confirmed)", "green"
            elif w == 0 and r > 0:
                verdict, color = "PASS (MTP, read-only)", "green"
            else:
                verdict, color = "PASS (MTP)", "green"
        elif r < 40:
            verdict, color = "PASS (slow / USB 2.0 class)", "amber"
        else:
            verdict, color = "PASS", "green"

        info = {
            "verdict": verdict,
            "verdict_color": color,
            "device": self.last_device_label or "\u2014",
            "generation": self.last_gen or "\u2014",
            "mode": "MTP (copy-based)" if is_mtp else "Mass storage",
            "timestamp": stamp_line(),
            "write": None if (is_mtp and w == 0) else round(w, 1),
            "read": None if (is_mtp and r == 0) else round(r, 1),
            "rand4k": None if is_mtp else round(rnd, 1),
            "notes": notes,
        }
        default_name = f"cable_report_{stamp_file()}.png"
        try:
            path = filedialog.asksaveasfilename(
                title="Save cable report",
                defaultextension=".png",
                initialfile=default_name,
                initialdir=APP_DIR,
                filetypes=[("PNG image", "*.png")],
            )
        except Exception:
            path = os.path.join(APP_DIR, default_name)
        if not path:
            return
        try:
            render_report_card(info, path)
            self._log_ui("Saved report card: " + path)
            try:
                os.startfile(path)  # noqa: E1101 (Windows)
            except Exception:
                pass
        except Exception as e:
            self._log_ui("Could not render report: " + str(e))

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

    def _open_device_browser(self):
        """Open the filesystem browser / backup window for the selected device."""
        if self.current_mount:
            mode, mount, mtp = "mass", self.current_mount, None
        elif self.mtp_target:
            mode, mount, mtp = "mtp", None, self.mtp_target
        else:
            self._log_ui("Browse/Backup: no browsable device selected.")
            return
        try:
            win = DeviceBrowserWindow(self, mode, mount, mtp, self.log)
            win.focus()
            self._log_ui("Opened Device Browser for %s"
                         % (mtp or mount))
        except Exception as e:
            self.log.exc("open_device_browser", e)
            self._log_ui("Could not open Device Browser: %s" % e)

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


class DeviceBrowserWindow(ctk.CTkToplevel):
    """Filesystem browser + backup/restore window for the connected device.

    Shows the device tree with tri-state-ish checkboxes (folder check cascades
    to children), a recurse toggle, a destination picker, a direction control
    (Device -> PC backup / PC -> Device restore), a live progress bar with
    ETA, and Resume support for interrupted backups. All heavy work (scan and
    copy) runs on a worker thread so the UI stays responsive; MTP work creates
    its own COM apartment on that thread.
    """

    def __init__(self, master, mode, mount, mtp_name, logger):
        super().__init__(master)
        self.master_app = master
        self.mode = mode                 # "mass" or "mtp"
        self.mount = mount
        self.mtp_name = mtp_name
        self.log = logger
        self.devfs = DeviceFS(logger)
        self.engine = CopyEngine(logger)

        self.root_node = None
        self._iid_to_node = {}           # treeview iid -> FSNode
        self._checked = set()            # set of checked iids
        self._worker = None
        self._busy = False

        dev_label = mtp_name if mode == "mtp" else mount
        self.title("Device Browser \u2014 %s" % dev_label)
        self.geometry("820x640")
        self.minsize(720, 560)
        self.transient(master)

        self._build()
        # Kick off the first scan shortly after the window is shown.
        self.after(150, self._start_scan)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ----------------------------------------------------------
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Top bar: recurse toggle + direction + rescan
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        self.recurse_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(top, text="Recurse into subfolders",
                        variable=self.recurse_var,
                        command=self._start_scan).grid(row=0, column=0, padx=6)
        ctk.CTkLabel(top, text="Direction:").grid(row=0, column=1, padx=(18, 4))
        self.dir_var = ctk.StringVar(value="backup")
        self.dir_menu = ctk.CTkSegmentedButton(
            top, values=["Device \u2192 PC (backup)", "PC \u2192 Device (restore)"],
            command=self._on_dir_change)
        self.dir_menu.set("Device \u2192 PC (backup)")
        self.dir_menu.grid(row=0, column=2, padx=6)
        ctk.CTkButton(top, text="Rescan", width=90,
                      command=self._start_scan).grid(row=0, column=3, padx=6)

        # Selection helpers
        selbar = ctk.CTkFrame(self, fg_color="transparent")
        selbar.grid(row=1, column=0, sticky="ew", padx=12, pady=2)
        ctk.CTkButton(selbar, text="Select all", width=90,
                      command=lambda: self._check_all(True)).grid(row=0, column=0, padx=4)
        ctk.CTkButton(selbar, text="Clear", width=70,
                      command=lambda: self._check_all(False)).grid(row=0, column=1, padx=4)
        self.sel_label = ctk.CTkLabel(selbar, text="0 selected")
        self.sel_label.grid(row=0, column=2, padx=12)

        # Tree
        tree_frame = ctk.CTkFrame(self)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=("size",), show="tree headings")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=520, anchor="w")
        self.tree.column("size", width=120, anchor="e")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)

        # Destination + actions
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
        act.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(act, text="Destination:").grid(row=0, column=0, padx=4)
        self.dest_var = ctk.StringVar(
            value=os.path.join(APP_DIR, "backups", "out"))
        self.dest_entry = ctk.CTkEntry(act, textvariable=self.dest_var)
        self.dest_entry.grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkButton(act, text="Choose...", width=90,
                      command=self._pick_dest).grid(row=0, column=2, padx=4)

        # Progress
        prog = ctk.CTkFrame(self, fg_color="transparent")
        prog.grid(row=4, column=0, sticky="ew", padx=12, pady=4)
        prog.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=4)
        self.prog_label = ctk.CTkLabel(prog, text="Idle", width=280, anchor="w")
        self.prog_label.grid(row=1, column=0, sticky="w", padx=4, pady=(2, 0))

        # Bottom buttons
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=12, pady=(4, 12))
        self.btn_start = ctk.CTkButton(btns, text="Start Backup", width=140,
                                       command=self._start_transfer,
                                       fg_color=COLOR["green"])
        self.btn_start.grid(row=0, column=0, padx=4)
        self.btn_cancel = ctk.CTkButton(btns, text="Cancel", width=90,
                                        command=self._cancel_transfer,
                                        state="disabled", fg_color=COLOR["amber"])
        self.btn_cancel.grid(row=0, column=1, padx=4)
        self.btn_resume = ctk.CTkButton(btns, text="Resume backup...", width=140,
                                        command=self._resume_backup)
        self.btn_resume.grid(row=0, column=2, padx=4)
        ctk.CTkLabel(btns, text="Logs: %s" % BACKUP_LOG_DIR,
                     text_color="#888888").grid(row=0, column=3, padx=12)

    # -- direction -------------------------------------------------------
    def _on_dir_change(self, value):
        if value.startswith("Device"):
            self.dir_var.set("backup")
            self.btn_start.configure(text="Start Backup")
            ttk_state = "normal"
        else:
            self.dir_var.set("restore")
            self.btn_start.configure(text="Start Restore")
        self._set_status("Direction: %s" % value)

    # -- scanning --------------------------------------------------------
    def _start_scan(self, *_):
        if self._busy:
            return
        self._busy = True
        self.tree.delete(*self.tree.get_children())
        self._iid_to_node.clear()
        self._checked.clear()
        self._update_sel_label()
        self._set_status("Scanning device...")
        recurse = bool(self.recurse_var.get())
        self._worker = threading.Thread(
            target=self._scan_worker, args=(recurse,), daemon=True)
        self._worker.start()

    def _scan_worker(self, recurse):
        try:
            if self.mode == "mtp":
                root = self.devfs.walk_mtp(self.mtp_name, recurse=recurse,
                                           progress_cb=self._bg_status)
            else:
                root = self.devfs.walk_mass(self.mount, recurse=recurse,
                                            progress_cb=self._bg_status)
        except Exception as e:
            self.log.exc("browser_scan", e)
            root = None
        self.after(0, lambda: self._scan_done(root))

    def _scan_done(self, root):
        self._busy = False
        self.root_node = root
        if root is None:
            self._set_status("Scan failed (see logs).")
            return
        nf, nd, tot = root.count()
        for child in root.children:
            self._insert_node("", child)
        self._set_status("Scanned: %d files, %d folders, %s"
                         % (nf, nd, _human_bytes(tot)))

    def _insert_node(self, parent_iid, node):
        label = "[ ] " + node.name + ("/" if node.is_folder else "")
        size_txt = "" if node.is_folder else _human_bytes(node.size)
        iid = self.tree.insert(parent_iid, "end", text=label, values=(size_txt,),
                               open=False)
        self._iid_to_node[iid] = node
        for child in node.children:
            self._insert_node(iid, child)
        return iid

    # -- checkbox handling ----------------------------------------------
    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("tree", "cell"):
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        # Only toggle when the click lands on the checkbox glyph area (near the
        # left of the label). Elsewhere in the row we let normal open/close and
        # selection behavior happen.
        elem = self.tree.identify_element(event.x, event.y)
        if "text" in str(elem) or region == "tree":
            # toggle if the click is on the left ~28px checkbox zone
            bbox = self.tree.bbox(iid, "#0")
            if bbox and event.x <= bbox[0] + 34:
                self._toggle(iid)
                return "break"

    def _on_tree_space(self, event):
        iid = self.tree.focus()
        if iid:
            self._toggle(iid)
            return "break"

    def _toggle(self, iid):
        want = iid not in self._checked
        self._set_check_recursive(iid, want)
        self._update_sel_label()

    def _set_check_recursive(self, iid, checked):
        self._set_check(iid, checked)
        for child in self.tree.get_children(iid):
            self._set_check_recursive(child, checked)

    def _set_check(self, iid, checked):
        node = self._iid_to_node.get(iid)
        if node is None:
            return
        if checked:
            self._checked.add(iid)
        else:
            self._checked.discard(iid)
        mark = "[x] " if checked else "[ ] "
        name = node.name + ("/" if node.is_folder else "")
        self.tree.item(iid, text=mark + name)

    def _check_all(self, checked):
        for iid in self._iid_to_node:
            self._set_check(iid, checked)
        self._update_sel_label()

    def _update_sel_label(self):
        # Count only file-leaf selections + selected-folder roots for the label.
        n_files = 0
        tot = 0
        for iid in self._checked:
            node = self._iid_to_node.get(iid)
            if node and not node.is_folder:
                n_files += 1
                tot += node.size
        self.sel_label.configure(text="%d files selected (%s)"
                                  % (n_files, _human_bytes(tot)))

    def _selected_roots(self):
        """Return the top-most checked nodes (a checked folder subsumes its
        checked children so we don't double-plan)."""
        roots = []
        for iid in self._checked:
            node = self._iid_to_node.get(iid)
            if node is None:
                continue
            parent = self.tree.parent(iid)
            # If any ancestor is also checked, skip (it will carry this node).
            anc = parent
            covered = False
            while anc:
                if anc in self._checked:
                    covered = True
                    break
                anc = self.tree.parent(anc)
            if not covered:
                roots.append(node)
        return roots

    # -- destination -----------------------------------------------------
    def _pick_dest(self):
        d = filedialog.askdirectory(title="Choose destination folder")
        if d:
            self.dest_var.set(d)

    # -- transfer --------------------------------------------------------
    def _start_transfer(self):
        if self._busy:
            return
        direction = self.dir_var.get()
        if direction == "restore":
            messagebox.showinfo(
                "Restore",
                "Restore (PC \u2192 Device) copies a chosen PC folder onto the "
                "device. Pick the SOURCE folder on your PC in the next dialog.")
            src = filedialog.askdirectory(title="Choose PC source folder to restore")
            if not src:
                return
            self._start_restore(src)
            return

        roots = self._selected_roots()
        if not roots:
            messagebox.showwarning("Nothing selected",
                                   "Check at least one file or folder to back up.")
            return
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning("No destination", "Choose a destination folder.")
            return
        self._busy = True
        self._set_buttons_running(True)
        self.engine = CopyEngine(self.log)
        self._worker = threading.Thread(
            target=self._backup_worker, args=(roots, dest), daemon=True)
        self._worker.start()

    def _backup_worker(self, roots, dest):
        try:
            job = self.engine.plan_backup(
                self.mtp_name or self.mount, roots, dest)
            summary = self.engine.run(job, progress_cb=self._bg_progress,
                                      mtp_pull=self._mtp_pull
                                      if self.mode == "mtp" else None)
        except Exception as e:
            self.log.exc("backup_worker", e)
            summary = {"error": str(e)}
        self.after(0, lambda: self._transfer_done(summary))

    def _start_restore(self, src):
        # Restore = a plain PC->device copy. For mass storage we can copy into
        # the mount directly; for MTP we push via the shell. Build a job whose
        # source is the PC tree and dest_root is the device mount (mass only).
        if self.mode == "mtp":
            messagebox.showinfo(
                "Restore to MTP",
                "MTP restore pushes files through Windows Explorer. Large MTP "
                "restores are best done by dragging in Explorer; this tool will "
                "copy top-level files into the device's first writable folder.")
        dest = self.mount if self.mode == "mass" else None
        if dest is None:
            messagebox.showwarning(
                "Unsupported",
                "Automated MTP restore isn't available; use Explorer for now.")
            return
        self._busy = True
        self._set_buttons_running(True)
        self.engine = CopyEngine(self.log)

        def _worker():
            try:
                dfs = DeviceFS(self.log)
                src_root = dfs.walk_mass(src, recurse=True)
                job = self.engine.plan_backup("PC", [src_root], dest)
                job.direction = "pc_to_device"
                summary = self.engine.run(job, progress_cb=self._bg_progress)
            except Exception as e:
                self.log.exc("restore_worker", e)
                summary = {"error": str(e)}
            self.after(0, lambda: self._transfer_done(summary))

        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _mtp_pull(self, src_path, dest_full):
        """Pull a single MTP file (identified by its '/'-joined device path)
        into dest_full using a fresh COM apartment on this worker thread.
        Returns the real bytes written."""
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            this_pc = shell.NameSpace(17)
            device_item = None
            for item in this_pc.Items():
                if getattr(item, "Name", "") == self.mtp_name:
                    device_item = item
                    break
            if device_item is None:
                return 0
            folder = device_item.GetFolder
            parts = src_path.split("/")
            # The first part is the device name itself; skip it.
            if parts and parts[0] == self.mtp_name:
                parts = parts[1:]
            *dir_parts, fname = parts
            for p in dir_parts:
                nxt = folder.ParseName(p)
                if nxt is None:
                    return 0
                folder = nxt.GetFolder
            item = folder.ParseName(fname)
            if item is None:
                return 0
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            pull_dir = os.path.dirname(dest_full)
            dst = shell.NameSpace(pull_dir)
            FLAGS = 16 + 4 + 512
            target = dest_full
            try:
                if os.path.exists(target):
                    os.remove(target)
            except Exception:
                pass
            dst.CopyHere(item, FLAGS)
            # Wait for the local file to settle.
            deadline = time.time() + 60.0
            last = -1
            stable = 0
            while time.time() < deadline:
                if os.path.exists(target):
                    cur = os.path.getsize(target)
                    if cur > 0 and cur == last:
                        stable += 1
                        if stable >= 3:
                            break
                    else:
                        stable = 0
                    last = cur
                time.sleep(0.1)
            return os.path.getsize(target) if os.path.exists(target) else 0
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _cancel_transfer(self):
        self.engine.cancel()
        self._set_status("Cancelling...")

    def _resume_backup(self):
        mf = filedialog.askopenfilename(
            title="Choose a backup manifest to resume",
            initialdir=BACKUP_LOG_DIR,
            filetypes=[("Backup manifest", "manifest_*.json"),
                       ("JSON", "*.json"), ("All", "*.*")])
        if not mf:
            return
        try:
            job = CopyJob.load(mf)
        except Exception as e:
            messagebox.showerror("Resume failed", "Could not load manifest:\n%s" % e)
            return
        self._busy = True
        self._set_buttons_running(True)
        self.engine = CopyEngine(self.log)

        def _worker():
            try:
                summary = self.engine.run(
                    job, progress_cb=self._bg_progress,
                    mtp_pull=self._mtp_pull if self.mode == "mtp" else None)
            except Exception as e:
                self.log.exc("resume_worker", e)
                summary = {"error": str(e)}
            self.after(0, lambda: self._transfer_done(summary))

        self._set_status("Resuming backup from manifest...")
        self._worker = threading.Thread(target=_worker, daemon=True)
        self._worker.start()

    def _transfer_done(self, summary):
        self._busy = False
        self._set_buttons_running(False)
        if summary.get("error"):
            self._set_status("Error: %s" % summary["error"])
            return
        if summary.get("cancelled"):
            self.progress.set(summary.get("done_files", 0)
                              / max(1, summary.get("total_files", 1)))
            self._set_status(
                "Cancelled at %d/%d files. Manifest saved \u2014 use "
                "'Resume backup...' to finish." % (summary.get("done_files", 0),
                                                   summary.get("total_files", 0)))
            return
        self.progress.set(1.0)
        rate = _human_bytes(summary.get("rate_bps", 0))
        self._set_status(
            "Done: %d/%d files, %s in %.1fs (%s/s). Errors: %d"
            % (summary.get("done_files", 0), summary.get("total_files", 0),
               _human_bytes(summary.get("moved_bytes", 0)),
               summary.get("elapsed_s", 0), rate, summary.get("errors", 0)))
        try:
            self.master_app._log_ui(
                "Backup complete: %d files to %s"
                % (summary.get("done_files", 0), summary.get("dest_root", "")))
        except Exception:
            pass

    # -- progress / status plumbing -------------------------------------
    def _bg_status(self, msg):
        self.after(0, lambda: self._set_status(msg))

    def _bg_progress(self, d):
        self.after(0, lambda: self._apply_progress(d))

    def _apply_progress(self, d):
        pct = d.get("percent", 0) / 100.0
        try:
            self.progress.set(max(0.0, min(1.0, pct)))
        except Exception:
            pass
        eta = _human_eta(d.get("eta_s"))
        rate = _human_bytes(d.get("rate_bps", 0))
        df = d.get("done_files")
        tf = d.get("total_files")
        head = ("%d/%d" % (df, tf)) if df is not None else "copying"
        self._set_status(
            "%s  |  %s / %s  |  %s/s  |  ETA %s  |  %s"
            % (head, _human_bytes(d.get("done_bytes", 0)),
               _human_bytes(d.get("total_bytes", 0)), rate, eta,
               (d.get("file") or "")[:40]))

    def _set_status(self, msg):
        try:
            self.prog_label.configure(text=msg)
        except Exception:
            pass

    def _set_buttons_running(self, running):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        self.btn_resume.configure(state="disabled" if running else "normal")

    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno(
                    "Close?", "A transfer is running. Cancel it and close?"):
                return
            self.engine.cancel()
        self.destroy()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    debug = "--debug" in sys.argv
    logger = Logger(debug=debug)
    _load_vendor_overrides()  # merge optional external usb_ids.json
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
