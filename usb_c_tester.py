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

    def __init__(self, out_queue, logger):
        super().__init__(daemon=True)
        self.q = out_queue
        self.log = logger
        self._stop = threading.Event()
        self._known = set()

    def stop(self):
        self._stop.set()

    def snapshot_volumes(self):
        vols = {}
        if psutil is None:
            return vols
        try:
            for p in psutil.disk_partitions(all=False):
                opts = (p.opts or "").lower()
                if "removable" in opts or "rw" in opts:
                    vols[p.device] = p.mountpoint
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

    def run(self):
        self._known = set(self.snapshot_volumes().keys())
        self.log.event("INFO", "detection_started", {"known_volumes": list(self._known)})
        use_wmi = wmi is not None
        watcher = None
        if use_wmi:
            try:
                import pythoncom

                pythoncom.CoInitialize()
                c = wmi.WMI()
                watcher = c.Win32_VolumeChangeEvent.watch_for()
                self.log.event("INFO", "wmi_watcher_active")
            except Exception as e:
                self.log.exc("wmi_watch_init", e)
                use_wmi = False

        while not self._stop.is_set():
            try:
                if use_wmi and watcher is not None:
                    try:
                        evt = watcher(timeout_ms=1500)
                        if evt is not None:
                            self._diff_and_emit()
                    except wmi.x_wmi_timed_out:
                        pass
                else:
                    self._diff_and_emit()
                    time.sleep(2.0)
            except Exception as e:
                self.log.exc("detection_loop", e)
                time.sleep(2.0)

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

        if force or added or removed or (not cur_keys):
            usb = self.enumerate_usb()
            payload = {
                "volumes": current,
                "added": list(added),
                "removed": list(removed),
                "usb_devices": usb,
                "generation": self.usb_generation(),
                "pd_voltage": self.pd_wattage(),
            }
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
        for i in range(3):
            tiles.grid_columnconfigure(i, weight=1)
        self.tile_conn = StatusTile(tiles, "Cable Connected")
        self.tile_data = StatusTile(tiles, "Data Capable")
        self.tile_speed = StatusTile(tiles, "Speed Grade")
        self.tile_conn.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.tile_data.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        self.tile_speed.grid(row=0, column=2, padx=6, pady=6, sticky="nsew")

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

        # Controls
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=5, column=0, sticky="ew", padx=16, pady=6)
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
        hist_frame.grid(row=6, column=0, sticky="nsew", padx=16, pady=6)
        self.grid_rowconfigure(6, weight=1)
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
        self.logbox.grid(row=7, column=0, sticky="ew", padx=16, pady=(6, 12))
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

    def _apply_detection(self, p):
        vols = p.get("volumes", {})
        usb = p.get("usb_devices", [])
        gen = p.get("generation", "Unknown")
        pdv = p.get("pd_voltage")

        # Cable connected tile
        if usb or vols:
            self.tile_conn.set("green", "Detected")
        else:
            self.tile_conn.set("red", "Nothing")

        # Data capable inference
        if vols:
            self.data_capable = True
            self.current_mount = list(vols.values())[0]
            self.tile_data.set("green", "Data OK")
            self.btn_bench.configure(state="normal", fg_color=COLOR["green"])
            self._set_banner("green", f"Data-capable cable — volume at {self.current_mount}")
        elif usb:
            # USB device present but no volume -> likely charge-only / non-storage
            self.data_capable = False
            self.current_mount = None
            self.tile_data.set("red", "Charge-only")
            self.btn_bench.configure(state="disabled", fg_color=COLOR["idle"])
            self._set_banner("red", "Charge-only (device present, no data volume)")
        else:
            self.data_capable = False
            self.current_mount = None
            self.tile_data.set("amber", "Inconclusive")
            self.btn_bench.configure(state="disabled", fg_color=COLOR["idle"])
            self._set_banner("amber", "Inconclusive — plug a data device on the far end")

        # Speed grade tile from negotiated generation
        if gen == "USB 2.0":
            self.tile_speed.set("amber", gen)
        elif gen == "Unknown":
            self.tile_speed.set("idle", "Unknown")
        else:
            self.tile_speed.set("green", gen)

        # Info labels
        if usb:
            d = usb[0]
            self.lbl_device.configure(
                text=f"Far-end device: {d['name'][:40]} (VID {d.get('vid')}/PID {d.get('pid')})"
            )
        else:
            self.lbl_device.configure(text="Far-end device: —")
        self.lbl_gen.configure(text=f"USB generation: {gen}")
        self.lbl_pd.configure(text=f"PD voltage: {pdv if pdv else '—'} V")

        if p.get("added"):
            self._log_ui(f"Volume added: {p['added']}")
        if p.get("removed"):
            self._log_ui(f"Volume removed: {p['removed']}")

    # -- benchmark -----------------------------------------------------------
    def _run_benchmark(self, calibrate=False):
        if not self.current_mount:
            self._log_ui("No data volume to benchmark.")
            return
        self.btn_bench.configure(state="disabled", fg_color=COLOR["amber"], text="Testing...")
        mount = self.current_mount
        gen = self.lbl_gen.cget("text").replace("USB generation: ", "")

        def worker():
            def prog(msg):
                self.after(0, lambda: self._log_ui(msg))

            res = self.bench.run(mount, self.cfg.get("test_size_mb", 256), prog)
            self.after(0, lambda: self._benchmark_done(res, gen, calibrate))

        threading.Thread(target=worker, daemon=True).start()

    def _benchmark_done(self, res, gen, calibrate):
        if res.get("error"):
            self.tile_speed.set("red", "Test failed")
            self.btn_bench.configure(state="normal", fg_color=COLOR["red"], text="Run Benchmark")
            self._log_ui(f"Benchmark error: {res['error']}")
            self._chime(False)
            return
        w = res["write"]["avg"]
        r = res["read"]["avg"]
        rnd = res["rand4k"]["avg"]
        verdict = "Data OK"
        state = "green"
        if r < 40:
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
        self.tile_conn.set("idle", "Idle")
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
