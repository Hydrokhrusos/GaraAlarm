# Gara + Roar HUD Alarm 🔔

A read-only Windows companion for **Warframe**. It watches the full visible top buff bar for the real **Splinter Storm** timer, and watches the bottom-right **Roar** ready/off ability icon.

It does **not** press keys, recast abilities, read game memory, inject code, or inspect network traffic.

## Alert behavior

- **Splinter Storm:** warns once at **10 seconds** with the regular sound and once urgently at **5 seconds** with a separate urgent sound.
- **Roar:** gives **no warning while Roar is active**. It calibrates from the stable bottom-right ready/off icon, arms after that icon disappears for 1.8 seconds, then sounds the Roar alarm once when the ready/off icon returns. If you calibrate Roar's grayed/no-energy icon too, it skips that state and reminds you every **8 seconds** while Roar stays ready, up to **5** alerts per ready cycle.

Roar tracking arms only after the script has first confirmed the ready/off icon, then seen it disappear. This prevents it from yelling in the Orbiter or before your first cast.

## Install

1. Set Warframe to **Borderless Fullscreen** or **Windowed**. Exclusive fullscreen can produce black screenshots on some systems.
2. Install **Python 3.10+**. Keep the option that installs the `py` launcher enabled.
3. Install **Tesseract OCR 5**. Its usual Windows path is:
   `C:\Program Files\Tesseract-OCR\tesseract.exe`
4. Double-click **`setup_windows.bat`**.
5. Double-click **`calibrate.bat`** and choose what to calibrate.
6. After calibration, double-click **`start_alarm.bat`** whenever you play Gara.

Official Tesseract installation notes:
https://tesseract-ocr.github.io/tessdoc/Installation.html

## Calibration

Calibration uses a frozen screenshot from *your* HUD, so custom HUD colors and scale are fine.

1. Enter a mission as Gara with Roar equipped.
2. Start `calibrate.bat` and choose **all**, **Splinter Storm only**, or **Roar ready/no-energy icons only**.
3. Switch back to Warframe during the countdown, activate **Splinter Storm**, and leave **Roar off/ready**.
4. At the capture beep, Alt-Tab to the selector if it does not appear in front.
5. For **Splinter Storm**, select it from the top buff bar:
   - the whole buff tile,
   - the icon only, cropped tightly,
   - the timer digits only.
6. Enter the Splinter Storm timer shown in the enlarged preview.
7. For **Roar**, select the castable off/ready icon from the bottom-right ability indicators:
   - the whole indicator,
   - the icon, cropped tightly.
8. When prompted, you can also capture Roar's grayed/no-energy state. Switch back to Warframe, make the Roar icon gray because you lack enough energy, then select:
   - the whole no-energy indicator,
   - the grayed icon, cropped tightly.

Roar does not need timer calibration or OCR. OpenCV's selector accepts **Enter** or **Space** after dragging the box.

You can refresh just one target later. Recalibrating Roar only leaves Splinter Storm's template and timer crop alone; recalibrating Splinter Storm only leaves Roar alone.

## Existing calibration

An older `config.json` still loads, but to use the bottom-right Roar ready/off icon you should rerun `calibrate.bat` with Roar uncast. Old Roar templates cropped from the top buff bar or from an active Roar state usually will not match the ready/off indicator.

## Adjust the behavior

After calibration, open `config.json`.

### Splinter Storm warning times

```json
"warnings": [10, 5]
```

The first value is the early warning; the smaller value is urgent. Splinter Storm uses `sound` for the early warning and `urgent_sound` for the urgent warning, falling back to `sound` if `urgent_sound` is omitted.

### Roar cast confirmation delay

```json
"missing_grace_seconds": 1.8
```

Raise this if brief ready-icon detection misses cause false alarms. Lowering it makes the script arm faster after you cast Roar, but also makes it more sensitive to single detection misses.

### Roar repeated reminder interval

```json
"inactive_reminder_seconds": 8.0
```

Set it to `0` for one alert when Roar becomes ready with no repeated reminders.

```json
"max_inactive_alerts": 5
```

Set it to `0` for unlimited reminders. Roar skips alarms while its calibrated no-energy icon matches. If that optional template has not been captured, it falls back to the dimmed-icon brightness check.

Restart the alarm after editing.

## Troubleshooting

### Splinter Storm's timer shows `?`

Re-run calibration and select only the visible digits—not the icon, percentage, label, or surrounding space. A little padding is okay, but a huge box makes OCR worse.

### Splinter Storm shows an impossible high timer

The parser treats three bare digits like `100` as a missing decimal, so it reads that as `10.0`. Splinter Storm is also capped by `max_reasonable_seconds`; raise that only if your real timer can exceed `90` seconds.

### Roar produces a false ready alarm

Raise Roar's `missing_grace_seconds` from `1.8` to `2.5` if brief misses arm the tracker while Roar is already ready. Slightly lower `match_threshold` if the ready/off icon is genuinely visible but not detected.

Roar uses a shape-aware match mode so the ready/off icon can still match if it is blue-highlighted as the last ability you cast.

### Detection score is below about `0.68`

Recalibrate and crop the icon tightly. Do not include the changing timer in the icon template.

### It finds the wrong icon

Raise that buff's `match_threshold` in `config.json` from `0.68` to `0.75`, then restart. If it stops detecting entirely, lower it slightly.

### The screenshot is black

Use Borderless Fullscreen or Windowed mode, then recalibrate.

### Resolution, HUD scale, or HUD color changed

Recalibrate. The icon templates and Splinter Storm timer offset are pixel-based by design.

### The alarm says it is paused

By default it only scans while a window containing `Warframe` in its title is foreground. To disable that guard, run:

```bat
.venv\Scripts\python.exe gara_roar_alarm.py --ignore-focus
```

### I need diagnostics

Run `start_alarm_debug.bat`. It writes `debug\latest.png` plus the Splinter Storm timer crop so you can inspect what the script detected. Debug images may contain the visible Warframe HUD regions being scanned.

## Important Warframe policy note

Digital Extremes does not maintain a universal allow-list for third-party software and says external software is used at the player's own risk. This tool stays on the conservative side: screen pixels in, sounds out, with no gameplay input automation.
