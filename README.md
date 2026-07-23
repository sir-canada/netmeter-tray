# netmeter-tray

Tiny scan-lined network speed meter for the system tray (Linux, Qt/PySide6).

Two vertical segmented bars, sized like a normal tray icon:
**green = download, red = upload**. Each enabled network device contributes
`current rate / its max speed` as a percentage; the percentages of all
devices are summed and drawn as the bar level.

## Features

- **Scan-lined bars** — segmented ridges (count configurable), rendered
  natively at every tray icon size for crisp pixels.
- **Per-device control** — pick which interfaces to monitor (checkboxes),
  rename them, and set independent max download / max upload per device
  with your choice of unit (B/s, KB/s, MB/s, GB/s). Low max = very
  sensitive (see even 10 KB/s); high max = device stays quiet.
- **Smart color** — lit segments dim at low traffic and ramp to a vivid,
  white-hot glow as the bar fills. Toggle + strength slider.
- **Live preview** — the config window shows real-time per-device ↓/↑
  rates so you know what to enable and how to tune it. The preview timer
  only runs while the window is visible — zero cost otherwise.
- **Double-click launcher** — spawn any command (e.g. `konsole -e nethogs`)
  by double-clicking the tray icon.
- **Apply / OK / Cancel** — preview changes live before committing.
- Tooltip with current totals. Config stored in
  `~/.config/netmeter/config.json`.

## Install

```sh
pip install .          # installs the `netmeter` command
# or just run it directly:
python3 netmeter.py
```

Requires Python ≥ 3.9 and PySide6. Reads `/proc/net/dev` — Linux only.

## Autostart (KDE/GNOME)

```sh
cp netmeter.desktop ~/.config/autostart/
```

## Configure

Right-click the tray icon → **Configure…**

| Tab | Controls |
|---|---|
| Devices | enable/disable per interface, rename, live ↓/↑ preview |
| Sensitivity | per-device max download / max upload + unit, live preview |
| General | bar segments, update interval, smoothing, smart color, double-click app |

## License

MIT
