# Gara + Roar HUD Alarm 🔔

A read-only Windows companion for **Warframe**. It watches the visible top-right HUD, OCRs the real **Splinter Storm** timer, and detects whether the **Roar** buff icon is present.

It does **not** press keys, recast abilities, read game memory, inject code, or inspect network traffic.

## Alert behavior

- **Splinter Storm:** warns at **10 seconds** and again urgently at **5 seconds** remaining.
- **Roar:** gives **no warning while Roar is active**. Once the icon has been continuously absent for 1.8 seconds, it sounds the Roar alarm twice. It then reminds you every **8 seconds** until the icon returns.

Roar tracking arms only after the script has seen Roar active at least once. This prevents it from yelling in the Orbiter or before your first cast.

## Install

1. Set Warframe to **Borderless Fullscreen** or **Windowed**. Exclusive fullscreen can produce black screenshots on some systems.
2. Install **Python 3.10+**. Keep the option that installs the `py` launcher enabled.
3. Install **Tesseract OCR 5**. Its usual Windows path is:
   `C:\Program Files\Tesseract-OCR\tesseract.exe`
4. Double-click **`setup_windows.bat`**.
5. Double-click **`calibrate.bat`**.
6. After calibration, double-click **`start_alarm.bat`** whenever you play Gara.

Official Tesseract installation notes:
https://tesseract-ocr.github.io/tessdoc/Installation.html

## Calibration

Calibration uses a frozen screenshot from *your* HUD, so custom HUD colors and scale are fine.

1. Enter a mission as Gara with Roar equipped.
2. Start `calibrate.bat` and press Enter when prompted.
3. Switch back to Warframe during the countdown and activate **both Splinter Storm and Roar**.
4. At the capture beep, Alt-Tab to the selector if it does not appear in front.
5. For **Splinter Storm**, select:
   - the whole buff tile,
   - the icon only, cropped tightly,
   - the timer digits only.
6. Enter the Splinter Storm timer shown in the enlarged preview.
7. For **Roar**, select only:
   - the whole buff tile,
   - the icon, cropped tightly.

Roar does not need timer calibration or OCR. OpenCV's selector accepts **Enter** or **Space** after dragging the box.

## Existing calibration

An older `config.json` still works. The updated script forces Roar into expiry-only mode and ignores its old warning thresholds and timer coordinates, so you do **not** need to recalibrate merely to get the new behavior.

## Adjust the behavior

After calibration, open `config.json`.

### Splinter Storm warning times

```json
"warnings": [10, 5]
```

The first value is the early warning; the smaller value is urgent and repeats the sound.

### Roar expiry confirmation delay

```json
"missing_grace_seconds": 1.8
```

Raise this if brief icon-detection misses cause false alarms. Lowering it makes the expiry alert faster but more sensitive.

### Roar repeated reminder interval

```json
"inactive_reminder_seconds": 8.0
```

Set it to `0` for one alert at expiry with no repeated reminders.

Restart the alarm after editing.

## Troubleshooting

### Splinter Storm's timer shows `?`

Re-run calibration and select only the visible digits—not the icon, percentage, label, or surrounding space. A little padding is okay, but a huge box makes OCR worse.

### Roar produces a false expiry alarm

Raise Roar's `missing_grace_seconds` from `1.8` to `2.5` or slightly lower its `match_threshold` if the icon is genuinely visible but not detected.

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

Run `start_alarm_debug.bat`. It writes `debug\latest.png` plus the Splinter Storm timer crop so you can inspect what the script detected. Debug images may contain whatever was visible in the captured top-right HUD region.

## Important Warframe policy note

Digital Extremes does not maintain a universal allow-list for third-party software and says external software is used at the player's own risk. This tool stays on the conservative side: screen pixels in, sounds out, with no gameplay input automation.
