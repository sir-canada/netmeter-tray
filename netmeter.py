#!/usr/bin/env python3
"""netmeter — tiny systray network meter.

Two vertical scan-lined bars: green = download, red = upload.
Right-click -> Configure:
  Devices tab     — checkbox + rename per interface
  Sensitivity tab — per-device max download / max upload (shared unit),
                    only for devices enabled in the Devices tab
  General tab     — segment count, update interval, smoothing
Apply = preview changes live, OK = apply + close, Cancel = discard.

Each enabled device contributes rate/max as a percentage; percentages of
all devices are summed and shown as the bar level.
"""

import copy
import json
import os
import shlex
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QDoubleValidator, QIcon, \
    QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
    QSlider, QSpinBox, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

CONFIG_PATH = os.path.expanduser("~/.config/netmeter/config.json")

UNITS = [("B/s", 1), ("KB/s", 1024), ("MB/s", 1024 ** 2), ("GB/s", 1024 ** 3)]
UNIT_MULT = dict(UNITS)

DEFAULT_MAX_BPS = 1000 * 1024      # 1000 KB/s
DEFAULT_UNIT = "KB/s"
OLD_DEFAULT_BPS = 10 * 1024 ** 2   # previous default, reset on migration

DEFAULT_CONFIG = {
    # per-device: {"enabled": bool, "alias": str, "unit": str,
    #              "max_down_bps": float, "max_up_bps": float}
    "devices": {},
    "segments": 7,           # scan-line ridges per bar
    "interval_ms": 500,      # poll interval
    "smoothing": 0.5,        # 0 = raw, 0.95 = very smooth
    "double_click_cmd": "",  # app to spawn on double-click
    "smart_color": True,     # brightness follows bar fill level
    "smart_strength": 0.7,   # 0 = off, 1 = max effect
}

ICON_SIZES = (16, 22, 24, 32, 48, 64)


# ---------------------------------------------------------------- config

def migrate_device(name, d):
    """Upgrade older per-device schemas."""
    if "max_down_bps" not in d:
        old = d.pop("max_mbps", None)
        bps = old * 1024 ** 2 if old else DEFAULT_MAX_BPS
        d["max_down_bps"] = bps
        d["max_up_bps"] = bps
    # reset values still at the old 10 MB/s default to the new default
    for key in ("max_down_bps", "max_up_bps"):
        if d[key] == OLD_DEFAULT_BPS:
            d[key] = DEFAULT_MAX_BPS
    if "unit" not in d:
        d["unit"] = d.pop("down_unit", DEFAULT_UNIT)
    d.pop("down_unit", None)
    d.pop("up_unit", None)
    if d["unit"] == "MB/s" and d["max_down_bps"] == DEFAULT_MAX_BPS \
            and d["max_up_bps"] == DEFAULT_MAX_BPS:
        d["unit"] = DEFAULT_UNIT
    d.setdefault("alias", name)
    d.setdefault("enabled", name != "lo")
    return d


def new_device(name):
    return {"enabled": name != "lo", "alias": name, "unit": DEFAULT_UNIT,
            "max_down_bps": DEFAULT_MAX_BPS, "max_up_bps": DEFAULT_MAX_BPS}


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    cfg.pop("sensitivity", None)
    for name, d in cfg["devices"].items():
        migrate_device(name, d)
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- stats

def list_devices():
    try:
        return sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []


def read_counters():
    """Return {dev: (rx_bytes, tx_bytes)} from /proc/net/dev."""
    out = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                fields = rest.split()
                if len(fields) >= 16:
                    out[name.strip()] = (int(fields[0]), int(fields[8]))
    except OSError:
        pass
    return out


def fmt_rate(bps):
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


# ---------------------------------------------------------------- icon

GREEN_LIT = QColor(40, 230, 90)
GREEN_DIM = QColor(20, 70, 35)
RED_LIT = QColor(240, 60, 50)
RED_DIM = QColor(80, 25, 22)


def smart_color(base, pct, strength):
    """Scale lit color with fill level: dim when low, vivid when full,
    blending toward white glow near the top."""
    if strength <= 0:
        return base
    b = (1 - strength) + strength * (0.35 + 0.65 * pct)
    c = QColor(min(255, int(base.red() * b)),
               min(255, int(base.green() * b)),
               min(255, int(base.blue() * b)))
    if pct > 0.8:
        w = (pct - 0.8) / 0.2 * 0.4 * strength   # 0..0.4 white blend
        c = QColor(int(c.red() + (255 - c.red()) * w),
                   int(c.green() + (255 - c.green()) * w),
                   int(c.blue() + (255 - c.blue()) * w))
    return c


def render_pixmap(size, down_pct, up_pct, segments, strength=0.0):
    """Crisp integer-aligned bars rendered natively at `size` px."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)

    bar_gap = 2 if size < 32 else 3          # gap between the two bars
    seg_gap = 1 if size < 32 else 2          # gap between ridges
    bar_w = (size - bar_gap) // 2
    seg_h = max(1, (size - (segments - 1) * seg_gap) // segments)
    n_fit = segments

    def lit_count(pct):
        if pct <= 0.004:
            return 0
        return max(1, min(n_fit, round(pct * n_fit)))

    def draw_bar(x, pct, base_lit, dim):
        n = lit_count(pct)
        lit = smart_color(base_lit, pct, strength)
        y = size
        for i in range(n_fit):
            y -= seg_h
            if y < 0:
                break
            p.fillRect(x, y, bar_w, seg_h, lit if i < n else dim)
            y -= seg_gap

    draw_bar(0, down_pct, GREEN_LIT, GREEN_DIM)
    draw_bar(bar_w + bar_gap, up_pct, RED_LIT, RED_DIM)
    p.end()
    return pm


def render_icon(down_pct, up_pct, segments, strength=0.0):
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(
            render_pixmap(size, down_pct, up_pct, segments, strength))
    return icon


# ---------------------------------------------------------------- config window

class ConfigWindow(QWidget):
    """Edits a working copy; Apply/OK commit it to the live config."""

    def __init__(self, live_cfg, on_apply):
        super().__init__()
        self.live = live_cfg
        self.on_apply = on_apply
        self.work = copy.deepcopy(live_cfg)
        self.setWindowTitle("netmeter — configure")
        self.resize(540, 480)

        self._ensure_devices()

        # live per-device usage preview — runs only while window is visible
        self.dev_rate_labels = {}
        self.sens_rate_labels = {}
        self.prev_counters = None
        self.prev_t = 0.0
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._update_preview)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._devices_tab(), "Devices")
        self.tabs.addTab(self._sensitivity_tab(), "Sensitivity")
        self.tabs.addTab(self._general_tab(), "General")
        self.tabs.currentChanged.connect(self._tab_changed)

        btns = QHBoxLayout()
        btns.addStretch(1)
        for text, slot in (("OK", self._ok),
                           ("Apply", self._apply),
                           ("Cancel", self.close)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        lay.addLayout(btns)

    # ----- live usage preview (CPU spent only while visible)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.prev_counters = read_counters()
        self.prev_t = time.monotonic()
        self.preview_timer.start(1000)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self.preview_timer.stop()

    def _update_preview(self):
        if not self.isVisible():
            self.preview_timer.stop()
            return
        now = time.monotonic()
        dt = max(now - self.prev_t, 1e-6)
        cur = read_counters()
        for name in set(self.dev_rate_labels) | set(self.sens_rate_labels):
            if name not in cur or name not in (self.prev_counters or {}):
                continue
            rx = max(0, cur[name][0] - self.prev_counters[name][0]) / dt
            tx = max(0, cur[name][1] - self.prev_counters[name][1]) / dt
            text = f"↓ {fmt_rate(rx)}  ↑ {fmt_rate(tx)}"
            if name in self.dev_rate_labels:
                self.dev_rate_labels[name].setText(text)
            if name in self.sens_rate_labels:
                self.sens_rate_labels[name].setText(text)
        self.prev_counters = cur
        self.prev_t = now

    # ----- commit

    def _apply(self):
        self.live.clear()
        self.live.update(copy.deepcopy(self.work))
        self.on_apply()

    def _ok(self):
        self._apply()
        self.close()

    # ----- helpers

    def _ensure_devices(self):
        devs = self.work["devices"]
        for name in list_devices():
            if name not in devs:
                devs[name] = new_device(name)

    def _tab_changed(self, idx):
        # entering Sensitivity: rebuild so enabled-set and aliases are fresh
        if self.tabs.tabText(idx) == "Sensitivity":
            self._rebuild_sens_list()

    # ----- Devices tab: enable + rename per interface

    def _devices_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)

        self.dev_area = QScrollArea()
        self.dev_area.setWidgetResizable(True)
        outer.addWidget(self.dev_area)

        refresh = QPushButton("Refresh device list")
        refresh.clicked.connect(self._refresh_devices)
        outer.addWidget(refresh)

        self._rebuild_device_list()
        return page

    def _refresh_devices(self):
        self._ensure_devices()
        self._rebuild_device_list()

    def _rebuild_device_list(self):
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setColumnStretch(2, 1)
        self.dev_rate_labels = {}

        for row, (name, d) in enumerate(sorted(self.work["devices"].items())):
            cb = QCheckBox(name)
            cb.setChecked(d["enabled"])
            cb.toggled.connect(
                lambda on, n=name: self._set_dev(n, "enabled", on))
            grid.addWidget(cb, row, 0)

            alias = QLineEdit(d["alias"])
            alias.setPlaceholderText(name)
            alias.textChanged.connect(
                lambda t, n=name: self._set_dev(n, "alias", t.strip() or n))
            grid.addWidget(alias, row, 2)

            rate = self._rate_label()
            grid.addWidget(rate, row, 3)
            self.dev_rate_labels[name] = rate

        grid.setRowStretch(grid.rowCount(), 1)
        self.dev_area.setWidget(inner)

    def _set_dev(self, name, key, value):
        self.work["devices"][name][key] = value

    # ----- Sensitivity tab: per-device max down/up, shared unit, one row

    def _sensitivity_tab(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        hint = QLabel("Max speed per device: the speed at which that device "
                      "alone fills the bar. Lower max = more sensitive.")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.sens_area = QScrollArea()
        self.sens_area.setWidgetResizable(True)
        outer.addWidget(self.sens_area)

        self._rebuild_sens_list()
        return page

    def _rebuild_sens_list(self):
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setColumnStretch(0, 1)

        for col, text in enumerate(
                ("Device", "↓ Download", "↑ Upload", "Unit", "Now")):
            lab = QLabel(f"<b>{text}</b>")
            grid.addWidget(lab, 0, col)

        self.sens_rate_labels = {}
        row = 1
        for name, d in sorted(self.work["devices"].items()):
            if not d["enabled"]:
                continue
            grid.addWidget(QLabel(d["alias"]), row, 0)

            mult = UNIT_MULT.get(d["unit"], 1024)

            down = self._num_edit(d["max_down_bps"] / mult)
            grid.addWidget(down, row, 1)
            up = self._num_edit(d["max_up_bps"] / mult)
            grid.addWidget(up, row, 2)

            unit = QComboBox()
            unit.addItems([u for u, _ in UNITS])
            unit.setCurrentText(d["unit"])
            grid.addWidget(unit, row, 3)

            rate = self._rate_label()
            grid.addWidget(rate, row, 4)
            self.sens_rate_labels[name] = rate

            def store(n=name, de=down, ue=up, uc=unit):
                m = UNIT_MULT[uc.currentText()]
                dd = self.work["devices"][n]
                dd["unit"] = uc.currentText()
                dd["max_down_bps"] = self._parse(de, dd["max_down_bps"] / m) * m
                dd["max_up_bps"] = self._parse(ue, dd["max_up_bps"] / m) * m

            down.textChanged.connect(lambda _t, s=store: s())
            up.textChanged.connect(lambda _t, s=store: s())
            unit.currentTextChanged.connect(lambda _t, s=store: s())
            down.returnPressed.connect(self._apply)   # Enter = auto-apply
            up.returnPressed.connect(self._apply)
            row += 1

        if row == 1:
            grid.addWidget(
                QLabel("No devices enabled — check some in Devices tab."),
                1, 0, 1, 5)
        grid.setRowStretch(grid.rowCount(), 1)
        self.sens_area.setWidget(inner)

    @staticmethod
    def _rate_label():
        """Fixed-width live-rate label so updates never shift columns."""
        lab = QLabel("↓ –  ↑ –")
        fm = lab.fontMetrics()
        lab.setFixedWidth(fm.horizontalAdvance("↓ 1023.9 MB/s  ↑ 1023.9 MB/s")
                          + 8)
        return lab

    @staticmethod
    def _num_edit(value):
        e = QLineEdit(f"{value:g}")
        v = QDoubleValidator(0.0, 1e12, 6)
        v.setNotation(QDoubleValidator.StandardNotation)
        e.setValidator(v)
        e.setAlignment(Qt.AlignRight)
        return e

    @staticmethod
    def _parse(edit, fallback):
        try:
            val = float(edit.text().replace(",", "."))
            return val if val > 0 else fallback
        except ValueError:
            return fallback

    # ----- General tab

    def _general_tab(self):
        page = QWidget()
        form = QFormLayout(page)

        seg = QSpinBox()
        seg.setRange(2, 16)
        seg.setValue(self.work["segments"])
        seg.setToolTip("Scan-line ridges per bar. Fewer = crisper at "
                       "small tray sizes.")
        seg.valueChanged.connect(
            lambda v: self.work.__setitem__("segments", v))
        form.addRow("Bar segments", seg)

        iv = QSpinBox()
        iv.setRange(100, 5000)
        iv.setSingleStep(100)
        iv.setSuffix(" ms")
        iv.setValue(self.work["interval_ms"])
        iv.valueChanged.connect(
            lambda v: self.work.__setitem__("interval_ms", v))
        form.addRow("Update interval", iv)

        sm = QSlider(Qt.Horizontal)
        sm.setRange(0, 95)
        sm.setValue(round(self.work["smoothing"] * 100))
        sm.setToolTip("0 = instant/jumpy, high = smooth/laggy")
        sm.valueChanged.connect(
            lambda v: self.work.__setitem__("smoothing", v / 100))
        form.addRow("Smoothing", sm)

        sc = QCheckBox("brightness follows bar level")
        sc.setChecked(self.work.get("smart_color", True))
        form.addRow("Smart color", sc)

        ss = QSlider(Qt.Horizontal)
        ss.setRange(0, 100)
        ss.setValue(round(self.work.get("smart_strength", 0.7) * 100))
        ss.setToolTip("Effect strength: 0 = constant color, "
                      "100 = strong dim-to-glow ramp")
        ss.setEnabled(sc.isChecked())
        ss.valueChanged.connect(
            lambda v: self.work.__setitem__("smart_strength", v / 100))
        form.addRow("Smart strength", ss)

        def sc_toggled(on):
            self.work["smart_color"] = on
            ss.setEnabled(on)
        sc.toggled.connect(sc_toggled)

        dc = QLineEdit(self.work.get("double_click_cmd", ""))
        dc.setPlaceholderText("e.g.  konsole -e nethogs")
        dc.setToolTip("Command spawned when the tray icon is double-clicked. "
                      "Empty = do nothing.")
        dc.textChanged.connect(
            lambda t: self.work.__setitem__("double_click_cmd", t))
        form.addRow("Double-click app", dc)

        return page


# ---------------------------------------------------------------- tray app

class NetMeter:
    def __init__(self, app):
        self.app = app
        self.cfg = load_config()
        self.prev = read_counters()
        self.prev_t = time.monotonic()
        self.disp_down = 0.0   # smoothed 0..1
        self.disp_up = 0.0
        self.config_win = None

        self.tray = QSystemTrayIcon(render_icon(0, 0, self.cfg["segments"]))
        menu = QMenu()
        act_cfg = QAction("Configure…", menu)
        act_cfg.triggered.connect(self.open_config)
        menu.addAction(act_cfg)
        menu.addSeparator()
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(app.quit)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_activated)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(self.cfg["interval_ms"])

    def open_config(self):
        # fresh window each time -> working copy starts from live config
        if self.config_win is not None:
            self.config_win.close()
            self.config_win.deleteLater()
        self.config_win = ConfigWindow(self.cfg, self.config_applied)
        self.config_win.show()
        self.config_win.raise_()
        self.config_win.activateWindow()

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            cmd = self.cfg.get("double_click_cmd", "").strip()
            if cmd:
                try:
                    subprocess.Popen(shlex.split(cmd),
                                     start_new_session=True)
                except (OSError, ValueError) as e:
                    self.tray.showMessage("netmeter",
                                          f"Launch failed: {e}")

    def config_applied(self):
        self.timer.setInterval(self.cfg["interval_ms"])
        save_config(self.cfg)

    def tick(self):
        now = time.monotonic()
        dt = max(now - self.prev_t, 1e-6)
        cur = read_counters()

        down_pct = up_pct = 0.0
        tot_rx = tot_tx = 0.0
        for name, dcfg in self.cfg["devices"].items():
            if not dcfg.get("enabled"):
                continue
            if name not in cur or name not in self.prev:
                continue
            rx = max(0, cur[name][0] - self.prev[name][0]) / dt
            tx = max(0, cur[name][1] - self.prev[name][1]) / dt
            tot_rx += rx
            tot_tx += tx
            if dcfg["max_down_bps"] > 0:
                down_pct += rx / dcfg["max_down_bps"]
            if dcfg["max_up_bps"] > 0:
                up_pct += tx / dcfg["max_up_bps"]

        down_pct = min(1.0, down_pct)
        up_pct = min(1.0, up_pct)

        a = 1.0 - self.cfg["smoothing"]
        self.disp_down += (down_pct - self.disp_down) * a
        self.disp_up += (up_pct - self.disp_up) * a

        self.prev = cur
        self.prev_t = now

        strength = (self.cfg.get("smart_strength", 0.7)
                    if self.cfg.get("smart_color", True) else 0.0)
        self.tray.setIcon(
            render_icon(self.disp_down, self.disp_up,
                        self.cfg["segments"], strength))
        self.tray.setToolTip(
            f"↓ {fmt_rate(tot_rx)}   ↑ {fmt_rate(tot_tx)}")


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("netmeter")
    meter = NetMeter(app)  # noqa: F841 — keep alive
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
