"""
Thibado Labs Instrument Suite -- Fast Edition
=============================================
Full multi-instrument suite (Keithley electrometer, Keithley picoammeter,
Fluke 8846A, Andeen-Hagerling AH2550A capacitance bridge) -- rebuilt for
maximum responsiveness on modest hardware.

Architectural differences vs. instrument_suite.py:
    * No matplotlib.  Each tab uses a custom tk.Canvas plot widget that
      pre-allocates all graphical items and only updates coordinates each
      frame.  Min/max-bucket decimation caps the rendered point count at
      ~1500 segments per trace regardless of history length.
    * No pymeasure.  The AH2550A is talked to directly via pyvisa (single
      GPIB command "Q" with regex parsing of "C= ... PF L= ... NS").
    * VISA reads happen on worker threads, one per active device.  The
      GUI never blocks on a `READ?` query.  Workers push samples into a
      Queue; the GUI drains it on a fast timer and redraws on a slower
      one (decoupled rates).
    * Cold start (interpreter + UI) is well under a second.

VISA hardware still requires an IVI/VISA driver (NI-VISA, Keysight, R&S).
The pyvisa Python client is bundled, the OS-level driver is not.

Run:
    python instrument_suite_fast.py
"""

from __future__ import annotations

import csv
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Callable

try:
    import pyvisa
    PYVISA_AVAILABLE = True
except ImportError:
    PYVISA_AVAILABLE = False


# =============================================================================
# Visuals
# =============================================================================
PALETTE = {
    "bg":          "#12141a",
    "panel":       "#1b1e27",
    "panel_alt":   "#232733",
    "border":      "#343848",
    "accent":      "#5cc4ff",
    "accent_alt":  "#ff8a5c",
    "text":        "#e8eaef",
    "text_dim":    "#8b93a7",
    "ok":          "#5cdd8b",
    "warn":        "#ffd66b",
    "err":         "#ff6b7a",
    "grid":        "#2c3040",
    "axis":        "#4e5468",
    "chan_a":      "#5cc4ff",
    "chan_b":      "#ff8a5c",
    "cap":         "#5cc4ff",
    "res":         "#5cdd8b",
}

# Plot tuning
MAX_PLOT_SEGMENTS = 1500
DRAIN_HZ = 60
REDRAW_HZ = 30

NPLC_OPTIONS = ["0.02", "0.2", "1", "10", "100"]

# Fluke 8846A AC filter bandwidth (VOLT:AC / CURR:AC).
# Label -> SCPI value (3 = Slow, 20 = Medium [default], 200 = Fast).
AC_BAND_OPTIONS = [
    ("Fast (200 Hz)",   "200"),
    ("Medium (20 Hz)",  "20"),
    ("Slow (3 Hz)",     "3"),
]
AC_BAND_LABELS = [lbl for lbl, _ in AC_BAND_OPTIONS]
AC_BAND_VALUE_BY_LABEL = {lbl: val for lbl, val in AC_BAND_OPTIONS}

_RE_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")
# AH2550A "Q" reply: C in pF, L (loss/conductance) in nS, V in volts.
# Allow scientific notation; V= field is optional on some firmware/formats.
_RE_CLV = re.compile(
    r"C=\s*(" + _RE_NUMBER.pattern + r")\s*PF\s+"
    r"L=\s*(" + _RE_NUMBER.pattern + r")\s*NS"
    r"(?:\s+V=\s*(" + _RE_NUMBER.pattern + r")\s*V)?",
    re.IGNORECASE,
)


def parse_number(text: str) -> float:
    m = _RE_NUMBER.search(text)
    if not m:
        raise ValueError(f"no numeric value in {text!r}")
    return float(m.group(0))


# -----------------------------------------------------------------------------
# Save-dialog prefs (last folder for CSV / log Save…)
# -----------------------------------------------------------------------------
def default_data_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "InstrumentSuiteData")


def _last_save_dir_prefs_path() -> str:
    return os.path.join(default_data_dir(), "last_save_dir.txt")


def get_last_save_dir() -> str:
    """Directory for Save dialogs; falls back to ~/InstrumentSuiteData."""
    fallback = default_data_dir()
    try:
        prefs = _last_save_dir_prefs_path()
        if os.path.isfile(prefs):
            with open(prefs, "r", encoding="utf-8") as f:
                stored = f.read().strip()
            if stored and os.path.isdir(stored):
                return stored
    except OSError:
        pass
    os.makedirs(fallback, exist_ok=True)
    return fallback


def set_last_save_dir(directory: str) -> None:
    """Remember *directory* for the next Save CSV / Save log dialog."""
    directory = os.path.abspath(directory)
    if not directory or not os.path.isdir(directory):
        return
    try:
        os.makedirs(default_data_dir(), exist_ok=True)
        with open(_last_save_dir_prefs_path(), "w", encoding="utf-8") as f:
            f.write(directory + "\n")
    except OSError:
        pass


def format_eng(value: float, unit: str) -> str:
    if value != value:
        return "---"
    if value == 0:
        return f"0 {unit}" if unit else "0"
    av = abs(value)
    if av < 1e-21:
        return f"0 {unit}" if unit else "0"
    e = int(math.floor(math.log10(av) / 3) * 3)
    e = max(-15, min(12, e))
    prefixes = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m",
                0: "", 3: "k", 6: "M", 9: "G", 12: "T"}
    scaled = value / (10 ** e)
    if abs(scaled) >= 100:
        s = f"{scaled:.2f}"
    elif abs(scaled) >= 10:
        s = f"{scaled:.3f}"
    else:
        s = f"{scaled:.4f}"
    return f"{s} {prefixes[e]}{unit}" if unit else f"{s}{prefixes[e]}"


def format_tick(value: float, unit: str) -> str:
    """Compact axis tick — short enough to sit in the left plot margin."""
    if value != value:
        return "—"
    if abs(value) < 1e-21:
        return "0"
    av = abs(value)
    e = int(math.floor(math.log10(av) / 3) * 3)
    e = max(-15, min(12, e))
    prefixes = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m",
                0: "", 3: "k", 6: "M", 9: "G", 12: "T"}
    scaled = value / (10 ** e)
    if abs(scaled) >= 100:
        s = f"{scaled:.0f}"
    elif abs(scaled) >= 10:
        s = f"{scaled:.1f}"
    else:
        s = f"{scaled:.2f}"
    pref = prefixes[e]
    # Prefer "2.20 nA" over "2.2000e-09 A" style clutter.
    return f"{s}{pref}" if not unit else f"{s}{pref}{unit}"


def apply_theme(root: tk.Tk) -> None:
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root.configure(bg=PALETTE["bg"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    font = ("Segoe UI", 10)
    bold = ("Segoe UI Semibold", 10)
    style.configure(".", background=PALETTE["bg"],
                    foreground=PALETTE["text"], font=font)
    style.configure("TFrame", background=PALETTE["bg"])
    style.configure("Card.TFrame", background=PALETTE["panel"])
    style.configure("CardAlt.TFrame", background=PALETTE["panel_alt"])
    style.configure("TLabel", background=PALETTE["bg"],
                    foreground=PALETTE["text"])
    style.configure("Card.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["text"])
    style.configure("CardAlt.TLabel", background=PALETTE["panel_alt"],
                    foreground=PALETTE["text"])
    style.configure("Dim.TLabel", background=PALETTE["bg"],
                    foreground=PALETTE["text_dim"])
    style.configure("DimCard.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["text_dim"])
    style.configure("DimCardAlt.TLabel", background=PALETTE["panel_alt"],
                    foreground=PALETTE["text_dim"])
    style.configure("Field.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["text_dim"],
                    font=("Segoe UI Semibold", 8))
    style.configure("FieldAlt.TLabel", background=PALETTE["panel_alt"],
                    foreground=PALETTE["text_dim"],
                    font=("Segoe UI Semibold", 8))
    style.configure("Section.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["text_dim"],
                    font=("Segoe UI Semibold", 8))
    style.configure("Header.TLabel", background=PALETTE["bg"],
                    foreground=PALETTE["text"],
                    font=("Segoe UI Semibold", 13))
    style.configure("AppTitle.TLabel", background=PALETTE["bg"],
                    foreground=PALETTE["text"],
                    font=("Segoe UI Semibold", 15))
    style.configure("Big.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["accent"],
                    font=("Consolas", 18, "bold"))
    style.configure("BigB.TLabel", background=PALETTE["panel"],
                    foreground=PALETTE["accent_alt"],
                    font=("Consolas", 18, "bold"))
    style.configure("TButton", padding=(11, 5), font=bold)
    style.map("TButton",
              background=[("active", PALETTE["panel_alt"])])
    style.configure("Accent.TButton", foreground="#0b1220",
                    background=PALETTE["accent"])
    style.map("Accent.TButton",
              background=[("active", "#7ad2ff"),
                          ("disabled", "#3e5566")],
              foreground=[("disabled", "#8aa0b0")])
    style.configure("Danger.TButton", foreground=PALETTE["text"],
                    background="#5a3a45")
    style.map("Danger.TButton",
              background=[("active", "#7a4a55"),
                          ("disabled", "#3e3641")])
    style.configure("TCombobox", fieldbackground=PALETTE["panel_alt"],
                    background=PALETTE["panel_alt"],
                    foreground=PALETTE["text"],
                    padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", PALETTE["panel_alt"])],
              foreground=[("readonly", PALETTE["text"])])
    style.configure("TEntry", fieldbackground=PALETTE["panel_alt"],
                    foreground=PALETTE["text"],
                    insertcolor=PALETTE["text"],
                    padding=4)
    style.configure("TCheckbutton", background=PALETTE["panel"],
                    foreground=PALETTE["text"])
    style.configure("Card.TCheckbutton", background=PALETTE["panel"],
                    foreground=PALETTE["text"])
    style.configure("CardAlt.TCheckbutton", background=PALETTE["panel_alt"],
                    foreground=PALETTE["text"])
    style.configure("Card.TRadiobutton", background=PALETTE["panel"],
                    foreground=PALETTE["text"])
    style.configure("TNotebook", background=PALETTE["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 8),
                    background=PALETTE["panel"], foreground=PALETTE["text_dim"],
                    font=bold)
    style.map("TNotebook.Tab",
              background=[("selected", PALETTE["panel_alt"])],
              foreground=[("selected", PALETTE["accent"])])


# =============================================================================
# Series + Pane (FastPlot data model)
# =============================================================================
@dataclass
class Series:
    name: str
    color: str
    ts: list[float] = field(default_factory=list)
    vs: list[float] = field(default_factory=list)
    # filled in by FastPlot
    line_id: int = 0
    legend_id: int = 0


@dataclass
class Pane:
    label: str
    unit: str
    series: list[Series]
    # filled in by FastPlot
    grid_x: list[int] = field(default_factory=list)
    grid_y: list[int] = field(default_factory=list)
    xtick: list[int] = field(default_factory=list)
    ytick: list[int] = field(default_factory=list)
    axis_x: int = 0
    axis_y: int = 0
    title: int = 0


# =============================================================================
# FastPlot: multi-pane Canvas plot (stacked or side-by-side).
# =============================================================================
class FastPlot(ttk.Frame):
    PAD_L = 108
    PAD_R = 28
    PAD_T = 22
    PAD_B = 36
    PANE_GAP = 20
    N_X_TICKS = 7
    N_Y_TICKS = 5

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style="Card.TFrame")
        self.canvas = tk.Canvas(self, bg=PALETTE["panel"],
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.panes: list[Pane] = []
        self._rolling = True
        self._window_s = 30.0
        self._layout = "stacked"  # "stacked" | "side"
        self._empty_id = self.canvas.create_text(0, 0, text="No data",
                                                 fill=PALETTE["text_dim"],
                                                 font=("Segoe UI", 12))
        self._clip_rects: list[int] = []
        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    # ---- construction -------------------------------------------------
    def set_panes(self, panes: list[Pane]) -> None:
        # Hard reset: remove every canvas item except the empty placeholder so
        # switched dual-mode layouts never leave ghost traces.
        keep = {self._empty_id}
        for item in self.canvas.find_all():
            if item not in keep:
                try:
                    self.canvas.delete(item)
                except Exception:
                    pass
        self.panes = panes
        self._clip_rects = []
        c = self.canvas
        for p in panes:
            p.grid_x = [c.create_line(0, 0, 0, 0, fill=PALETTE["grid"],
                                      width=1)
                        for _ in range(self.N_X_TICKS)]
            p.grid_y = [c.create_line(0, 0, 0, 0, fill=PALETTE["grid"],
                                      width=1)
                        for _ in range(self.N_Y_TICKS)]
            p.xtick = [c.create_text(0, 0, text="",
                                     fill=PALETTE["text_dim"],
                                     font=("Segoe UI", 9))
                       for _ in range(self.N_X_TICKS)]
            p.ytick = [c.create_text(0, 0, text="",
                                     fill=PALETTE["text_dim"],
                                     font=("Consolas", 9),
                                     anchor="e")
                       for _ in range(self.N_Y_TICKS)]
            p.axis_x = c.create_line(0, 0, 0, 0,
                                     fill=PALETTE["axis"], width=1)
            p.axis_y = c.create_line(0, 0, 0, 0,
                                     fill=PALETTE["axis"], width=1)
            p.title = c.create_text(0, 0, text=p.label,
                                    fill=PALETTE["text"],
                                    font=("Segoe UI Semibold", 10),
                                    anchor="nw")
            for s in p.series:
                s.line_id = c.create_line(0, 0, 0, 0, fill=s.color,
                                          width=1.6, smooth=False)
                s.legend_id = c.create_text(0, 0, text="",
                                            fill=s.color,
                                            font=("Segoe UI", 9),
                                            anchor="ne")
        self.redraw()

    def set_window(self, rolling: bool, window_s: float) -> None:
        self._rolling = rolling
        self._window_s = max(0.5, window_s)

    def set_layout(self, layout: str) -> None:
        """Pane arrangement for 2+ panes: 'stacked' or 'side' (side-by-side)."""
        key = (layout or "stacked").strip().lower().replace(" ", "_")
        if key in ("side", "side_by_side", "side-by-side", "sideways", "beside"):
            key = "side"
        else:
            key = "stacked"
        if key != self._layout:
            self._layout = key
            self.redraw()

    def _pane_geometry(self, W: int, H: int, n_panes: int
                       ) -> list[tuple[float, float, float, float, bool]]:
        """Per-pane (plot_left, plot_w, y_top, pane_h, show_xticks)."""
        pl0, pr, pt, pb = self.PAD_L, self.PAD_R, self.PAD_T, self.PAD_B
        gap = self.PANE_GAP if n_panes > 1 else 0
        side = self._layout == "side" and n_panes > 1
        rects: list[tuple[float, float, float, float, bool]] = []
        if side:
            usable = max(10.0, float(W - pr))
            slot_w = max(40.0, (usable - gap * (n_panes - 1)) / n_panes)
            pane_h = max(20.0, float(H - pt - pb))
            for pi in range(n_panes):
                x0 = pi * (slot_w + gap)
                pl = x0 + pl0
                plot_w = max(10.0, slot_w - pl0)
                rects.append((pl, plot_w, float(pt), pane_h, True))
        else:
            plot_w = max(10.0, float(W - pl0 - pr))
            total_h = max(10.0, float(H - pt - pb))
            pane_h = max(20.0, (total_h - gap * (n_panes - 1)) / n_panes)
            for pi in range(n_panes):
                y_top = pt + pi * (pane_h + gap)
                show_x = (pi == n_panes - 1)
                rects.append((float(pl0), plot_w, y_top, pane_h, show_x))
        return rects

    def clear_data(self) -> None:
        for p in self.panes:
            for s in p.series:
                s.ts.clear()
                s.vs.clear()
        self.redraw()

    def mark_dirty(self) -> None:
        # API parity with subclasses; the App schedules redraws.
        pass

    # ---- helpers ------------------------------------------------------
    @staticmethod
    def _decimate(ts: list[float], vs: list[float]
                  ) -> tuple[list[float], list[float]]:
        n = len(ts)
        if n <= MAX_PLOT_SEGMENTS:
            return ts, vs
        n_buckets = MAX_PLOT_SEGMENTS // 2
        bucket = n / n_buckets
        out_t: list[float] = []
        out_v: list[float] = []
        for b in range(n_buckets):
            i = int(b * bucket)
            j = int((b + 1) * bucket)
            if j <= i:
                j = i + 1
            if j > n:
                j = n
            v_min = v_max = vs[i]
            t_min_p = t_max_p = ts[i]
            for k in range(i + 1, j):
                vk = vs[k]
                if vk < v_min:
                    v_min = vk
                    t_min_p = ts[k]
                elif vk > v_max:
                    v_max = vk
                    t_max_p = ts[k]
            if t_min_p <= t_max_p:
                out_t.append(t_min_p); out_v.append(v_min)
                out_t.append(t_max_p); out_v.append(v_max)
            else:
                out_t.append(t_max_p); out_v.append(v_max)
                out_t.append(t_min_p); out_v.append(v_min)
        return out_t, out_v

    @staticmethod
    def _nice_ticks(lo: float, hi: float, n: int) -> list[float]:
        if hi <= lo:
            hi = lo + 1.0
        rng = hi - lo
        raw = rng / max(1, n - 1)
        if raw <= 0:
            return [lo]
        mag = 10 ** math.floor(math.log10(raw))
        norm = raw / mag
        if norm < 1.5:
            step = 1 * mag
        elif norm < 3:
            step = 2 * mag
        elif norm < 7:
            step = 5 * mag
        else:
            step = 10 * mag
        start = math.ceil(lo / step) * step
        ticks: list[float] = []
        v = start
        while v <= hi + step * 0.001 and len(ticks) < n + 1:
            ticks.append(v)
            v += step
        return ticks

    @staticmethod
    def _start_index(ts: list[float], t_min: float) -> int:
        n = len(ts)
        if n == 0 or ts[0] >= t_min:
            return 0
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if ts[mid] < t_min:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _shared_x_range(self) -> tuple[float, float]:
        all_first: list[float] = []
        all_last: list[float] = []
        for p in self.panes:
            for s in p.series:
                if s.ts:
                    all_first.append(s.ts[0])
                    all_last.append(s.ts[-1])
        if not all_first:
            return 0.0, 1.0
        if self._rolling:
            t_max = max(all_last)
            t_min = max(0.0, t_max - self._window_s)
        else:
            t_min = min(all_first)
            t_max = max(all_last)
        if t_max <= t_min:
            t_max = t_min + 1e-9
        return t_min, t_max

    # ---- main redraw --------------------------------------------------
    def redraw(self) -> None:
        c = self.canvas
        W = max(int(c.winfo_width()), 100)
        H = max(int(c.winfo_height()), 80)

        has_data = any(s.ts for p in self.panes for s in p.series)

        for p in self.panes:
            ids = (p.grid_x + p.grid_y + p.xtick + p.ytick
                   + [p.axis_x, p.axis_y, p.title]
                   + [s.line_id for s in p.series]
                   + [s.legend_id for s in p.series])
            state = "normal" if has_data else "hidden"
            for i in ids:
                c.itemconfig(i, state=state)
        if not has_data:
            c.coords(self._empty_id, W / 2, H / 2)
            c.itemconfig(self._empty_id, state="normal")
            return
        c.itemconfig(self._empty_id, state="hidden")

        n_panes = len(self.panes)
        if n_panes == 0:
            return
        rects = self._pane_geometry(W, H, n_panes)

        t_min, t_max = self._shared_x_range()
        tx_span = t_max - t_min
        xt = self._nice_ticks(t_min, t_max, self.N_X_TICKS)

        for pi, pane in enumerate(self.panes):
            pl, plot_w, y_top, pane_h, show_xticks = rects[pi]
            y_bot = y_top + pane_h

            def sx(t: float, pl=pl, plot_w=plot_w) -> float:
                return pl + (t - t_min) / tx_span * plot_w

            # Title at top of pane (above plot area so it never collides)
            c.coords(pane.title, pl, y_top - 16)
            title_txt = pane.label
            if pane.unit:
                title_txt = f"{pane.label}  [{pane.unit}]"
            c.itemconfig(pane.title, text=title_txt)

            # Axis lines
            c.coords(pane.axis_x, pl, y_bot, pl + plot_w, y_bot)
            c.coords(pane.axis_y, pl, y_top, pl, y_bot)

            # Tk canvas clip is unreliable across platforms; clamp coords below.

            # Determine Y range from this pane's series
            vlo: float | None = None
            vhi: float | None = None
            for s in pane.series:
                if not s.ts:
                    continue
                start_idx = self._start_index(s.ts, t_min)
                if start_idx >= len(s.vs):
                    continue
                seg = s.vs[start_idx:]
                if not seg:
                    continue
                lo = min(seg); hi = max(seg)
                vlo = lo if vlo is None else min(vlo, lo)
                vhi = hi if vhi is None else max(vhi, hi)
            if vlo is None:
                vlo, vhi = 0.0, 1.0
            if vhi == vlo:
                pad = max(1e-12, abs(vlo) * 0.05 if vlo else 1.0)
                vlo -= pad; vhi += pad
            else:
                pad = (vhi - vlo) * 0.08
                vlo -= pad; vhi += pad
            vy_span = vhi - vlo

            def sy(v: float, y_top=y_top, vlo=vlo, vy_span=vy_span,
                   pane_h=pane_h) -> float:
                return y_top + pane_h - (v - vlo) / vy_span * pane_h

            # X grid
            for i, it in enumerate(pane.grid_x):
                if i < len(xt):
                    x = sx(xt[i])
                    c.coords(it, x, y_top, x, y_bot)
                    c.itemconfig(it, state="normal")
                else:
                    c.itemconfig(it, state="hidden")
            for i, it in enumerate(pane.xtick):
                if i < len(xt) and show_xticks:
                    x = sx(xt[i])
                    c.coords(it, x, y_bot + 14)
                    c.itemconfig(it, text=f"{xt[i]:.1f}s", state="normal")
                else:
                    c.itemconfig(it, state="hidden")

            # Y grid and labels (compact ticks; unit lives in pane title)
            yt = self._nice_ticks(vlo, vhi, self.N_Y_TICKS)
            for i, it in enumerate(pane.grid_y):
                if i < len(yt):
                    y = sy(yt[i])
                    c.coords(it, pl, y, pl + plot_w, y)
                    c.itemconfig(it, state="normal")
                else:
                    c.itemconfig(it, state="hidden")
            for i, it in enumerate(pane.ytick):
                if i < len(yt):
                    y = min(y_bot - 2, max(y_top + 2, sy(yt[i])))
                    c.coords(it, pl - 10, y)
                    c.itemconfig(it,
                                 text=format_tick(yt[i], pane.unit),
                                 state="normal")
                else:
                    c.itemconfig(it, state="hidden")

            # Traces — clamp into plot box so they never cross into labels
            legend_y = y_top + 4
            x_lo, x_hi = pl, pl + plot_w
            y_lo, y_hi = y_top, y_bot
            for s in pane.series:
                if not s.ts:
                    c.itemconfig(s.line_id, state="hidden")
                    c.itemconfig(s.legend_id, state="hidden")
                    continue
                start_idx = self._start_index(s.ts, t_min)
                vts = s.ts[start_idx:]
                vvs = s.vs[start_idx:]
                if not vts:
                    c.itemconfig(s.line_id, state="hidden")
                    c.itemconfig(s.legend_id, state="hidden")
                    continue
                dts, dvs = self._decimate(vts, vvs)
                flat: list[float] = []
                ext = flat.extend
                for t, v in zip(dts, dvs):
                    x = min(x_hi, max(x_lo, sx(t)))
                    y = min(y_hi, max(y_lo, sy(v)))
                    ext((x, y))
                if len(flat) >= 4:
                    c.coords(s.line_id, *flat)
                    c.itemconfig(s.line_id, state="normal")
                else:
                    c.itemconfig(s.line_id, state="hidden")
                # legend (top-right of pane)
                c.coords(s.legend_id, pl + plot_w - 6, legend_y)
                c.itemconfig(s.legend_id, text=f"{s.name}", state="normal")
                legend_y += 14

            # Keep axis labels above traces
            for it in pane.ytick:
                c.tag_raise(it)
            c.tag_raise(pane.title)


# =============================================================================
# Live data log window
# =============================================================================
class LiveLogWindow(tk.Toplevel):
    """A pop-out scrolling text log of incoming samples.

    Backed by a rolling deque owned by the parent panel; reopening the
    window re-seeds it from the buffer so history is never lost while
    measurements continue.
    """
    MAX_DISPLAY_LINES = 6000

    def __init__(self, parent: tk.Misc, title: str,
                 get_buffer, on_close=None) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("820x480")
        self.configure(bg=PALETTE["bg"])
        self.get_buffer = get_buffer
        self._on_close = on_close

        bar = ttk.Frame(self, style="Card.TFrame", padding=(8, 6))
        bar.pack(fill="x")
        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Auto-scroll",
                        variable=self.auto_var,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Button(bar, text="Clear",
                   command=self.clear).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="Save log…",
                   command=self.save).pack(side="left", padx=4)
        self.lines_label = ttk.Label(bar, text="0 lines",
                                     style="DimCard.TLabel")
        self.lines_label.pack(side="right")

        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, bg=PALETTE["panel"], fg=PALETTE["text"],
                            insertbackground=PALETTE["text"],
                            wrap="none", borderwidth=0,
                            font=("Consolas", 10))
        ysb = ttk.Scrollbar(body, orient="vertical",
                            command=self.text.yview)
        self.text.config(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="disabled")

        self._seed()
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

    def _seed(self) -> None:
        buf = self.get_buffer()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if buf:
            self.text.insert("end", "\n".join(buf) + "\n")
        self.text.configure(state="disabled")
        self.lines_label.config(text=f"{len(buf)} lines")
        if self.auto_var.get():
            self.text.see("end")

    def append_lines(self, lines) -> None:
        if not lines:
            return
        self.text.configure(state="normal")
        self.text.insert("end", "\n".join(lines) + "\n")
        end_idx = self.text.index("end-1c")
        n = int(end_idx.split(".")[0])
        if n > self.MAX_DISPLAY_LINES:
            self.text.delete(
                "1.0", f"{n - (self.MAX_DISPLAY_LINES - 1000)}.0")
        self.text.configure(state="disabled")
        if self.auto_var.get():
            self.text.see("end")
        buf = self.get_buffer()
        self.lines_label.config(text=f"{len(buf)} lines")

    def clear(self) -> None:
        buf = self.get_buffer()
        buf.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.lines_label.config(text="0 lines")

    def save(self) -> None:
        buf = self.get_buffer()
        if not buf:
            messagebox.showwarning("Empty log", "Nothing to save yet.")
            return
        name = "Log_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
        path = filedialog.asksaveasfilename(
            initialdir=get_last_save_dir(), initialfile=name,
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(buf))
                f.write("\n")
            set_last_save_dir(os.path.dirname(path))
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _handle_close(self) -> None:
        try:
            if self._on_close is not None:
                self._on_close()
        finally:
            self.destroy()


# =============================================================================
# Worker thread (generic)
# =============================================================================
class Worker(threading.Thread):
    def __init__(self, channel: str, addr: str,
                 setup_fn: Callable[..., None],
                 read_fn: Callable[..., dict[str, float]],
                 interval_sec: float,
                 data_q: queue.Queue, status_q: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.channel = channel
        self.addr = addr
        self.setup_fn = setup_fn
        self.read_fn = read_fn
        self.interval_sec = max(0.0, interval_sec)
        self.data_q = data_q
        self.status_q = status_q
        self.stop_evt = threading.Event()

    def _post(self, level: str, msg: str) -> None:
        self.status_q.put((level, f"[{self.channel}] {msg}"))

    def run(self) -> None:
        if not PYVISA_AVAILABLE:
            self._post("err", "pyvisa not installed.")
            return
        rm = None
        inst = None
        try:
            rm = pyvisa.ResourceManager()
            inst = rm.open_resource(self.addr)
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            inst.timeout = 10000
            self.setup_fn(inst, self._post)

            t0 = time.perf_counter()
            next_t = 0.0
            while not self.stop_evt.is_set():
                now = time.perf_counter() - t0
                wait = next_t - now
                if wait > 0:
                    if self.stop_evt.wait(min(wait, 0.05)):
                        break
                    continue
                try:
                    sample = self.read_fn(inst)
                    self.data_q.put((self.channel,
                                     time.perf_counter() - t0,
                                     sample))
                except Exception as e:
                    self._post("err", f"read error: {e}")
                    if self.stop_evt.wait(0.2):
                        break
                if self.interval_sec > 0:
                    next_t += self.interval_sec
                    if (time.perf_counter() - t0) - next_t > 1.0:
                        next_t = time.perf_counter() - t0
                else:
                    next_t = time.perf_counter() - t0
        except Exception as e:
            self._post("err", f"VISA error: {e}")
        finally:
            try:
                if inst is not None:
                    inst.close()
            except Exception:
                pass
            try:
                if rm is not None:
                    rm.close()
            except Exception:
                pass
            self._post("info", "disconnected.")


class SimWorker(threading.Thread):
    """Synthetic traces for UI demos only (no VISA). Start uses Worker."""

    def __init__(self, channel: str, meas_key: str, interval_sec: float,
                 data_q: queue.Queue, status_q: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.channel = channel
        self.meas_key = meas_key
        self.interval_sec = max(0.02, interval_sec)  # keep GUI responsive
        self.data_q = data_q
        self.status_q = status_q
        self.stop_evt = threading.Event()
        self._phase = 0.0 if channel == "A" else 1.3

    def _post(self, level: str, msg: str) -> None:
        self.status_q.put((level, f"[{self.channel}] {msg}"))

    def _value_at(self, t: float) -> float:
        ph = self._phase
        if self.meas_key == "DC Voltage":
            # ~2.4 V with slow drift + ripple
            return (2.40
                    + 0.28 * math.sin(2 * math.pi * 0.12 * t + ph)
                    + 0.04 * math.sin(2 * math.pi * 1.8 * t)
                    + 0.01 * math.sin(2 * math.pi * 7.0 * t + ph))
        if self.meas_key == "DC Current":
            # Distinct nanoamp-scale currents for A vs B
            base = 2.2e-9 if self.channel == "A" else 8.5e-9
            return base * (1.0
                           + 0.22 * math.sin(2 * math.pi * 0.18 * t + ph)
                           + 0.05 * math.sin(2 * math.pi * 0.9 * t))
        # Charge: slow ramp with tiny noise-like ripple
        return (5.0e-10 * t
                + 2.0e-11 * math.sin(2 * math.pi * 0.25 * t + ph))

    def run(self) -> None:
        self._post("ok", f"sim {self.meas_key}")
        t0 = time.perf_counter()
        next_t = 0.0
        while not self.stop_evt.is_set():
            now = time.perf_counter() - t0
            wait = next_t - now
            if wait > 0:
                if self.stop_evt.wait(min(wait, 0.05)):
                    break
                continue
            t = time.perf_counter() - t0
            self.data_q.put((self.channel, t, self._sample_at(t)))
            next_t += self.interval_sec
            if (time.perf_counter() - t0) - next_t > 1.0:
                next_t = time.perf_counter() - t0
        self._post("info", "sim stopped.")

    def _sample_at(self, t: float) -> dict[str, float]:
        # Cap bridge expects cap (pF) + res (GΩ); other panels use "value".
        if self.meas_key.lower().startswith("cap"):
            cap = (120.0
                   + 8.0 * math.sin(2 * math.pi * 0.11 * t + self._phase)
                   + 1.5 * math.sin(2 * math.pi * 0.7 * t))
            res = (2.4
                   + 0.35 * math.sin(2 * math.pi * 0.09 * t + self._phase)
                   + 0.05 * math.sin(2 * math.pi * 0.55 * t))
            return {"cap": cap, "res": max(1e-6, res)}
        return {"value": self._value_at(t)}


# =============================================================================
# Base panel
# =============================================================================
class BasePanel(ttk.Frame):
    """Common scaffolding for every instrument tab."""

    TITLE = ""
    SUBTITLE = ""
    SUPPORTS_DUAL = True
    SUPPORTS_RANGE = False
    DEFAULT_ADDRS = ("GPIB0::1::INSTR", "GPIB0::2::INSTR")
    MEAS_TYPES: dict[str, dict] = {}
    # Per-measurement manual range presets, keyed by MEAS_TYPES key.
    # Each entry is a list of (display label, SCPI value) tuples.  A missing
    # key or empty list means only autorange is offered for that measurement.
    RANGE_PRESETS: dict[str, list[tuple[str, str]]] = {}

    def __init__(self, parent: tk.Misc,
                 get_rm: Callable[[], object | None],
                 set_status: Callable[[str, str], None]) -> None:
        super().__init__(parent, style="TFrame")
        self.get_rm = get_rm
        self.set_status = set_status

        self.workers: dict[str, Worker] = {}
        self.data_q: queue.Queue = queue.Queue()
        self.status_q: queue.Queue = queue.Queue()
        # False while stopped so late worker samples cannot append after Stop.
        self._accepting_samples = False
        self._dirty = False
        self._paused = False
        self._rate_window: deque[float] = deque(maxlen=20)

        self.series_objs: dict[str, Series] = {}
        self.readout_vars: dict[str, tk.StringVar] = {}

        # Live-log state
        self._log_buffer: deque[str] = deque(maxlen=5000)
        self._pending_log: list[str] = []
        self._log_window: LiveLogWindow | None = None

        self.dual_var = tk.BooleanVar(value=False)
        self.func_var = tk.StringVar(
            value=next(iter(self.MEAS_TYPES.keys())) if self.MEAS_TYPES else "")
        self.interval_var = tk.StringVar(value="100")
        # Auto-stop duration in minutes. 0 / blank = run until Stop is pressed.
        self.timer_var = tk.StringVar(value="0")
        self._timer_after_id: str | None = None
        self._timer_deadline: float | None = None
        self.window_var = tk.StringVar(value="30")
        self.rolling_var = tk.BooleanVar(value=True)
        self.plot_layout_var = tk.StringVar(value="stacked")
        # "Auto" = autorange; any numeric string = fixed manual range.
        self.range_var = tk.StringVar(value="Auto")
        self.addr_vars = [tk.StringVar(value=self.DEFAULT_ADDRS[0]),
                          tk.StringVar(value=self.DEFAULT_ADDRS[1]
                                       if len(self.DEFAULT_ADDRS) > 1
                                       else "")]

        self._build_ui()
        self._on_function_changed()
        self._on_mode_changed()
        self._rebuild_plot()

        self.after(int(1000 / DRAIN_HZ), self._drain)
        self.after(int(1000 / REDRAW_HZ), self._redraw_tick)

    # ---- subclass hooks ----------------------------------------------
    def _build_setup_fn(self) -> Callable[..., None]:
        raise NotImplementedError

    def _build_setup_fn_for(self, channel: str) -> Callable[..., None]:
        """Per-channel setup; default shares one config across devices."""
        return self._build_setup_fn()

    def _build_read_fn(self) -> Callable[..., dict[str, float]]:
        raise NotImplementedError

    def _series_ids(self) -> list[str]:
        return ["A", "B"] if (self.SUPPORTS_DUAL and self.dual_var.get()) else ["A"]

    def _series_color(self, sid: str) -> str:
        return {"A": PALETTE["chan_a"], "B": PALETTE["chan_b"]}.get(
            sid, PALETTE["accent"])

    def _series_label(self, sid: str) -> str:
        mt = self.MEAS_TYPES[self.func_var.get()]
        if sid == "A":
            return f"Ch A · {mt.get('label', self.func_var.get())}"
        if sid == "B":
            return f"Ch B · {mt.get('label', self.func_var.get())}"
        return sid

    def _channels(self) -> list[str]:
        return ["A", "B"] if (self.SUPPORTS_DUAL and self.dual_var.get()) else ["A"]

    def _route(self, channel: str,
               sample: dict[str, float]) -> list[tuple[str, float]]:
        # Default: a single-value sample feeds into the per-channel series.
        return [(channel, float(sample.get("value", float("nan"))))]

    def _build_pane_specs(self) -> list[Pane]:
        mt = self.MEAS_TYPES[self.func_var.get()]
        sids = self._series_ids()
        series = [Series(self._series_label(sid),
                         self._series_color(sid)) for sid in sids]
        # remember refs so we can find them by sid -> series object
        self.series_objs = dict(zip(sids, series))
        return [Pane(mt.get("label", self.func_var.get()),
                     mt.get("unit", ""), series)]

    def _extra_controls(self, parent: tk.Misc) -> None:
        return None

    def _extra_setup_kwargs(self) -> dict:
        return {}

    def _field(self, parent: tk.Misc, caption: str,
               *, alt: bool = False, padx: tuple[int, int] = (0, 18)) -> ttk.Frame:
        """Caption column; create the control as a child of the returned frame."""
        box = ttk.Frame(parent, style="CardAlt.TFrame" if alt else "Card.TFrame")
        box.pack(side="left", padx=padx, anchor="n")
        style = "FieldAlt.TLabel" if alt else "Field.TLabel"
        ttk.Label(box, text=caption.upper(), style=style).pack(anchor="w")
        return box

    def _device_card(self, parent: tk.Misc, title: str, color: str
                     ) -> tuple[ttk.Frame, ttk.Frame]:
        """Accent-bar card used for per-device setup."""
        card = ttk.Frame(parent, style="CardAlt.TFrame")
        bar = tk.Frame(card, bg=color, width=3, highlightthickness=0)
        bar.pack(side="left", fill="y")
        inner = ttk.Frame(card, style="CardAlt.TFrame", padding=(12, 10))
        inner.pack(side="left", fill="both", expand=True)
        ttk.Label(inner, text=title.upper(), style="FieldAlt.TLabel",
                  foreground=color).pack(anchor="w", pady=(0, 8))
        return card, inner

    # ---- UI -----------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- settings card ----
        settings = ttk.Frame(self, style="Card.TFrame", padding=(14, 12))
        self._settings_card = settings
        settings.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        settings.columnconfigure(0, weight=1)

        if self.SUBTITLE:
            ttk.Label(settings, text=self.SUBTITLE,
                      style="DimCard.TLabel").grid(row=0, column=0,
                                                   sticky="w", pady=(0, 10))

        acq = ttk.Frame(settings, style="Card.TFrame")
        acq.grid(row=1, column=0, sticky="ew")

        interval_box = self._field(acq, "Interval (ms)")
        ttk.Entry(interval_box, textvariable=self.interval_var,
                  width=8).pack(anchor="w", pady=(4, 0))

        timer_box = self._field(acq, "Timer (min)")
        self.timer_entry = ttk.Entry(timer_box, textvariable=self.timer_var,
                                     width=8)
        self.timer_entry.pack(anchor="w", pady=(4, 0))

        if self.SUPPORTS_DUAL:
            dual_box = ttk.Frame(acq, style="Card.TFrame")
            ttk.Label(dual_box, text="MODE", style="Field.TLabel").pack(anchor="w")
            self.dual_check = ttk.Checkbutton(
                dual_box, text="Dual device",
                variable=self.dual_var, style="Card.TCheckbutton",
                command=self._on_mode_changed)
            self.dual_check.pack(anchor="w", pady=(4, 0))
            dual_box.pack(side="left", padx=(0, 18), anchor="n")

        visa_box = ttk.Frame(acq, style="Card.TFrame")
        ttk.Label(visa_box, text="VISA", style="Field.TLabel").pack(anchor="w")
        ttk.Button(visa_box, text="Refresh", width=10,
                   command=self._refresh_addrs).pack(anchor="w", pady=(4, 0))
        visa_box.pack(side="left", padx=(0, 18), anchor="n")

        self._extras_frame = ttk.Frame(acq, style="Card.TFrame")
        self._extras_frame.pack(side="left", anchor="n")
        self._extra_controls(self._extras_frame)

        # Instrument / device controls share this same strip (no second row).
        self._build_instrument_row(acq)
        self._settings_colspan = 1

        # ---- plot ----
        self.plot = FastPlot(self)
        self.plot.grid(row=1, column=0, sticky="nsew",
                       padx=10, pady=(0, 6))

        # ---- footer: actions + live readouts + view + stats ----
        footer = ttk.Frame(self, style="Card.TFrame", padding=(12, 6))
        footer.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        actions = ttk.Frame(footer, style="Card.TFrame")
        actions.pack(side="left")
        self.start_btn = ttk.Button(actions, text="Start",
                                    style="Accent.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = ttk.Button(actions, text="Stop",
                                   command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(actions, text="Pause",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        ttk.Button(actions, text="Clear",
                   command=self.clear_data).pack(side="left", padx=4)
        ttk.Button(actions, text="Save CSV",
                   command=self.save_csv).pack(side="left", padx=4)
        ttk.Button(actions, text="Live log",
                   command=self.open_log).pack(side="left", padx=4)
        self.sim_btn = ttk.Button(actions, text="Simulate",
                                  command=self.start_simulation)
        self.sim_btn.pack(side="left", padx=(10, 4))

        # LIVE A/B strip shares the footer row (no separate row stealing plot height)
        self._readout_card = ttk.Frame(footer, style="Card.TFrame")
        self._readout_card.pack(side="left", padx=(16, 0))

        view = ttk.Frame(footer, style="Card.TFrame")
        view.pack(side="left", padx=(18, 0))
        ttk.Label(view, text="VIEW", style="Field.TLabel").pack(anchor="w")
        view_row = ttk.Frame(view, style="Card.TFrame")
        view_row.pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(view_row, text="Rolling",
                        variable=self.rolling_var,
                        style="Card.TCheckbutton",
                        command=self._mark_dirty).pack(side="left")
        ttk.Label(view_row, text="last",
                  style="DimCard.TLabel").pack(side="left", padx=(10, 4))
        win_entry = ttk.Entry(view_row, textvariable=self.window_var, width=5)
        win_entry.pack(side="left")
        ttk.Label(view_row, text="s",
                  style="DimCard.TLabel").pack(side="left", padx=(4, 0))
        win_entry.bind("<KeyRelease>", lambda _e: self._mark_dirty())

        layout_row = ttk.Frame(view, style="Card.TFrame")
        layout_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(layout_row, text="Layout:",
                  style="DimCard.TLabel").pack(side="left")
        ttk.Radiobutton(layout_row, text="Stacked",
                        value="stacked", variable=self.plot_layout_var,
                        style="Card.TRadiobutton",
                        command=self._on_layout_changed).pack(
                            side="left", padx=(8, 0))
        ttk.Radiobutton(layout_row, text="Side by side",
                        value="side", variable=self.plot_layout_var,
                        style="Card.TRadiobutton",
                        command=self._on_layout_changed).pack(
                            side="left", padx=(8, 0))

        stats = ttk.Frame(footer, style="Card.TFrame")
        stats.pack(side="right")
        self.timer_remain_label = ttk.Label(stats, text="",
                                            style="DimCard.TLabel",
                                            foreground=PALETTE["warn"])
        self.timer_remain_label.pack(side="right", padx=(12, 0))
        self.sample_label = ttk.Label(stats, text="0 samples",
                                      style="DimCard.TLabel")
        self.sample_label.pack(side="right", padx=(12, 0))
        self.rate_label = ttk.Label(stats, text="0.0 Hz",
                                    style="DimCard.TLabel")
        self.rate_label.pack(side="right", padx=(12, 0))

    def _build_instrument_row(self, parent: tk.Misc) -> None:
        """Measurement, range, and addresses — packed into the acq strip."""
        self._meas_row = parent
        self._addr_row = parent

        meas_box = self._field(parent, "Measurement")
        self.meas_label = meas_box.winfo_children()[0]
        self.func_combo = ttk.Combobox(meas_box, textvariable=self.func_var,
                                       values=list(self.MEAS_TYPES.keys()),
                                       state="readonly", width=22)
        self.func_combo.pack(anchor="w", pady=(4, 0))
        self.func_combo.bind("<<ComboboxSelected>>",
                             lambda _e: self._on_function_changed())

        if self.SUPPORTS_RANGE:
            range_box = self._field(parent, "Range")
            self.range_label = range_box.winfo_children()[0]
            self.range_combo = ttk.Combobox(
                range_box, textvariable=self.range_var,
                values=["Auto"], state="readonly", width=14)
            self.range_combo.pack(anchor="w", pady=(4, 0))
        else:
            self.range_label = None

        addr_a = self._field(parent, "Device A")
        self.addr_combo_a = ttk.Combobox(addr_a, textvariable=self.addr_vars[0],
                                         values=self._scan_resources(),
                                         width=24)
        self.addr_combo_a.pack(anchor="w", pady=(4, 0))

        self._b_field = self._field(parent, "Device B", padx=(0, 0))
        self.addr_label_b = self._b_field.winfo_children()[0]
        self.addr_combo_b = ttk.Combobox(self._b_field,
                                         textvariable=self.addr_vars[1],
                                         values=self._scan_resources(),
                                         width=24)
        self.addr_combo_b.pack(anchor="w", pady=(4, 0))
        if not self.SUPPORTS_DUAL:
            self._b_field.pack_forget()

    def _rebuild_readouts(self) -> None:
        for child in self._readout_card.winfo_children():
            child.destroy()
        self.readout_vars.clear()
        sids = list(self.series_objs.keys())
        n = len(sids)
        ttk.Label(self._readout_card, text="LIVE",
                  style="Field.TLabel").pack(side="left", padx=(0, 10))
        for i, sid in enumerate(sids):
            if sid == "A":
                color = PALETTE["chan_a"]
            elif sid == "B":
                color = PALETTE["chan_b"]
            else:
                color = (PALETTE["accent"] if i % 2 == 0
                         else PALETTE["accent_alt"])
            col = ttk.Frame(self._readout_card, style="Card.TFrame")
            col.pack(side="left", padx=(0 if i == 0 else 14, 0))
            label_text = self.series_objs[sid].name
            ttk.Label(col, text=label_text.upper(),
                      style="Field.TLabel",
                      foreground=color).pack(anchor="w")
            var = tk.StringVar(value="—")
            if sid == "B" or (sid not in ("A", "B") and i % 2 == 1):
                style_name = "BigB.TLabel"
            else:
                style_name = "Big.TLabel"
            if n == 1:
                style_name = "Big.TLabel"
            ttk.Label(col, textvariable=var, style=style_name,
                      foreground=color, anchor="w").pack(anchor="w")
            self.readout_vars[sid] = var

    def _is_cap_panel(self) -> bool:
        return False

    # ---- VISA -------------------------------------------------------
    def _scan_resources(self) -> list[str]:
        items: list[str] = []
        rm = self.get_rm()
        if rm is not None:
            try:
                items = list(rm.list_resources())
            except Exception:
                items = []
        if not items:
            items = [f"GPIB0::{i}::INSTR" for i in range(1, 31)]
        return items

    def _refresh_addrs(self) -> None:
        items = self._scan_resources()
        self.addr_combo_a["values"] = items
        self.addr_combo_b["values"] = items
        self.set_status(f"VISA list refreshed ({len(items)} entries)", "info")

    # ---- control --------------------------------------------------
    def _refresh_range_options(self) -> None:
        """Repopulate the manual-range dropdown for the current measurement.

        Always offers "Auto" first; appends this measurement's presets (if any).
        Resets the selection to "Auto" when the previous choice is no longer
        valid for the newly selected measurement.
        """
        if not self.SUPPORTS_RANGE or not hasattr(self, "range_combo"):
            return
        presets = self.RANGE_PRESETS.get(self.func_var.get(), [])
        labels = ["Auto"] + [lbl for lbl, _ in presets]
        self.range_combo["values"] = labels
        if self.range_var.get() not in labels:
            self.range_var.set("Auto")

    def _resolve_range(self) -> str:
        """Return the SCPI range value for the current selection, or "" for auto."""
        if not self.SUPPORTS_RANGE:
            return ""
        sel = self.range_var.get()
        if sel == "Auto":
            return ""
        for lbl, val in self.RANGE_PRESETS.get(self.func_var.get(), []):
            if lbl == sel:
                return val
        return ""

    def _on_function_changed(self) -> None:
        self._refresh_range_options()
        self._rebuild_plot()

    def _set_run_controls_locked(self, locked: bool) -> None:
        """Subclass hook to lock/unlock extra controls while a run is active."""
        return None

    def _on_mode_changed(self) -> None:
        dual = self.SUPPORTS_DUAL and self.dual_var.get()
        state = "!disabled" if dual else "disabled"
        self.addr_combo_b.state([state])
        self.addr_label_b.configure(
            foreground=PALETTE["chan_b"] if dual else PALETTE["text_dim"])
        self._rebuild_plot()

    def _rebuild_plot(self) -> None:
        panes = self._build_pane_specs()
        self.plot.set_panes(panes)
        self.plot.set_layout(self.plot_layout_var.get())
        self._rebuild_readouts()
        self._dirty = True

    def _on_layout_changed(self) -> None:
        self.plot.set_layout(self.plot_layout_var.get())
        self._dirty = True

    def _mark_dirty(self) -> None:
        self._dirty = True

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_btn.config(
            text="Resume" if self._paused else "Pause")

    def clear_data(self) -> None:
        for s in self.series_objs.values():
            s.ts.clear(); s.vs.clear()
        self._rate_window.clear()
        for var in self.readout_vars.values():
            var.set("—")
        self.sample_label.config(text="0 samples")
        self.rate_label.config(text="0.0 Hz")
        self._log_buffer.clear()
        self._pending_log.clear()
        if self._log_window is not None and self._log_window.winfo_exists():
            self._log_window.clear()
        self.plot.redraw()

    def open_log(self) -> None:
        if self._log_window is not None and self._log_window.winfo_exists():
            self._log_window.deiconify()
            self._log_window.lift()
            self._log_window.focus_force()
            return
        self._log_window = LiveLogWindow(
            self, f"{self.TITLE} -- Live log",
            get_buffer=lambda: self._log_buffer,
            on_close=self._on_log_window_closed)

    def _on_log_window_closed(self) -> None:
        self._log_window = None

    def _format_log_line(self, t: float, channel: str,
                         sample: dict[str, float]) -> str:
        parts: list[str] = []
        for sid, val in self._route(channel, sample):
            s = self.series_objs.get(sid)
            unit = ""
            if s is not None:
                for p in self.plot.panes:
                    if s in p.series:
                        unit = p.unit
                        break
            if sid in ("A", "B"):
                label = f"Ch {sid}"
            else:
                label = sid
            parts.append(f"{label}: {format_eng(val, unit)}")
        return f"[t={t:9.3f} s]  " + "    ".join(parts)

    @staticmethod
    def _format_countdown(seconds: float) -> str:
        sec = max(0.0, seconds)
        if sec >= 3600:
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            return f"{h}:{m:02d}:{s:02d}"
        if sec >= 60:
            m = int(sec // 60)
            s = int(sec % 60)
            return f"{m}:{s:02d}"
        return f"{sec:.1f} s"

    def _cancel_run_timer(self) -> None:
        if self._timer_after_id is not None:
            try:
                self.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        self._timer_deadline = None
        if hasattr(self, "timer_remain_label"):
            self.timer_remain_label.config(text="")

    def _tick_run_timer(self) -> None:
        if self._timer_deadline is None or not self.workers:
            self._cancel_run_timer()
            return
        remaining = self._timer_deadline - time.perf_counter()
        if remaining <= 0:
            self._cancel_run_timer()
            self.stop()
            self.set_status(
                f"{self.TITLE}: timer elapsed — stopped.", "ok")
            return
        self.timer_remain_label.config(
            text=f"{self._format_countdown(remaining)} left")
        self._timer_after_id = self.after(200, self._tick_run_timer)

    def _sim_meas_key(self, channel: str) -> str:
        """Measurement name used by SimWorker for this channel."""
        return self.func_var.get()

    def _flush_queues(self) -> None:
        """Drop any pending samples/status so stopped workers cannot ghost."""
        try:
            while True:
                self.data_q.get_nowait()
        except queue.Empty:
            pass
        try:
            while True:
                self.status_q.get_nowait()
        except queue.Empty:
            pass

    def _prepare_run(self) -> float | None:
        """Validate interval/timer and clear series. Returns interval_sec or None."""
        if self.workers:
            return None
        self._accepting_samples = False
        try:
            interval_ms = float(self.interval_var.get())
            if not (0.0 <= interval_ms <= 60000.0):
                raise ValueError
        except ValueError:
            messagebox.showerror("Bad interval",
                                 "Interval must be 0-60000 ms.")
            return None
        try:
            timer_min = float(self.timer_var.get().strip() or "0")
            if not (0.0 <= timer_min <= 1440.0):
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Bad timer",
                "Timer must be 0-1440 minutes (0 = no auto-stop).")
            return None
        self._pending_timer_sec = timer_min * 60.0

        self._flush_queues()
        for s in self.series_objs.values():
            s.ts.clear(); s.vs.clear()
        self._rate_window.clear()
        for var in self.readout_vars.values():
            var.set("—")
        self.sample_label.config(text="0 samples")
        self.rate_label.config(text="0.0 Hz")
        self._log_buffer.clear()
        self._pending_log.clear()
        if self._log_window is not None and self._log_window.winfo_exists():
            self._log_window.clear()
        self.plot.redraw()
        return interval_ms / 1000.0

    def _lock_run_ui(self, running: bool, *, status: str) -> None:
        if running:
            self.start_btn.state(["disabled"])
            if hasattr(self, "sim_btn"):
                self.sim_btn.state(["disabled"])
            self.stop_btn.state(["!disabled"])
            self.func_combo.state(["disabled"])
            self.timer_entry.state(["disabled"])
            if self.SUPPORTS_DUAL:
                self.dual_check.state(["disabled"])
            if self.SUPPORTS_RANGE and hasattr(self, "range_combo"):
                self.range_combo.state(["disabled"])
            self._set_run_controls_locked(True)
            self.addr_combo_a.state(["disabled"])
            self.addr_combo_b.state(["disabled"])
            self.set_status(status, "ok")
        else:
            self.start_btn.state(["!disabled"])
            if hasattr(self, "sim_btn"):
                self.sim_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
            self.func_combo.state(["!disabled"])
            self.timer_entry.state(["!disabled"])
            if self.SUPPORTS_DUAL:
                self.dual_check.state(["!disabled"])
            if self.SUPPORTS_RANGE and hasattr(self, "range_combo"):
                self.range_combo.state(["!disabled"])
            self._set_run_controls_locked(False)
            self.addr_combo_a.state(["!disabled"])
            if self.SUPPORTS_DUAL and self.dual_var.get():
                self.addr_combo_b.state(["!disabled"])
            self.set_status(status, "info")

    def start(self) -> None:
        if not PYVISA_AVAILABLE:
            messagebox.showerror(
                "pyvisa missing",
                "Install pyvisa + a VISA backend (e.g. NI-VISA) to "
                "talk to instruments.\n\nUse Simulate for a demo without hardware.")
            return
        interval_sec = self._prepare_run()
        if interval_sec is None:
            return
        timer_sec = getattr(self, "_pending_timer_sec", 0.0)

        read_fn = self._build_read_fn()
        addr_lookup = {"A": self.addr_vars[0].get(),
                       "B": self.addr_vars[1].get()}
        self._accepting_samples = True
        for ch in self._channels():
            w = Worker(channel=ch, addr=addr_lookup[ch],
                       setup_fn=self._build_setup_fn_for(ch),
                       read_fn=read_fn,
                       interval_sec=interval_sec,
                       data_q=self.data_q, status_q=self.status_q)
            self.workers[ch] = w
            w.start()

        self._cancel_run_timer()
        if timer_sec > 0:
            self._timer_deadline = time.perf_counter() + timer_sec
            self._tick_run_timer()
            self._lock_run_ui(
                True,
                status=(f"{self.TITLE}: running… auto-stop in "
                        f"{self._format_countdown(timer_sec)}"))
        else:
            self._lock_run_ui(True, status=f"{self.TITLE}: running…")

    def start_simulation(self) -> None:
        """Run synthetic dual/single traces without instruments (demo only)."""
        interval_sec = self._prepare_run()
        if interval_sec is None:
            return
        timer_sec = getattr(self, "_pending_timer_sec", 0.0)
        if interval_sec <= 0:
            interval_sec = 0.05

        self._accepting_samples = True
        for ch in self._channels():
            w = SimWorker(channel=ch,
                          meas_key=self._sim_meas_key(ch),
                          interval_sec=interval_sec,
                          data_q=self.data_q, status_q=self.status_q)
            self.workers[ch] = w
            w.start()

        self._cancel_run_timer()
        if timer_sec > 0:
            self._timer_deadline = time.perf_counter() + timer_sec
            self._tick_run_timer()
            self._lock_run_ui(
                True,
                status=(f"{self.TITLE}: SIMULATING… auto-stop in "
                        f"{self._format_countdown(timer_sec)}"))
        else:
            self._lock_run_ui(True, status=f"{self.TITLE}: SIMULATING…")

    def stop(self) -> None:
        # Reject samples before signaling workers so late posts cannot append.
        self._accepting_samples = False
        self._cancel_run_timer()
        workers = list(self.workers.values())
        for w in workers:
            w.stop_evt.set()
        # Wait briefly so old SimWorkers/VISA workers cannot keep posting
        # into a freshly rebuilt dual-mode plot (ghost double traces).
        deadline = time.perf_counter() + 0.35
        for w in workers:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                w.join(timeout=max(0.01, remaining))
            except Exception:
                pass
        self.workers.clear()
        self._flush_queues()
        self._lock_run_ui(False, status=f"{self.TITLE}: stopped.")

    def save_csv(self) -> None:
        # Single-series: Time + value. Multi-series: each series gets its
        # own Time_* column beside its values (no shared-clock sync).
        sids = list(self.series_objs.keys())
        if not any(self.series_objs[sid].ts for sid in sids):
            messagebox.showwarning("No data", "Nothing to save yet.")
            return
        name = (self.TITLE.replace(" ", "")
                + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
        path = filedialog.asksaveasfilename(initialdir=get_last_save_dir(),
                                            initialfile=name,
                                            defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"),
                                                       ("All files", "*.*")])
        if not path:
            return
        multi = len(sids) > 1
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                header: list[str] = []
                for sid in sids:
                    s = self.series_objs[sid]
                    pane = next((p for p in self.plot.panes
                                 if s in p.series), None)
                    unit = pane.unit if pane else ""
                    t_col = f"Time_{sid} (s)" if multi else "Time (s)"
                    header.extend([t_col, f"{s.name} [{unit}]"])
                w.writerow(header)
                max_len = max(len(self.series_objs[sid].ts) for sid in sids)
                for i in range(max_len):
                    row: list[str] = []
                    for sid in sids:
                        s = self.series_objs[sid]
                        if i < len(s.ts) and i < len(s.vs):
                            row.append(f"{s.ts[i]:.6f}")
                            row.append(f"{s.vs[i]:.9e}")
                        else:
                            row.extend(["", ""])
                    w.writerow(row)
            set_last_save_dir(os.path.dirname(path))
            self.set_status(
                f"Saved {max_len} rows to {path}", "ok")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ---- timers ---------------------------------------------------
    def _drain(self) -> None:
        pulled = 0
        try:
            while pulled < 512:
                channel, t, sample = self.data_q.get_nowait()
                if not self._accepting_samples:
                    # Stale post after Stop / before next Start — drop it.
                    pulled += 1
                    continue
                try:
                    for sid, val in self._route(channel, sample):
                        s = self.series_objs.get(sid)
                        if s is None:
                            continue
                        # Always pair this worker's timestamp with its values.
                        s.ts.append(t)
                        s.vs.append(val)
                        if sid in self.readout_vars:
                            self.readout_vars[sid].set(
                                self._format_value(sid, val))
                    self._rate_window.append(time.perf_counter())
                    line = self._format_log_line(t, channel, sample)
                    self._log_buffer.append(line)
                    self._pending_log.append(line)
                except Exception as e:
                    self.set_status(
                        f"{self.TITLE}: sample error: {e}", "err")
                pulled += 1
        except queue.Empty:
            pass
        if pulled and self._accepting_samples:
            total = sum(len(s.ts) for s in self.series_objs.values())
            self.sample_label.config(text=f"{total} samples")
            if len(self._rate_window) >= 2:
                span = self._rate_window[-1] - self._rate_window[0]
                hz = (len(self._rate_window) - 1) / span if span > 0 else 0.0
                self.rate_label.config(text=f"{hz:.1f} Hz")
            self._dirty = True
        try:
            while True:
                level, msg = self.status_q.get_nowait()
                self.set_status(f"{self.TITLE}: {msg}", level)
        except queue.Empty:
            pass
        self.after(int(1000 / DRAIN_HZ), self._drain)

    def _redraw_tick(self) -> None:
        if self._dirty and not self._paused:
            try:
                ws = float(self.window_var.get())
            except ValueError:
                ws = 30.0
            self.plot.set_window(self.rolling_var.get(), ws)
            self.plot.set_layout(self.plot_layout_var.get())
            self.plot.redraw()
            self._dirty = False
        # Flush pending log lines to the open log window.
        if self._pending_log and (self._log_window is not None
                                  and self._log_window.winfo_exists()):
            flush = self._pending_log
            self._pending_log = []
            try:
                self._log_window.append_lines(flush)
            except tk.TclError:
                self._log_window = None
        elif self._pending_log and len(self._pending_log) > 1000:
            # Window closed; clip pending so it doesn't grow unbounded.
            self._pending_log = self._pending_log[-500:]
        self.after(int(1000 / REDRAW_HZ), self._redraw_tick)

    def _format_value(self, sid: str, val: float) -> str:
        # Find the unit for this series via its pane.
        s = self.series_objs.get(sid)
        if s is None:
            return format_eng(val, "")
        for pane in self.plot.panes:
            if s in pane.series:
                return format_eng(val, pane.unit)
        return format_eng(val, "")


# =============================================================================
# Setup / read helpers
# =============================================================================
def _setup_fluke(conf: str, nplc: str, ac_band: str = "",
                 rng: str = "") -> Callable[..., None]:
    def setup(inst, post):
        inst.write("*RST"); time.sleep(0.3)
        inst.write("*CLS"); time.sleep(0.05)
        inst.write(f"CONF:{conf}"); time.sleep(0.1)
        if conf != "FREQ":
            if rng:
                # Manual fixed range: disable autorange, then pin the range.
                try:
                    inst.write(f"{conf}:RANG:AUTO OFF"); time.sleep(0.05)
                    inst.write(f"{conf}:RANG {rng}"); time.sleep(0.05)
                    post("info", f"range {rng}")
                except Exception:
                    pass
            else:
                try:
                    inst.write(f"{conf}:RANG:AUTO ON")
                except Exception:
                    pass
        if nplc and conf in ("VOLT:DC", "CURR:DC", "RES", "FRES"):
            try:
                inst.write(f"{conf}:NPLC {nplc}")
            except Exception:
                pass
        if ac_band and conf in ("VOLT:AC", "CURR:AC"):
            # AC filter bandwidth (3 = Slow, 20 = Medium, 200 = Fast).
            try:
                inst.write(f"{conf}:BAND {ac_band}")
            except Exception:
                pass
        try:
            inst.write("TRIG:SOUR IMM")
            inst.write("SAMP:COUN 1")
            inst.write("TRIG:COUN 1")
        except Exception:
            pass
        try:
            actual = inst.query("FUNC?").strip().strip('"')
            wanted = conf.replace(":", "").upper()
            actual_norm = actual.replace(":", "").replace(" ", "").upper()
            if wanted not in actual_norm and actual_norm not in wanted:
                post("err",
                     f"Meter reports {actual!r}, requested {conf!r}")
            else:
                post("ok", f"function {actual}")
        except Exception:
            post("ok", "configured.")
    return setup


def _read_fluke() -> Callable[..., dict[str, float]]:
    def read(inst):
        return {"value": parse_number(inst.query("READ?"))}
    return read


def _setup_keithley(func: str, rng: str = "") -> Callable[..., None]:
    def setup(inst, post):
        inst.write("*RST"); time.sleep(0.3)
        inst.write("*CLS"); time.sleep(0.05)
        inst.write(f":SENS:FUNC '{func}'"); time.sleep(0.1)
        if rng:
            # Manual fixed range: disable autorange, then set upper range.
            try:
                inst.write(f":SENS:{func}:RANG:AUTO OFF"); time.sleep(0.05)
                inst.write(f":SENS:{func}:RANG {rng}"); time.sleep(0.05)
                post("info", f"range {rng}")
            except Exception:
                pass
        else:
            try:
                inst.write(f":SENS:{func}:RANG:AUTO ON"); time.sleep(0.05)
            except Exception:
                pass
        try:
            inst.write(":FORM:ELEM READ"); time.sleep(0.05)
        except Exception:
            pass
        try:
            inst.write(":SYST:ZCH OFF"); time.sleep(0.1)
        except Exception:
            pass
        try:
            err = inst.query(":SYST:ERR?").strip()
            if '0,"No error"' not in err:
                post("warn", f"config: {err}")
            else:
                post("ok", f"function {func}")
        except Exception:
            post("ok", "configured.")
    return setup


def _read_keithley() -> Callable[..., dict[str, float]]:
    def read(inst):
        return {"value": parse_number(inst.query(":READ?"))}
    return read


def _setup_cap_bridge(avg_exp: int) -> Callable[..., None]:
    def setup(inst, post):
        try:
            inst.write(f"AV {avg_exp}"); time.sleep(0.1)
            post("ok", f"AV={avg_exp}")
        except Exception as e:
            post("warn", f"AV set failed: {e}")
    return setup


def _read_cap_bridge() -> Callable[..., dict[str, float]]:
    def read(inst):
        raw = inst.query("Q")
        m = _RE_CLV.search(raw if isinstance(raw, str)
                           else " ".join(map(str, raw)))
        if not m:
            if "EXCESS NOISE" in raw.upper():
                return {"cap": float("nan"), "res": float("nan")}
            raise ValueError(f"unparsable AH2550A reply: {raw!r}")
        c_pF = float(m.group(1))
        l_nS = float(m.group(2))
        if abs(l_nS) > 1e-9:
            r_GOhm = 1.0 / l_nS
        else:
            r_GOhm = 1e4
        return {"cap": c_pF, "res": r_GOhm}
    return read


# =============================================================================
# Concrete panels
# =============================================================================
class ElectrometerPanel(BasePanel):
    TITLE = "Keithley Electrometer"
    SUBTITLE = "Voltage / Current / Charge   (e.g., 6517B)"
    SUPPORTS_RANGE = True
    DEFAULT_ADDRS = ("GPIB0::27::INSTR", "GPIB0::28::INSTR")
    MEAS_TYPES = {
        "DC Voltage": {"func": "VOLT", "unit": "V", "label": "Voltage"},
        "DC Current": {"func": "CURR", "unit": "A", "label": "Current"},
        "Charge":     {"func": "CHAR", "unit": "C", "label": "Charge"},
    }
    # Upper-range presets sent as :SENS:<func>:RANG <value> (base SI units).
    RANGE_PRESETS = {
        "DC Voltage": [
            ("2 V", "2"), ("20 V", "20"), ("200 V", "200"),
        ],
        "DC Current": [
            ("20 pA", "20e-12"), ("200 pA", "200e-12"),
            ("2 nA", "2e-9"), ("20 nA", "20e-9"), ("200 nA", "200e-9"),
            ("2 µA", "2e-6"), ("20 µA", "20e-6"), ("200 µA", "200e-6"),
            ("2 mA", "2e-3"), ("20 mA", "20e-3"),
        ],
        "Charge": [
            ("2 nC", "2e-9"), ("20 nC", "20e-9"),
            ("200 nC", "200e-9"), ("2 µC", "2e-6"),
        ],
    }

    def __init__(self, parent: tk.Misc,
                 get_rm: Callable[[], object | None],
                 set_status: Callable[[str, str], None]) -> None:
        # Device B measurement/range (created before BasePanel builds UI so
        # _build_ui can wire the dual-mode controls).
        self.func_var_b = tk.StringVar(value="DC Current")
        self.range_var_b = tk.StringVar(value="Auto")
        self._dual_devices: ttk.Frame | None = None
        super().__init__(parent, get_rm, set_status)
        self._refresh_range_options_b()

    def _build_instrument_row(self, parent: tk.Misc) -> None:
        """Per-device Addr/Meas/Range on the same strip as Interval/Timer/VISA."""
        self._dual_devices = parent
        self._meas_row = parent
        self._addr_row = parent

        # ---- Device A (always visible) ----
        addr_a = self._field(parent, "Device A")
        addr_a.winfo_children()[0].configure(foreground=PALETTE["chan_a"])
        self.addr_combo_a = ttk.Combobox(
            addr_a, textvariable=self.addr_vars[0],
            values=self._scan_resources(), width=22)
        self.addr_combo_a.pack(anchor="w", pady=(4, 0))

        meas_box = self._field(parent, "Meas A")
        self.meas_label = meas_box.winfo_children()[0]
        self.meas_label.configure(foreground=PALETTE["chan_a"])
        self.func_combo = ttk.Combobox(
            meas_box, textvariable=self.func_var,
            values=list(self.MEAS_TYPES.keys()),
            state="readonly", width=14)
        self.func_combo.pack(anchor="w", pady=(4, 0))
        self.func_combo.bind("<<ComboboxSelected>>",
                             lambda _e: self._on_function_changed())

        range_box = self._field(parent, "Range A")
        self.range_label = range_box.winfo_children()[0]
        self.range_label.configure(foreground=PALETTE["chan_a"])
        self.range_combo = ttk.Combobox(
            range_box, textvariable=self.range_var,
            values=["Auto"], state="readonly", width=10)
        self.range_combo.pack(anchor="w", pady=(4, 0))

        # ---- Device B (shown only in Dual) ----
        col_b = ttk.Frame(parent, style="Card.TFrame")
        self._device_col_b = col_b
        self._b_field = col_b

        addr_b = self._field(col_b, "Device B")
        self.addr_label_b = addr_b.winfo_children()[0]
        self.addr_label_b.configure(foreground=PALETTE["chan_b"])
        self.addr_combo_b = ttk.Combobox(
            addr_b, textvariable=self.addr_vars[1],
            values=self._scan_resources(), width=22)
        self.addr_combo_b.pack(anchor="w", pady=(4, 0))

        meas_b = self._field(col_b, "Meas B")
        meas_b.winfo_children()[0].configure(foreground=PALETTE["chan_b"])
        self.func_combo_b = ttk.Combobox(
            meas_b, textvariable=self.func_var_b,
            values=list(self.MEAS_TYPES.keys()),
            state="readonly", width=14)
        self.func_combo_b.pack(anchor="w", pady=(4, 0))
        self.func_combo_b.bind("<<ComboboxSelected>>",
                               lambda _e: self._on_function_b_changed())

        range_b = self._field(col_b, "Range B", padx=(0, 0))
        range_b.winfo_children()[0].configure(foreground=PALETTE["chan_b"])
        self.range_combo_b = ttk.Combobox(
            range_b, textvariable=self.range_var_b,
            values=["Auto"], state="readonly", width=10)
        self.range_combo_b.pack(anchor="w", pady=(4, 0))

        self._apply_device_layout(False)

    def _apply_device_layout(self, dual: bool) -> None:
        if not hasattr(self, "_device_col_b"):
            return
        if dual:
            self._device_col_b.pack(side="left", anchor="n")
        else:
            self._device_col_b.pack_forget()

    def _meas_key_for(self, channel: str) -> str:
        if channel == "B":
            return self.func_var_b.get()
        return self.func_var.get()

    def _refresh_range_options_b(self) -> None:
        if not hasattr(self, "range_combo_b"):
            return
        presets = self.RANGE_PRESETS.get(self.func_var_b.get(), [])
        labels = ["Auto"] + [lbl for lbl, _ in presets]
        self.range_combo_b["values"] = labels
        if self.range_var_b.get() not in labels:
            self.range_var_b.set("Auto")

    def _resolve_range_for(self, channel: str) -> str:
        if channel == "B":
            sel = self.range_var_b.get()
            key = self.func_var_b.get()
        else:
            sel = self.range_var.get()
            key = self.func_var.get()
        if sel == "Auto":
            return ""
        for lbl, val in self.RANGE_PRESETS.get(key, []):
            if lbl == sel:
                return val
        return ""

    def _on_function_b_changed(self) -> None:
        self._refresh_range_options_b()
        self._rebuild_plot()

    def _on_mode_changed(self) -> None:
        dual = self.SUPPORTS_DUAL and self.dual_var.get()
        if dual:
            a_key = self.func_var.get()
            if a_key in ("DC Voltage", "Charge"):
                self.func_var_b.set("DC Current")
            else:
                self.func_var_b.set("DC Voltage")
            self._refresh_range_options_b()
            if hasattr(self, "func_combo_b") and not self.workers:
                self.func_combo_b.state(["!disabled"])
                self.range_combo_b.state(["!disabled"])
                self.addr_combo_b.state(["!disabled"])
        self._apply_device_layout(dual)
        # Skip BasePanel label rewriting; layout is handled above.
        # Still need address B enable + plot rebuild from base logic:
        state = "!disabled" if dual else "disabled"
        if hasattr(self, "addr_combo_b"):
            self.addr_combo_b.state([state])
        self._rebuild_plot()

    def _set_run_controls_locked(self, locked: bool) -> None:
        if not hasattr(self, "func_combo_b"):
            return
        dual = self.SUPPORTS_DUAL and self.dual_var.get()
        if locked:
            self.func_combo_b.state(["disabled"])
            self.range_combo_b.state(["disabled"])
        elif dual:
            # Must clear disabled explicitly — state(["readonly"]) does not.
            self.func_combo_b.state(["!disabled"])
            self.range_combo_b.state(["!disabled"])
        else:
            self.func_combo_b.state(["disabled"])
            self.range_combo_b.state(["disabled"])

    def _series_label(self, sid: str) -> str:
        key = self._meas_key_for(sid)
        mt = self.MEAS_TYPES[key]
        if sid in ("A", "B"):
            return f"Ch {sid} · {mt.get('label', key)}"
        return sid

    def _build_pane_specs(self) -> list[Pane]:
        if not (self.SUPPORTS_DUAL and self.dual_var.get()):
            return super()._build_pane_specs()
        panes: list[Pane] = []
        self.series_objs = {}
        for sid in ("A", "B"):
            key = self._meas_key_for(sid)
            mt = self.MEAS_TYPES[key]
            series = Series(self._series_label(sid), self._series_color(sid))
            self.series_objs[sid] = series
            panes.append(Pane(f"Ch {sid} · {mt.get('label', key)}",
                              mt.get("unit", ""), [series]))
        return panes

    def _sim_meas_key(self, channel: str) -> str:
        return self._meas_key_for(channel)

    def _build_setup_fn(self):
        func = self.MEAS_TYPES[self.func_var.get()]["func"]
        return _setup_keithley(func, self._resolve_range_for("A"))

    def _build_setup_fn_for(self, channel: str):
        key = self._meas_key_for(channel)
        func = self.MEAS_TYPES[key]["func"]
        return _setup_keithley(func, self._resolve_range_for(channel))

    def _build_read_fn(self):
        return _read_keithley()


class PicoammeterPanel(BasePanel):
    TITLE = "Keithley Picoammeter"
    SUBTITLE = "Low-current ammeter   (e.g., 6485)"
    SUPPORTS_RANGE = True
    DEFAULT_ADDRS = ("GPIB0::27::INSTR", "GPIB0::28::INSTR")
    MEAS_TYPES = {
        "DC Current": {"func": "CURR", "unit": "A", "label": "Current"},
    }
    RANGE_PRESETS = {
        "DC Current": [
            ("2 nA", "2e-9"), ("20 nA", "20e-9"), ("200 nA", "200e-9"),
            ("2 µA", "2e-6"), ("20 µA", "20e-6"), ("200 µA", "200e-6"),
            ("2 mA", "2e-3"), ("20 mA", "20e-3"),
        ],
    }

    def _build_setup_fn(self):
        return _setup_keithley("CURR", self._resolve_range())

    def _build_read_fn(self):
        return _read_keithley()


class FlukePanel(BasePanel):
    TITLE = "Fluke 8846A"
    SUBTITLE = "DC/AC voltage, current, resistance, frequency"
    SUPPORTS_RANGE = True
    DEFAULT_ADDRS = ("GPIB0::22::INSTR", "GPIB0::23::INSTR")
    MEAS_TYPES = {
        "DC Voltage":          {"conf": "VOLT:DC", "unit": "V",  "label": "Voltage",       "nplc": True,  "ac": False},
        "AC Voltage":          {"conf": "VOLT:AC", "unit": "V",  "label": "Voltage RMS",   "nplc": False, "ac": True},
        "DC Current":          {"conf": "CURR:DC", "unit": "A",  "label": "Current",       "nplc": True,  "ac": False},
        "AC Current":          {"conf": "CURR:AC", "unit": "A",  "label": "Current RMS",   "nplc": False, "ac": True},
        "Resistance (2-wire)": {"conf": "RES",     "unit": "Ω",  "label": "Resistance",    "nplc": True,  "ac": False},
        "Resistance (4-wire)": {"conf": "FRES",    "unit": "Ω",  "label": "Resistance 4W", "nplc": True,  "ac": False},
        "Frequency":           {"conf": "FREQ",    "unit": "Hz", "label": "Frequency",     "nplc": False, "ac": False},
    }
    # Fixed-range presets sent as CONF:<conf> <range> (base SI units).
    # Frequency has no meaningful range setting, so it gets Auto only.
    RANGE_PRESETS = {
        "DC Voltage": [
            ("100 mV", "0.1"), ("1 V", "1"), ("10 V", "10"),
            ("100 V", "100"), ("1000 V", "1000"),
        ],
        "AC Voltage": [
            ("100 mV", "0.1"), ("1 V", "1"), ("10 V", "10"),
            ("100 V", "100"), ("750 V", "750"),
        ],
        "DC Current": [
            ("100 µA", "100e-6"), ("1 mA", "1e-3"), ("10 mA", "10e-3"),
            ("100 mA", "100e-3"), ("1 A", "1"), ("3 A", "3"),
        ],
        "AC Current": [
            ("10 mA", "10e-3"), ("100 mA", "100e-3"),
            ("1 A", "1"), ("3 A", "3"),
        ],
        "Resistance (2-wire)": [
            ("100 Ω", "100"), ("1 kΩ", "1e3"), ("10 kΩ", "10e3"),
            ("100 kΩ", "100e3"), ("1 MΩ", "1e6"), ("10 MΩ", "10e6"),
            ("100 MΩ", "100e6"),
        ],
        "Resistance (4-wire)": [
            ("100 Ω", "100"), ("1 kΩ", "1e3"), ("10 kΩ", "10e3"),
            ("100 kΩ", "100e3"), ("1 MΩ", "1e6"), ("10 MΩ", "10e6"),
            ("100 MΩ", "100e6"),
        ],
    }

    def _extra_controls(self, parent: tk.Misc) -> None:
        self.nplc_var = tk.StringVar(value="1")
        nplc_box = self._field(parent, "NPLC")
        self.nplc_combo = ttk.Combobox(nplc_box, textvariable=self.nplc_var,
                                       values=NPLC_OPTIONS, width=6,
                                       state="readonly")
        self.nplc_combo.pack(anchor="w", pady=(4, 0))

        self.ac_band_var = tk.StringVar(value=AC_BAND_LABELS[1])  # Medium
        ac_box = self._field(parent, "AC filter", padx=(0, 0))
        self.ac_band_combo = ttk.Combobox(ac_box,
                                          textvariable=self.ac_band_var,
                                          values=AC_BAND_LABELS, width=14,
                                          state="readonly")
        self.ac_band_combo.pack(anchor="w", pady=(4, 0))

    def _on_function_changed(self) -> None:
        mt = self.MEAS_TYPES[self.func_var.get()]
        if hasattr(self, "nplc_combo"):
            if mt["nplc"]:
                self.nplc_combo.state(["!disabled"])
            else:
                self.nplc_combo.state(["disabled"])
        if hasattr(self, "ac_band_combo"):
            if mt["ac"]:
                self.ac_band_combo.state(["!disabled"])
            else:
                self.ac_band_combo.state(["disabled"])
        super()._on_function_changed()

    def _build_setup_fn(self):
        mt = self.MEAS_TYPES[self.func_var.get()]
        nplc = self.nplc_var.get() if mt["nplc"] else ""
        ac_band = (AC_BAND_VALUE_BY_LABEL.get(self.ac_band_var.get(), "")
                   if mt["ac"] else "")
        return _setup_fluke(mt["conf"], nplc, ac_band, self._resolve_range())

    def _build_read_fn(self):
        return _read_fluke()


class CapBridgePanel(BasePanel):
    TITLE = "AH2550A Capacitance Bridge"
    SUBTITLE = "Capacitance + Loss / Resistance  (precision)"
    SUPPORTS_DUAL = False
    DEFAULT_ADDRS = ("GPIB0::28::INSTR",)
    MEAS_TYPES = {
        "Cap + Loss (R)": {"unit": "pF", "label": "Capacitance + Resistance"},
    }

    def _extra_controls(self, parent: tk.Misc) -> None:
        self.avg_var = tk.StringVar(value="10")
        avg_box = self._field(parent, "Avg exp", padx=(0, 0))
        ttk.Entry(avg_box, textvariable=self.avg_var,
                  width=5).pack(anchor="w", pady=(4, 0))

    def _build_setup_fn(self):
        try:
            avg = max(0, min(15, int(self.avg_var.get() or "10")))
        except ValueError:
            avg = 10
        return _setup_cap_bridge(avg)

    def _build_read_fn(self):
        return _read_cap_bridge()

    def _route(self, channel: str,
               sample: dict[str, float]) -> list[tuple[str, float]]:
        # Keys must match _read_cap_bridge / Cap SimWorker (not "value").
        return [("cap", float(sample.get("cap", float("nan")))),
                ("res", float(sample.get("res", float("nan"))))]

    def _build_pane_specs(self) -> list[Pane]:
        cap_series = Series("Capacitance (pF)", PALETTE["cap"])
        res_series = Series("Resistance (GΩ)", PALETTE["res"])
        self.series_objs = {"cap": cap_series, "res": res_series}
        return [
            Pane("Capacitance", "pF", [cap_series]),
            Pane("Resistance", "GΩ", [res_series]),
        ]


# =============================================================================
# Main application
# =============================================================================
class InstrumentSuiteFastApp(tk.Tk):
    def __init__(self, demo: bool = False) -> None:
        super().__init__()
        self.title("Thibado Labs Instrument Suite -- Fast Edition")
        self.geometry("1320x880")
        self.minsize(1100, 720)
        apply_theme(self)
        self._demo = demo
        self._notebook: ttk.Notebook | None = None

        self.rm = None
        if PYVISA_AVAILABLE:
            try:
                self.rm = pyvisa.ResourceManager()
            except Exception:
                self.rm = None

        self._build_ui()
        if not PYVISA_AVAILABLE:
            self._set_status("pyvisa not installed -- use Simulate for demos.",
                             "warn")
        elif self.rm is None:
            self._set_status("VISA backend missing -- use Simulate for demos.",
                             "warn")
        else:
            self._set_status("Ready.", "ok")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if demo:
            self.after(400, self._demo_voltage_current)

    def _build_ui(self) -> None:
        banner = ttk.Frame(self, style="TFrame")
        banner.pack(fill="x", padx=14, pady=(10, 0))
        ttk.Label(banner, text="Thibado Labs",
                  style="AppTitle.TLabel").pack(side="left")
        ttk.Label(banner, text="Instrument Suite",
                  style="Dim.TLabel").pack(side="left", padx=(8, 0), pady=(4, 0))
        ttk.Label(banner, text="Fast Edition",
                  style="Dim.TLabel").pack(side="right")

        accent = tk.Frame(self, bg=PALETTE["accent"], height=2,
                          highlightthickness=0)
        accent.pack(fill="x", padx=14, pady=(8, 0))

        notebook = ttk.Notebook(self)
        self._notebook = notebook
        notebook.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        get_rm = lambda: self.rm
        self.electro = ElectrometerPanel(notebook, get_rm, self._set_status)
        notebook.add(self.electro, text="Electrometer")
        self.pico = PicoammeterPanel(notebook, get_rm, self._set_status)
        notebook.add(self.pico, text="Picoammeter")
        self.fluke = FlukePanel(notebook, get_rm, self._set_status)
        notebook.add(self.fluke, text="Fluke 8846A")
        self.cap = CapBridgePanel(notebook, get_rm, self._set_status)
        notebook.add(self.cap, text="Capacitance Bridge")

        bar = tk.Frame(self, bg=PALETTE["panel"], height=26,
                       highlightthickness=0)
        bar.pack(side="bottom", fill="x")
        pip = tk.Frame(bar, bg=PALETTE["ok"], width=6, highlightthickness=0)
        pip.pack(side="left", fill="y")
        self._status_pip = pip
        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = tk.Label(bar, textvariable=self.status_var,
                                     bg=PALETTE["panel"],
                                     fg=PALETTE["text_dim"],
                                     anchor="w", font=("Segoe UI", 9))
        self.status_label.pack(side="left", fill="x", expand=True, padx=10, pady=4)
        visa_txt = "PyVISA ready" if (PYVISA_AVAILABLE and self.rm is not None) \
            else "PyVISA unavailable"
        tk.Label(bar, text=visa_txt, bg=PALETTE["panel"],
                 fg=PALETTE["text_dim"], font=("Segoe UI", 9)).pack(
                     side="right", padx=10)

    def _set_status(self, msg: str, level: str = "info") -> None:
        color = {"ok": PALETTE["ok"], "warn": PALETTE["warn"],
                 "err": PALETTE["err"], "info": PALETTE["text_dim"]
                 }.get(level, PALETTE["text_dim"])
        self.status_label.config(fg=color)
        self._status_pip.config(bg=color if level in ("ok", "warn", "err")
                                else PALETTE["text_dim"])
        self.status_var.set(msg)

    def _demo_prepare_electro(self, meas_a: str, meas_b: str,
                              range_a: str, range_b: str) -> None:
        if self._notebook is not None:
            self._notebook.select(0)
        e = self.electro
        if e.workers:
            e.stop()
        e._flush_queues()
        e.dual_var.set(True)
        # Avoid _on_mode_changed resetting B to a complementary function —
        # set measurements explicitly, then apply layout + rebuild once.
        e.func_var.set(meas_a)
        e.func_var_b.set(meas_b)
        e._refresh_range_options()
        e._refresh_range_options_b()
        if range_a in (e.range_combo["values"] or ()):
            e.range_var.set(range_a)
        if range_b in (e.range_combo_b["values"] or ()):
            e.range_var_b.set(range_b)
        e.interval_var.set("50")
        e.window_var.set("20")
        e.rolling_var.set(True)
        e._apply_device_layout(True)
        if hasattr(e, "addr_combo_b"):
            e.addr_combo_b.state(["!disabled"])
        if hasattr(e, "func_combo_b"):
            e.func_combo_b.state(["!disabled"])
            e.range_combo_b.state(["!disabled"])
        e._rebuild_plot()

    def _demo_voltage_current(self) -> None:
        self._demo_prepare_electro(
            "DC Voltage", "DC Current", "20 V", "20 nA")
        self.electro.start_simulation()
        self._set_status(
            "DEMO 1/2 — Dual: Voltage (A) + Current (B). Switching in 12 s…",
            "ok")
        self.after(12000, self._demo_current_current)

    def _demo_current_current(self) -> None:
        self._demo_prepare_electro(
            "DC Current", "DC Current", "20 nA", "20 nA")
        self.electro.start_simulation()
        self._set_status(
            "DEMO 2/2 — Dual: Current (A) + Current (B). Running…",
            "ok")

    def _on_close(self) -> None:
        for panel in (self.electro, self.pico, self.fluke, self.cap):
            try:
                panel.stop()
            except Exception:
                pass
        self.destroy()


def main() -> None:
    demo = "--demo" in sys.argv
    app = InstrumentSuiteFastApp(demo=demo)
    app.mainloop()


if __name__ == "__main__":
    main()
