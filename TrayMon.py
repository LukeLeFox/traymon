import os
import sys
import time
import json
import threading
import queue
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List, Any

import psutil
import pystray
from pystray import MenuItem as item, Menu
from PIL import Image, ImageDraw

# Tk must live in MAIN THREAD
import tkinter as tk
from tkinter import simpledialog, colorchooser

# OPTIONAL: WMI access for LibreHardwareMonitor (LHM)
try:
    import wmi  # pip install wmi
    WMI_OK = True
except Exception:
    WMI_OK = False


# =========================
# DEFAULT CONFIG
# =========================
CONFIG: Dict[str, Any] = {
    "refresh_s": 1.0,

    "show_cpu": True,
    "show_ram": True,
    "show_net": True,
    "show_disk": True,

    # Legacy single-interface
    "net_iface": "auto",
    # Multi-interface (optional)
    "net_ifaces": None,          # ["Ethernet","Wi-Fi"] or ["auto"]
    "net_mode": "aggregate",     # "aggregate" or "separate"

    # Temps/GPU via LHM
    "show_temps": True,
    "show_cpu_temp": True,
    "show_gpu": True,
    "show_gpu_temp": True,
    "show_gpu_load": True,

    "tooltip_lines": 6,

    # LHM
    "lhm_exe": "LibreHardwareMonitor.exe",
    "lhm_run_hidden": True,
    "lhm_wmi_namespace": r"root\LibreHardwareMonitor",

    # Config file
    "config_json": "config.json",

    # Overlay
    "overlay_enabled": False,

    # Positioning
    "overlay_pos": "bottom_right",  # top_left/top_right/bottom_left/bottom_right/center
    "overlay_x": None,              # custom coords if both ints
    "overlay_y": None,

    # Drag/lock
    "overlay_locked": True,

    # Visuals
    "overlay_padding": 10,
    "overlay_bg": "black",
    "overlay_fg": "white",
    "overlay_font": ["Segoe UI", 10],
    "overlay_format": "{cpu}\n{ram}\n{net}\n{disk}\n{gpu}",
}


# =========================
# Helpers
# =========================
def safe_bool(v, default=False) -> bool:
    return default if v is None else bool(v)


def human_rate(bps: float) -> str:
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    v = float(max(0.0, bps))
    i = 0
    while v >= 1024.0 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}"


def human_bytes(b: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(max(0.0, b))
    i = 0
    while v >= 1024.0 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.1f} {units[i]}"


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _strip_jsonc(text: str) -> str:
    """
    Minimal JSONC/# support:
    - removes line comments starting with // or #
    - ignores comment markers inside double quotes
    """
    out_lines: List[str] = []
    for line in text.splitlines():
        s = line
        in_str = False
        esc = False
        cut = None
        for i in range(len(s)):
            ch = s[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "#" and cut is None:
                    cut = i
                    break
                if ch == "/" and i + 1 < len(s) and s[i + 1] == "/" and cut is None:
                    cut = i
                    break
        if cut is not None:
            s = s[:cut]
        out_lines.append(s)
    return "\n".join(out_lines)


def _read_json_file_allow_comments(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    cooked = _strip_jsonc(raw)
    obj = json.loads(cooked)
    return obj if isinstance(obj, dict) else {}


def load_config_override(base_dir: str) -> None:
    path = os.path.join(base_dir, CONFIG["config_json"])
    if not os.path.isfile(path):
        return
    try:
        data = _read_json_file_allow_comments(path)
        for k, v in data.items():
            if k in CONFIG:
                CONFIG[k] = v
    except Exception as e:
        print(f"[config] errore nel config.json: {e}")


def save_config_updates(base_dir: str, updates: Dict[str, Any]) -> None:
    """
    Merge updates into config.json (create if missing).
    Keeps unknown keys intact.
    """
    path = os.path.join(base_dir, CONFIG["config_json"])
    try:
        current: Dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                current = _read_json_file_allow_comments(path)
            except Exception:
                current = {}
        current.update(updates)
        _atomic_write_json(path, current)
    except Exception:
        pass


# =========================
# Overlay (Tk main thread)
# =========================
class OverlayBar:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=CONFIG["overlay_bg"])

        font = tuple(CONFIG["overlay_font"]) if isinstance(CONFIG["overlay_font"], list) else CONFIG["overlay_font"]
        self.label = tk.Label(
            self.root,
            text="starting...",
            fg=CONFIG["overlay_fg"],
            bg=CONFIG["overlay_bg"],
            font=font,
            padx=10,
            pady=6,
            justify="left",
        )
        self.label.pack()

        self.visible = True
        self._q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

        # drag state
        self._drag_x = 0
        self._drag_y = 0
        self._drag_bound = False

        self.root.after(100, self._drain_queue)

        self._apply_config_to_ui()

        if not safe_bool(CONFIG.get("overlay_enabled"), False):
            self.visible = False
            self.root.withdraw()

    # ---- thread-safe posts ----
    def post_set_text(self, text: str): self._q.put(("text", text))
    def post_showhide(self, enable: bool): self._q.put(("showhide", bool(enable)))
    def post_stop(self): self._q.put(("stop", None))
    def post_set_pos_preset(self, preset: str): self._q.put(("set_preset", str(preset)))
    def post_set_coords(self, x: int, y: int): self._q.put(("set_coords", (int(x), int(y))))
    def post_set_locked(self, locked: bool): self._q.put(("set_locked", bool(locked)))
    def post_apply_config(self): self._q.put(("apply_config", None))
    def post_get_pos(self, response_q: "queue.Queue[Tuple[int, int]]"): self._q.put(("get_pos", response_q))
    def post_prompt_coords(self, response_q: "queue.Queue[Optional[Tuple[int, int]]]"): self._q.put(("prompt_coords", response_q))
    def post_prompt_colors(self, response_q: "queue.Queue[Optional[Tuple[str, str]]]"): self._q.put(("prompt_colors", response_q))

    # ---- UI internals ----
    def _apply_config_to_ui(self):
        self.root.configure(bg=CONFIG["overlay_bg"])
        self.label.configure(bg=CONFIG["overlay_bg"], fg=CONFIG["overlay_fg"])

        font = tuple(CONFIG["overlay_font"]) if isinstance(CONFIG["overlay_font"], list) else CONFIG["overlay_font"]
        try:
            self.label.configure(font=font)
        except Exception:
            pass

        locked = safe_bool(CONFIG.get("overlay_locked"), True)
        self._set_drag_enabled(not locked)

        self._position_from_config()

    def _position_from_config(self):
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        pad = int(CONFIG.get("overlay_padding", 10))

        cx = CONFIG.get("overlay_x", None)
        cy = CONFIG.get("overlay_y", None)

        if isinstance(cx, int) and isinstance(cy, int):
            x, y = cx, cy
        else:
            pos = str(CONFIG.get("overlay_pos", "bottom_right"))
            if pos == "bottom_left":
                x = pad
                y = sh - h - 60
            elif pos == "top_right":
                x = sw - w - pad
                y = pad
            elif pos == "top_left":
                x = pad
                y = pad
            elif pos == "center":
                x = max(pad, (sw - w) // 2)
                y = max(pad, (sh - h) // 2)
            else:
                x = sw - w - pad
                y = sh - h - 60

        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _set_drag_enabled(self, enabled: bool):
        if enabled and not self._drag_bound:
            self.root.bind("<ButtonPress-1>", self._on_drag_start)
            self.root.bind("<B1-Motion>", self._on_drag_move)
            self.root.bind("<ButtonRelease-1>", self._on_drag_end)
            self._drag_bound = True
        elif (not enabled) and self._drag_bound:
            self.root.unbind("<ButtonPress-1>")
            self.root.unbind("<B1-Motion>")
            self.root.unbind("<ButtonRelease-1>")
            self._drag_bound = False

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_move(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _on_drag_end(self, event):
        # Save coords immediately (lightweight, once per drag)
        try:
            x = int(self.root.winfo_x())
            y = int(self.root.winfo_y())
            CONFIG["overlay_x"] = x
            CONFIG["overlay_y"] = y
            save_config_updates(self.base_dir, {"overlay_x": x, "overlay_y": y})
        except Exception:
            pass

    # ---- command loop ----
    def _drain_queue(self):
        try:
            while True:
                cmd, payload = self._q.get_nowait()

                if cmd == "text":
                    self.label.config(text=payload)
                    self.root.update_idletasks()
                    self._position_from_config()

                elif cmd == "showhide":
                    self.visible = bool(payload)
                    if self.visible:
                        self.root.deiconify()
                        self._position_from_config()
                    else:
                        self.root.withdraw()

                elif cmd == "apply_config":
                    self._apply_config_to_ui()
                    if self.visible:
                        self.root.deiconify()
                        self._position_from_config()

                elif cmd == "set_preset":
                    preset = str(payload)
                    CONFIG["overlay_pos"] = preset
                    CONFIG["overlay_x"] = None
                    CONFIG["overlay_y"] = None
                    self._position_from_config()

                elif cmd == "set_coords":
                    x, y = payload
                    CONFIG["overlay_x"] = int(x)
                    CONFIG["overlay_y"] = int(y)
                    self._position_from_config()

                elif cmd == "set_locked":
                    locked = bool(payload)
                    CONFIG["overlay_locked"] = locked
                    self._set_drag_enabled(not locked)

                elif cmd == "get_pos":
                    resp_q = payload
                    try:
                        resp_q.put((int(self.root.winfo_x()), int(self.root.winfo_y())))
                    except Exception:
                        resp_q.put((0, 0))

                elif cmd == "prompt_coords":
                    resp_q = payload
                    try:
                        curx = int(self.root.winfo_x())
                        cury = int(self.root.winfo_y())
                        x = simpledialog.askinteger("Overlay X", "Inserisci X (pixel):", initialvalue=curx)
                        if x is None:
                            resp_q.put(None)
                            continue
                        y = simpledialog.askinteger("Overlay Y", "Inserisci Y (pixel):", initialvalue=cury)
                        if y is None:
                            resp_q.put(None)
                            continue
                        resp_q.put((int(x), int(y)))
                    except Exception:
                        resp_q.put(None)

                elif cmd == "prompt_colors":
                    resp_q = payload
                    try:
                        fg = colorchooser.askcolor(title="Scegli testo (foreground)")[1]
                        if fg is None:
                            resp_q.put(None)
                            continue
                        bg = colorchooser.askcolor(title="Scegli sfondo (background)")[1]
                        if bg is None:
                            resp_q.put(None)
                            continue
                        resp_q.put((bg, fg))
                    except Exception:
                        resp_q.put(None)

                elif cmd == "stop":
                    try:
                        self.root.destroy()
                    except Exception:
                        pass
                    return

        except queue.Empty:
            pass

        self.root.after(100, self._drain_queue)

    def mainloop(self):
        self.root.mainloop()


# =========================
# LibreHardwareMonitor bridge
# =========================
class LHMBridge:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.proc: Optional[subprocess.Popen] = None
        self.connected = False
        self.w = None

    def start_if_available(self) -> None:
        if not safe_bool(CONFIG.get("show_temps"), True) and not safe_bool(CONFIG.get("show_gpu"), True):
            return
        if not WMI_OK:
            return

        exe_path = os.path.join(self.base_dir, CONFIG["lhm_exe"])
        if not os.path.isfile(exe_path):
            return

        try:
            creationflags = 0
            if safe_bool(CONFIG.get("lhm_run_hidden"), True) and os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

            self.proc = subprocess.Popen(
                [exe_path],
                cwd=self.base_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )

            time.sleep(1.0)

            self.w = wmi.WMI(namespace=CONFIG["lhm_wmi_namespace"])
            _ = self.w.Sensor()
            self.connected = True
        except Exception:
            self.connected = False
            self.w = None

    def stop(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass

    def read_cpu_temp_c(self) -> Optional[float]:
        if not self.connected or not self.w:
            return None
        try:
            temps = []
            for s in self.w.Sensor():
                if s.SensorType == "Temperature" and "cpu" in (s.Name or "").lower():
                    if s.Value is not None:
                        temps.append(float(s.Value))
            return max(temps) if temps else None
        except Exception:
            return None

    def read_gpu(self) -> Tuple[Optional[float], Optional[float]]:
        if not self.connected or not self.w:
            return (None, None)
        try:
            load_vals = []
            temp_vals = []
            for s in self.w.Sensor():
                name = (s.Name or "").lower()
                if s.SensorType == "Load":
                    if "gpu" in name and "core" in name and s.Value is not None:
                        load_vals.append(float(s.Value))
                elif s.SensorType == "Temperature":
                    if "gpu" in name and s.Value is not None:
                        temp_vals.append(float(s.Value))
            gpu_load = max(load_vals) if load_vals else None
            gpu_temp = max(temp_vals) if temp_vals else None
            return (gpu_load, gpu_temp)
        except Exception:
            return (None, None)


# =========================
# Metrics models
# =========================
@dataclass
class NetRate:
    up_bps: float
    down_bps: float


@dataclass
class Snapshot:
    cpu_percent: Optional[float] = None
    cpu_temp_c: Optional[float] = None

    ram_used: Optional[int] = None
    ram_total: Optional[int] = None

    net_agg: Optional[NetRate] = None
    net_per_iface: Dict[str, NetRate] = field(default_factory=dict)

    disk_read_bps: Optional[float] = None
    disk_write_bps: Optional[float] = None

    gpu_load_percent: Optional[float] = None
    gpu_temp_c: Optional[float] = None

    def _cpu_line(self) -> Optional[str]:
        if not CONFIG["show_cpu"]:
            return None
        cpu = "CPU n/a" if self.cpu_percent is None else f"CPU {self.cpu_percent:.0f}%"
        if safe_bool(CONFIG.get("show_temps"), True) and safe_bool(CONFIG.get("show_cpu_temp"), True) and self.cpu_temp_c is not None:
            cpu += f" | {self.cpu_temp_c:.0f}°C"
        return cpu

    def _ram_line(self) -> Optional[str]:
        if not CONFIG["show_ram"]:
            return None
        if self.ram_used is None or self.ram_total is None:
            return "RAM n/a"
        return f"RAM {human_bytes(self.ram_used)}/{human_bytes(self.ram_total)}"

    def _disk_line(self) -> Optional[str]:
        if not CONFIG["show_disk"]:
            return None
        if self.disk_read_bps is None or self.disk_write_bps is None:
            return "DISK n/a"
        return f"DISK R {human_rate(self.disk_read_bps)} W {human_rate(self.disk_write_bps)}"

    def _gpu_line(self) -> Optional[str]:
        if not safe_bool(CONFIG.get("show_gpu"), True):
            return None
        show_temp = safe_bool(CONFIG.get("show_temps"), True) and safe_bool(CONFIG.get("show_gpu_temp"), True)
        show_load = safe_bool(CONFIG.get("show_gpu_load"), True)

        if self.gpu_load_percent is None and self.gpu_temp_c is None:
            return "GPU n/a"

        parts = ["GPU"]
        if show_load:
            parts.append(f"{self.gpu_load_percent:.0f}%" if self.gpu_load_percent is not None else "n/a")
        if show_temp and self.gpu_temp_c is not None:
            parts.append(f"{self.gpu_temp_c:.0f}°C")
        return " | ".join(parts)

    def _net_lines(self) -> List[str]:
        if not CONFIG["show_net"]:
            return []

        if isinstance(CONFIG.get("net_ifaces"), list) and (CONFIG.get("net_mode") == "separate"):
            if not self.net_per_iface:
                return ["NET n/a"]
            lines = []
            for name, rate in self.net_per_iface.items():
                lines.append(f"NET({name}) ↓{human_rate(rate.down_bps)} ↑{human_rate(rate.up_bps)}")
            return lines

        if self.net_agg is None:
            return ["NET n/a"]

        label = "NET"
        if isinstance(CONFIG.get("net_ifaces"), list) and CONFIG.get("net_mode") == "aggregate":
            label = "NET(" + "+".join(CONFIG["net_ifaces"]) + ")"
        return [f"{label} ↓{human_rate(self.net_agg.down_bps)} ↑{human_rate(self.net_agg.up_bps)}"]

    def to_lines(self) -> List[str]:
        lines: List[str] = []
        for l in (self._cpu_line(), self._ram_line()):
            if l:
                lines.append(l)
        lines.extend(self._net_lines())
        for l in (self._disk_line(), self._gpu_line()):
            if l:
                lines.append(l)
        return lines[: max(1, int(CONFIG.get("tooltip_lines", 6)))]

    def to_text(self) -> str:
        return "\n".join(self.to_lines())

    def overlay_tokens(self) -> Dict[str, str]:
        cpu = self._cpu_line() or ""
        ram = self._ram_line() or ""
        disk = self._disk_line() or ""
        gpu = self._gpu_line() or ""

        net = ""
        net_lines = self._net_lines()
        if net_lines:
            net = " / ".join(net_lines) if CONFIG.get("net_mode") == "separate" else net_lines[0]

        return {"cpu": cpu, "ram": ram, "net": net, "disk": disk, "gpu": gpu}


# =========================
# Sampler
# =========================
class Sampler:
    def __init__(self, lhm: LHMBridge):
        self.lhm = lhm
        psutil.cpu_percent(interval=None)

        self.last_net_single = None  # (t, bytes_sent, bytes_recv, iface)
        self.net_iface_single = self._pick_iface_single()

        self.last_net_multi = None   # (t, {iface: (sent, recv)})
        self.net_ifaces_multi = self._pick_ifaces_multi()

        self.last_disk = None  # (t, read_bytes, write_bytes)

    def _pick_iface_single(self) -> Optional[str]:
        try:
            stats = psutil.net_if_stats()
            io = psutil.net_io_counters(pernic=True)
            if not stats:
                return None

            cfg_iface = str(CONFIG.get("net_iface", "auto")).strip()
            if cfg_iface.lower() != "auto":
                return cfg_iface if cfg_iface in stats else None

            candidates = []
            for name, st in stats.items():
                if not st.isup:
                    continue
                c = io.get(name)
                if not c:
                    continue
                score = c.bytes_sent + c.bytes_recv
                candidates.append((score, name))
            candidates.sort(reverse=True)
            return candidates[0][1] if candidates else None
        except Exception:
            return None

    def _pick_ifaces_multi(self) -> List[str]:
        cfg = CONFIG.get("net_ifaces", None)
        if not isinstance(cfg, list):
            return []

        try:
            stats = psutil.net_if_stats()
            io = psutil.net_io_counters(pernic=True)

            if any(str(x).lower() == "auto" for x in cfg):
                ifaces = []
                for name, st in stats.items():
                    if not st.isup:
                        continue
                    if name in io:
                        ifaces.append(name)
                return ifaces

            return [name for name in cfg if name in stats and name in io]
        except Exception:
            return []

    def reload_net_selection(self):
        self.net_iface_single = self._pick_iface_single()
        self.net_ifaces_multi = self._pick_ifaces_multi()
        self.last_net_single = None
        self.last_net_multi = None

    def read(self) -> Snapshot:
        s = Snapshot()

        if CONFIG["show_cpu"]:
            try:
                s.cpu_percent = psutil.cpu_percent(interval=None)
            except Exception:
                s.cpu_percent = None

        if CONFIG["show_ram"]:
            try:
                vm = psutil.virtual_memory()
                s.ram_used = int(vm.used)
                s.ram_total = int(vm.total)
            except Exception:
                s.ram_used = s.ram_total = None

        if CONFIG["show_net"]:
            if isinstance(CONFIG.get("net_ifaces"), list):
                self._read_net_multi(s)
            else:
                self._read_net_single(s)

        if CONFIG["show_disk"]:
            try:
                now = time.time()
                dio = psutil.disk_io_counters()
                if dio is None:
                    s.disk_read_bps = s.disk_write_bps = None
                else:
                    if self.last_disk is None:
                        self.last_disk = (now, dio.read_bytes, dio.write_bytes)
                        s.disk_read_bps = 0.0
                        s.disk_write_bps = 0.0
                    else:
                        t0, r0, w0 = self.last_disk
                        dt = max(0.25, now - t0)
                        s.disk_read_bps = (dio.read_bytes - r0) / dt
                        s.disk_write_bps = (dio.write_bytes - w0) / dt
                        self.last_disk = (now, dio.read_bytes, dio.write_bytes)
            except Exception:
                s.disk_read_bps = s.disk_write_bps = None

        if safe_bool(CONFIG.get("show_temps"), True) and (CONFIG["show_cpu"] or safe_bool(CONFIG.get("show_gpu"), True)):
            if self.lhm.connected:
                if CONFIG["show_cpu"] and safe_bool(CONFIG.get("show_cpu_temp"), True):
                    s.cpu_temp_c = self.lhm.read_cpu_temp_c()
                if safe_bool(CONFIG.get("show_gpu"), True):
                    gl, gt = self.lhm.read_gpu()
                    if safe_bool(CONFIG.get("show_gpu_load"), True):
                        s.gpu_load_percent = gl
                    if safe_bool(CONFIG.get("show_gpu_temp"), True):
                        s.gpu_temp_c = gt

        return s

    def _read_net_single(self, s: Snapshot):
        iface = self.net_iface_single
        try:
            now = time.time()
            c = psutil.net_io_counters(pernic=True).get(iface) if iface else None
            if not c:
                s.net_agg = None
                return

            if self.last_net_single is None:
                self.last_net_single = (now, c.bytes_sent, c.bytes_recv, iface)
                s.net_agg = NetRate(up_bps=0.0, down_bps=0.0)
            else:
                t0, sent0, recv0, _ = self.last_net_single
                dt = max(0.25, now - t0)
                up = (c.bytes_sent - sent0) / dt
                down = (c.bytes_recv - recv0) / dt
                self.last_net_single = (now, c.bytes_sent, c.bytes_recv, iface)
                s.net_agg = NetRate(up_bps=up, down_bps=down)
        except Exception:
            s.net_agg = None

    def _read_net_multi(self, s: Snapshot):
        ifaces = self.net_ifaces_multi
        try:
            now = time.time()
            io = psutil.net_io_counters(pernic=True)

            curr: Dict[str, Tuple[int, int]] = {}
            for name in ifaces:
                c = io.get(name)
                if c:
                    curr[name] = (c.bytes_sent, c.bytes_recv)

            if not curr:
                s.net_agg = None
                s.net_per_iface = {}
                return

            if self.last_net_multi is None:
                self.last_net_multi = (now, curr)
                s.net_per_iface = {n: NetRate(0.0, 0.0) for n in curr.keys()}
                s.net_agg = NetRate(0.0, 0.0)
                return

            t0, prev = self.last_net_multi
            dt = max(0.25, now - t0)

            per: Dict[str, NetRate] = {}
            agg_up = 0.0
            agg_down = 0.0

            for name, (sent, recv) in curr.items():
                sent0, recv0 = prev.get(name, (sent, recv))
                up = (sent - sent0) / dt
                down = (recv - recv0) / dt
                per[name] = NetRate(up_bps=up, down_bps=down)
                agg_up += up
                agg_down += down

            self.last_net_multi = (now, curr)
            s.net_per_iface = per
            s.net_agg = NetRate(up_bps=agg_up, down_bps=agg_down)
        except Exception:
            s.net_agg = None
            s.net_per_iface = {}


# =========================
# Tray app
# =========================
class TrayApp:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        load_config_override(self.base_dir)

        self.lhm = LHMBridge(self.base_dir)
        self.lhm.start_if_available()

        self.sampler = Sampler(self.lhm)

        self._lock = threading.Lock()
        self._last_snapshot = Snapshot()
        self._stop = threading.Event()

        self.overlay: Optional[OverlayBar] = None

        self.icon = pystray.Icon("TrayPerfMon", self._make_image(), "TrayPerfMon", self._make_menu())

        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def attach_overlay(self, overlay: OverlayBar):
        self.overlay = overlay

    def _make_image(self) -> Image.Image:
        img = Image.new("RGB", (64, 64), "white")
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), outline="black", width=4)
        d.line((32, 18, 32, 46), fill="black", width=4)
        d.line((32, 46, 44, 38), fill="black", width=4)
        return img

    # ---------- overlay menu labels (pystray passes 1 arg) ----------
    def _menu_overlay_label(self, _):
        return f"Overlay: {'ON' if safe_bool(CONFIG.get('overlay_enabled'), False) else 'OFF'}"

    def _menu_drag_label(self, _):
        return f"Sposta a mano: {'SBLOCCATO' if (not safe_bool(CONFIG.get('overlay_locked'), True)) else 'BLOCCATO'}"

    # ---------- overlay actions ----------
    def overlay_toggle_enabled(self, icon=None, item_=None):
        new_val = not safe_bool(CONFIG.get("overlay_enabled"), False)
        CONFIG["overlay_enabled"] = new_val
        save_config_updates(self.base_dir, {"overlay_enabled": new_val})
        if self.overlay:
            self.overlay.post_showhide(new_val)

    def overlay_toggle_lock(self, icon=None, item_=None):
        locked = safe_bool(CONFIG.get("overlay_locked"), True)
        new_locked = not locked
        CONFIG["overlay_locked"] = new_locked
        save_config_updates(self.base_dir, {"overlay_locked": new_locked})
        if self.overlay:
            self.overlay.post_set_locked(new_locked)

        # On lock -> persist current position (extra safety; drag-end already saves)
        if new_locked and self.overlay:
            try:
                resp = queue.Queue()
                self.overlay.post_get_pos(resp)
                x, y = resp.get(timeout=2)
                CONFIG["overlay_x"] = int(x)
                CONFIG["overlay_y"] = int(y)
                save_config_updates(self.base_dir, {"overlay_x": int(x), "overlay_y": int(y)})
            except Exception:
                pass

    def overlay_set_preset(self, preset: str):
        CONFIG["overlay_pos"] = preset
        CONFIG["overlay_x"] = None
        CONFIG["overlay_y"] = None
        save_config_updates(self.base_dir, {"overlay_pos": preset, "overlay_x": None, "overlay_y": None})
        if self.overlay:
            self.overlay.post_set_pos_preset(preset)

    def overlay_preset_top_left(self, icon=None, item_=None): self.overlay_set_preset("top_left")
    def overlay_preset_top_right(self, icon=None, item_=None): self.overlay_set_preset("top_right")
    def overlay_preset_bottom_left(self, icon=None, item_=None): self.overlay_set_preset("bottom_left")
    def overlay_preset_bottom_right(self, icon=None, item_=None): self.overlay_set_preset("bottom_right")
    def overlay_preset_center(self, icon=None, item_=None): self.overlay_set_preset("center")

    def overlay_set_coords(self, icon=None, item_=None):
        if not self.overlay:
            return
        try:
            resp = queue.Queue()
            self.overlay.post_prompt_coords(resp)
            result = resp.get(timeout=120)
            if result is None:
                return
            x, y = result
            CONFIG["overlay_x"] = int(x)
            CONFIG["overlay_y"] = int(y)
            save_config_updates(self.base_dir, {"overlay_x": int(x), "overlay_y": int(y)})
            self.overlay.post_set_coords(int(x), int(y))
        except Exception:
            pass

    # ---- overlay colors ----
    def overlay_set_colors(self, bg: str, fg: str):
        CONFIG["overlay_bg"] = bg
        CONFIG["overlay_fg"] = fg
        save_config_updates(self.base_dir, {"overlay_bg": bg, "overlay_fg": fg})
        if self.overlay:
            self.overlay.post_apply_config()

    def overlay_colors_dark(self, icon=None, item_=None): self.overlay_set_colors("black", "white")
    def overlay_colors_light(self, icon=None, item_=None): self.overlay_set_colors("white", "black")
    def overlay_colors_matrix(self, icon=None, item_=None): self.overlay_set_colors("black", "#00FF66")
    def overlay_colors_amber(self, icon=None, item_=None): self.overlay_set_colors("black", "#FFB000")

    def overlay_colors_custom(self, icon=None, item_=None):
        if not self.overlay:
            return
        try:
            resp = queue.Queue()
            self.overlay.post_prompt_colors(resp)
            result = resp.get(timeout=120)
            if result is None:
                return
            bg, fg = result
            self.overlay_set_colors(bg, fg)
        except Exception:
            pass

    def _overlay_colors_menu(self):
        return Menu(
            item("Preset: Dark (black/white)", self.overlay_colors_dark),
            item("Preset: Light (white/black)", self.overlay_colors_light),
            item("Preset: Matrix (green on black)", self.overlay_colors_matrix),
            item("Preset: Amber (amber on black)", self.overlay_colors_amber),
            Menu.SEPARATOR,
            item("Custom...", self.overlay_colors_custom),
        )

    def _overlay_menu(self):
        return Menu(
            item(self._menu_overlay_label, self.overlay_toggle_enabled),
            item(self._menu_drag_label, self.overlay_toggle_lock),
            Menu.SEPARATOR,
            item("Griglia: Top Left", self.overlay_preset_top_left),
            item("Griglia: Top Right", self.overlay_preset_top_right),
            item("Griglia: Bottom Left", self.overlay_preset_bottom_left),
            item("Griglia: Bottom Right", self.overlay_preset_bottom_right),
            item("Griglia: Center", self.overlay_preset_center),
            Menu.SEPARATOR,
            item("Imposta coordinate (X/Y)...", self.overlay_set_coords),
            Menu.SEPARATOR,
            item("Colori...", self._overlay_colors_menu()),
        )

    # ---------- main menu ----------
    def _make_menu(self):
        return Menu(
            item("Copia stats", self.copy_stats),
            item("Apri cartella", self.open_folder),
            item("Ricarica config.json", self.reload_config),
            item("Overlay", self._overlay_menu()),
            item("Restart LHM", self.restart_lhm),
            item("Esci", self.quit),
        )

    def _loop(self):
        while not self._stop.is_set():
            refresh = float(CONFIG.get("refresh_s", 1.0))

            snap = self.sampler.read()
            with self._lock:
                self._last_snapshot = snap

            try:
                self.icon.title = snap.to_text()
            except Exception:
                pass

            if self.overlay:
                try:
                    tokens = snap.overlay_tokens()
                    fmt = str(CONFIG.get("overlay_format", "{cpu}\n{ram}\n{net}\n{disk}\n{gpu}"))
                    text = fmt.format(**tokens).strip() or " "
                    self.overlay.post_set_text(text)
                except Exception:
                    pass

            time.sleep(max(0.25, refresh))

    def copy_stats(self, icon=None, item_=None):
        try:
            import tkinter as _tk
            with self._lock:
                text = self._last_snapshot.to_text()
            r = _tk.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
            r.destroy()
        except Exception:
            pass

    def open_folder(self, icon=None, item_=None):
        try:
            if os.name == "nt":
                os.startfile(self.base_dir)  # type: ignore[attr-defined]
        except Exception:
            pass

    def reload_config(self, icon=None, item_=None):
        load_config_override(self.base_dir)
        self.sampler.reload_net_selection()

        if self.overlay:
            try:
                self.overlay.post_apply_config()
                self.overlay.post_showhide(safe_bool(CONFIG.get("overlay_enabled"), False))
                self.overlay.post_set_locked(safe_bool(CONFIG.get("overlay_locked"), True))

                cx, cy = CONFIG.get("overlay_x", None), CONFIG.get("overlay_y", None)
                if isinstance(cx, int) and isinstance(cy, int):
                    self.overlay.post_set_coords(int(cx), int(cy))
                else:
                    self.overlay.post_set_pos_preset(str(CONFIG.get("overlay_pos", "bottom_right")))
            except Exception:
                pass

    def restart_lhm(self, icon=None, item_=None):
        try:
            self.lhm.stop()
        except Exception:
            pass
        time.sleep(0.5)
        self.lhm = LHMBridge(self.base_dir)
        self.lhm.start_if_available()
        self.sampler.lhm = self.lhm

    def quit(self, icon=None, item_=None):
        self._stop.set()

        try:
            self.lhm.stop()
        except Exception:
            pass

        try:
            if self.overlay:
                self.overlay.post_stop()
        except Exception:
            pass

        try:
            self.icon.stop()
        except Exception:
            pass

    def run_tray(self):
        self.icon.run()


if __name__ == "__main__":
    base = get_base_dir()
    load_config_override(base)

    overlay = OverlayBar(base)
    app = TrayApp(base)
    app.attach_overlay(overlay)

    t = threading.Thread(target=app.run_tray, daemon=True)
    t.start()

    overlay.mainloop()
