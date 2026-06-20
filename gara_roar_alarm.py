#!/usr/bin/env python3
"""Gara + Roar HUD timer alarm for Warframe.

This program is deliberately read-only: it captures screen pixels, recognizes two
user-calibrated buff icons, OCRs Splinter Storm's visible timer, and watches for
Roar's ready icon to return. It never sends keyboard/mouse/controller input and does
not inspect the game process.

Windows is the supported platform because alerts use the built-in ``winsound``
module. Screen capture/template matching/OCR are otherwise portable.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import cv2
    import mss
    import numpy as np
    import pytesseract
except ModuleNotFoundError as exc:  # Friendly message before any stack trace.
    missing = exc.name or "a required package"
    raise SystemExit(
        f"Missing Python dependency: {missing}\n"
        "Run setup_windows.bat first, or install requirements.txt manually."
    ) from exc

if os.name == "nt":
    import winsound
else:  # pragma: no cover - Windows is the supported target.
    winsound = None  # type: ignore[assignment]


APP_NAME = "Gara + Roar Alarm"
CONFIG_VERSION = 1
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATE_DIR = BASE_DIR / "templates"
DEBUG_DIR = BASE_DIR / "debug"

DEFAULT_BUFFS: dict[str, dict[str, Any]] = {
    "splinter_storm": {
        "name": "Splinter Storm",
        "template": "templates/splinter_storm.png",
        "sound": "sounds/splinter_storm.wav",
        "urgent_sound": "sounds/splinter_storm_urgent.wav",
        "alert_mode": "thresholds",
        "warnings": [10, 5],
        "match_threshold": 0.68,
        "max_reasonable_seconds": 90,
        "missing_grace_seconds": 2.2,
    },
    "roar": {
        "name": "Roar",
        "template": "templates/roar.png",
        "disabled_template": "templates/roar_disabled.png",
        "sound": "sounds/roar.wav",
        "alert_mode": "on_ready",
        "warnings": [],
        "match_threshold": 0.68,
        "disabled_match_threshold": 0.68,
        "disabled_match_margin": 0.02,
        "max_reasonable_seconds": 180,
        "missing_grace_seconds": 1.8,
        "match_mode": "gray_or_edges",
        "presence_confirm_hits": 2,
        "expiry_repeat": 1,
        "inactive_reminder_seconds": 8.0,
        "inactive_reminder_repeat": 1,
        "max_inactive_alerts": 5,
        "suppress_dim_ready_icon": True,
        "dim_ready_brightness_ratio": 0.72,
        "dim_ready_contrast_ratio": 0.75,
    },
}

OCR_MODES = (
    "otsu_inv:8",
    "otsu_inv:7",
    "clahe:8",
    "clahe:7",
    "otsu:8",
    "gray:8",
    "adaptive_inv:8",
)

MISSING_DECIMAL_MAX_SECONDS = 90.0

CALIBRATION_TARGET_ALIASES = {
    "": "all",
    "1": "all",
    "all": "all",
    "both": "all",
    "everything": "all",
    "2": "splinter_storm",
    "splinter": "splinter_storm",
    "splinterstorm": "splinter_storm",
    "splinter_storm": "splinter_storm",
    "gara": "splinter_storm",
    "3": "roar",
    "roar": "roar",
}


class ConfigurationError(RuntimeError):
    """Raised when config.json is missing or malformed."""


@dataclass
class Detection:
    x: int
    y: int
    width: int
    height: int
    score: float


@dataclass
class OCRResult:
    seconds: float
    text: str
    mode: str


@dataclass
class TrackerState:
    key: str
    name: str
    warnings: list[float]
    max_reasonable_seconds: float
    alert_mode: str = "thresholds"
    missing_grace_seconds: float = 2.2
    inactive_reminder_seconds: float = 0.0
    presence_confirm_hits: int = 2
    last_timer: Optional[float] = None
    last_ocr_at: float = 0.0
    last_seen_at: float = 0.0
    missing_since: Optional[float] = None
    active: bool = False
    ever_active: bool = False
    fired: set[float] = field(default_factory=set)
    last_detection: Optional[Detection] = None
    last_ocr_text: str = ""
    last_ocr_mode: str = ""
    pending_refresh_value: Optional[float] = None
    pending_refresh_at: float = 0.0
    last_inactive_alert_at: float = 0.0
    presence_seen_streak: int = 0
    ever_ready: bool = False
    ready_actionable: bool = True
    pending_actionable_alert: bool = False
    inactive_alert_count: int = 0
    max_inactive_alerts: int = 0

    def inactive_alert_limit_reached(self) -> bool:
        return (
            self.max_inactive_alerts > 0
            and self.inactive_alert_count >= self.max_inactive_alerts
        )

    def record_inactive_alert(self) -> None:
        self.inactive_alert_count += 1
        self.pending_actionable_alert = False

    def estimate(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_timer is None:
            return None
        if now is None:
            now = time.monotonic()
        return max(0.0, self.last_timer - max(0.0, now - self.last_ocr_at))

    def mark_seen(
        self, detection: Detection, now: float, actionable: bool = True
    ) -> Optional[str]:
        self.last_seen_at = now
        self.last_detection = detection
        self.missing_since = None

        # Expiry-style icon tracking: presence means the buff is active, and
        # absence after a grace period means it ended.
        if self.alert_mode == "on_expire":
            self.presence_seen_streak += 1
            if self.presence_seen_streak < max(1, self.presence_confirm_hits):
                return None
            was_active = self.active
            self.active = True
            self.ever_active = True
            self.last_timer = None
            if not was_active:
                self.last_inactive_alert_at = 0.0
                self.inactive_alert_count = 0
            return None

        # For bottom-right Roar tracking, the calibrated template is the stable
        # ready/off icon. It should alert when that icon returns after it has
        # been absent long enough to mean Roar was cast and active.
        if self.alert_mode == "on_ready":
            self.presence_seen_streak += 1
            if self.presence_seen_streak < max(1, self.presence_confirm_hits):
                return None
            was_active = self.active
            self.ever_ready = True
            self.ready_actionable = actionable
            self.active = False
            self.last_timer = None
            self.pending_refresh_value = None
            self.pending_refresh_at = 0.0
            if was_active and self.ever_active:
                if not actionable:
                    self.last_inactive_alert_at = 0.0
                    self.pending_actionable_alert = True
                    return None
                if self.inactive_alert_limit_reached():
                    self.pending_actionable_alert = False
                    return None
                self.last_inactive_alert_at = now
                self.pending_actionable_alert = False
                return "expired"
            if self.pending_actionable_alert:
                if not actionable:
                    return None
                if self.inactive_alert_limit_reached():
                    self.pending_actionable_alert = False
                    return None
                self.last_inactive_alert_at = now
                self.pending_actionable_alert = False
                return "expired"
            if self.ever_active and self.inactive_reminder_seconds > 0.0:
                if not actionable:
                    return None
                if self.last_inactive_alert_at <= 0.0:
                    self.last_inactive_alert_at = now
                elif now - self.last_inactive_alert_at >= self.inactive_reminder_seconds:
                    if self.inactive_alert_limit_reached():
                        return None
                    self.last_inactive_alert_at = now
                    return "reminder"
            return None

        return None

    def mark_missing(self, now: float) -> Optional[str]:
        """Return ``expired`` or ``reminder`` for icon-only buff modes.

        A short grace period filters single-frame template misses. For Roar's
        ready-icon mode, absence only arms the tracker after the ready/off icon
        has first been confirmed visible.
        """
        if self.missing_since is None:
            self.missing_since = now
        self.presence_seen_streak = 0
        if now - self.missing_since < self.missing_grace_seconds:
            return None

        self.last_detection = None
        if self.alert_mode == "on_ready":
            if not self.ever_ready:
                return None
            if not self.active:
                self.active = True
                self.ever_active = True
                self.ready_actionable = True
                self.pending_actionable_alert = False
                self.inactive_alert_count = 0
                self.last_timer = None
                self.pending_refresh_value = None
                self.pending_refresh_at = 0.0
                self.last_inactive_alert_at = 0.0
            return None

        if self.active:
            self.active = False
            self.last_timer = None
            self.pending_refresh_value = None
            self.pending_refresh_at = 0.0
            if self.alert_mode == "on_expire" and self.ever_active:
                if self.inactive_alert_limit_reached():
                    return None
                self.last_inactive_alert_at = now
                return "expired"
            return None

        if (
            self.alert_mode == "on_expire"
            and self.ever_active
            and self.inactive_reminder_seconds > 0.0
        ):
            if self.last_inactive_alert_at <= 0.0:
                self.last_inactive_alert_at = now
            elif now - self.last_inactive_alert_at >= self.inactive_reminder_seconds:
                if self.inactive_alert_limit_reached():
                    return None
                self.last_inactive_alert_at = now
                return "reminder"
        return None

    def accept_timer(self, result: OCRResult, now: float) -> tuple[Optional[float], bool]:
        """Accept an OCR value and return (crossed_warning, urgent).

        Timer increases are treated as refreshes and re-arm every warning. Large,
        implausible downward jumps are rejected as OCR glitches.
        """
        value = result.seconds
        if not (0.0 <= value <= self.max_reasonable_seconds):
            return None, False

        previous_timer = self.last_timer
        previous_estimate = self.estimate(now)
        was_active = self.active

        # Reject a very large downward jump while the icon has remained visible.
        # A genuine timer should roughly follow real time; refreshes go upward.
        if (
            was_active
            and previous_estimate is not None
            and value < previous_estimate - 14.0
            and previous_estimate > 16.0
        ):
            return None, False

        high_jump = (
            was_active
            and previous_estimate is not None
            and value > previous_estimate + 4.0
        )
        refreshed = False
        if high_jump:
            # A refresh is an upward timer jump, but OCR can occasionally turn
            # 12 into 112. Require two consecutive high readings that count down
            # consistently before accepting the jump.
            if self.pending_refresh_value is None:
                self.pending_refresh_value = value
                self.pending_refresh_at = now
                return None, False
            pending_expected = max(
                0.0, self.pending_refresh_value - (now - self.pending_refresh_at)
            )
            if (
                now - self.pending_refresh_at <= 3.0
                and abs(value - pending_expected) <= 5.0
            ):
                refreshed = True
                self.pending_refresh_value = None
                self.pending_refresh_at = 0.0
            else:
                self.pending_refresh_value = value
                self.pending_refresh_at = now
                return None, False
        else:
            self.pending_refresh_value = None
            self.pending_refresh_at = 0.0

        if refreshed or not was_active:
            self.fired.clear()

        self.active = True
        self.ever_active = True
        self.last_timer = value
        self.last_ocr_at = now
        self.last_ocr_text = result.text
        self.last_ocr_mode = result.mode

        if self.alert_mode != "thresholds":
            return None, False

        warnings_desc = sorted(set(self.warnings), reverse=True)
        if not warnings_desc:
            return None, False

        crossed: list[float] = []
        if not was_active:
            # Starting the script mid-buff should still warn, but only with the
            # most urgent applicable threshold rather than playing every sound.
            crossed = [threshold for threshold in warnings_desc if value <= threshold]
        elif not refreshed and previous_estimate is not None:
            # Compare with the last OCR reading as well as its time-adjusted
            # estimate. Otherwise a threshold crossed between OCR samples could
            # be missed because the estimate is already below it.
            upper_bound = max(previous_estimate, previous_timer or previous_estimate)
            crossed = [
                threshold
                for threshold in warnings_desc
                if threshold not in self.fired
                and upper_bound > threshold >= value
            ]

        if not crossed:
            return None, False

        chosen = min(crossed)  # Most urgent threshold reached.
        for threshold in warnings_desc:
            if value <= threshold:
                self.fired.add(threshold)
        urgent = chosen == min(warnings_desc)
        return chosen, urgent


class SoundWorker:
    """Serialize alert sounds on a daemon thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Path, int, str] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="alert-sound", daemon=True)
        self._thread.start()

    def play(self, path: Path, repeat: int, label: str) -> None:
        self._queue.put((path, max(1, repeat), label))

    def close(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            path, repeat, label = item
            try:
                for index in range(repeat):
                    if os.name == "nt" and winsound is not None and path.is_file():
                        winsound.PlaySound(str(path), winsound.SND_FILENAME)
                    elif os.name == "nt" and winsound is not None:
                        lower_label = label.lower()
                        if "urgent" in lower_label:
                            fallback = 1900
                        else:
                            fallback = 1500 if "splinter" in lower_label else 900
                        winsound.Beep(fallback, 350)
                    else:  # Basic fallback for non-Windows terminals.
                        print("\a", end="", flush=True)
                    if index + 1 < repeat:
                        time.sleep(0.12)
            except Exception as exc:  # Sound failure must not stop monitoring.
                print(f"\n[Sound error] {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read Splinter Storm's timer from the visible Warframe HUD, "
            "warn before it expires, and alert when Roar's ready icon returns."
        )
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="capture your HUD and create icon/timer calibration",
    )
    parser.add_argument(
        "--calibrate-buff",
        default=None,
        help=(
            "which calibration to refresh: all, splinter_storm, or roar "
            "(calibration prompts when omitted)"
        ),
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=None,
        help="MSS monitor index for calibration (normally 1)",
    )
    parser.add_argument(
        "--capture-delay",
        type=int,
        default=9,
        help="seconds to switch to Warframe and prepare the selected calibration target",
    )
    parser.add_argument(
        "--ignore-focus",
        action="store_true",
        help="scan even when a window titled Warframe is not foreground",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="periodically save annotated screenshots and timer crops under debug/",
    )
    parser.add_argument(
        "--test-sounds",
        action="store_true",
        help="play configured alert sounds and exit",
    )
    parser.add_argument(
        "--list-monitors",
        action="store_true",
        help="show screenshot monitor indices and exit",
    )
    return parser.parse_args()


def monitor_to_dict(monitor: Any) -> dict[str, int]:
    return {
        "left": int(monitor["left"]),
        "top": int(monitor["top"]),
        "width": int(monitor["width"]),
        "height": int(monitor["height"]),
    }


def list_monitors() -> list[dict[str, int]]:
    with mss.MSS() as sct:
        return [monitor_to_dict(monitor) for monitor in sct.monitors]


def print_monitors(monitors: list[dict[str, int]]) -> None:
    print("Available screenshot regions:")
    for index, monitor in enumerate(monitors):
        label = "all monitors" if index == 0 else f"monitor {index}"
        print(
            f"  {index}: {label} — {monitor['width']}x{monitor['height']} "
            f"at ({monitor['left']}, {monitor['top']})"
        )


def choose_monitor(monitors: list[dict[str, int]], requested: Optional[int]) -> int:
    if requested is not None:
        if requested <= 0 or requested >= len(monitors):
            raise ConfigurationError(
                f"Monitor {requested} does not exist. Use --list-monitors."
            )
        return requested
    if len(monitors) == 2:
        return 1

    print_monitors(monitors)
    while True:
        raw = input("Which monitor contains Warframe? [1]: ").strip()
        if not raw:
            raw = "1"
        try:
            index = int(raw)
        except ValueError:
            print("Enter a monitor number.")
            continue
        if 0 < index < len(monitors):
            return index
        print("Choose one of the numbered physical monitors, not 0/all monitors.")


def clamp_relative_box(
    box: dict[str, int], monitor: dict[str, int]
) -> dict[str, int]:
    monitor_width = int(monitor["width"])
    monitor_height = int(monitor["height"])
    left = min(max(0, int(box["left"])), max(0, monitor_width - 1))
    top = min(max(0, int(box["top"])), max(0, monitor_height - 1))
    width = min(max(1, int(box["width"])), monitor_width - left)
    height = min(max(1, int(box["height"])), monitor_height - top)
    return {"left": left, "top": top, "width": width, "height": height}


def full_monitor_box(monitor: dict[str, int]) -> dict[str, int]:
    return {
        "left": 0,
        "top": 0,
        "width": int(monitor["width"]),
        "height": int(monitor["height"]),
    }


def default_splinter_storm_search_box(monitor: dict[str, int]) -> dict[str, int]:
    """Full-width top HUD band so buff-bar ordering can drift safely."""
    width = monitor["width"]
    height = monitor["height"]
    return clamp_relative_box(
        {
            "left": 0,
            "top": 0,
            "width": width,
            "height": max(320, int(height * 0.50)),
        },
        monitor,
    )


def default_roar_search_box(monitor: dict[str, int]) -> dict[str, int]:
    """Bottom-right ability HUD area used for Roar indicator tracking."""
    width = monitor["width"]
    height = monitor["height"]
    box_width = max(520, int(width * 0.50))
    box_height = max(360, int(height * 0.50))
    return clamp_relative_box(
        {
            "left": width - box_width,
            "top": height - box_height,
            "width": box_width,
            "height": box_height,
        },
        monitor,
    )


def default_search_boxes(monitor: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        "splinter_storm": default_splinter_storm_search_box(monitor),
        "roar": default_roar_search_box(monitor),
    }


def default_search_box(monitor: dict[str, int]) -> dict[str, int]:
    """Backward-compatible alias for Splinter Storm's top HUD band."""
    return default_splinter_storm_search_box(monitor)


def coerce_search_box(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be an object with left/top/width/height.")
    try:
        box = {
            "left": int(value["left"]),
            "top": int(value["top"]),
            "width": int(value["width"]),
            "height": int(value["height"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"{label} must contain integer left/top/width/height values."
        ) from exc
    if box["width"] <= 0 or box["height"] <= 0:
        raise ConfigurationError(f"{label} width and height must be positive.")
    return box


def search_boxes_for_config(config: dict[str, Any]) -> dict[str, dict[str, int]]:
    configured_boxes = config.get("search_boxes", {})
    fallback = config.get("search_box")
    if configured_boxes is not None and not isinstance(configured_boxes, dict):
        raise ConfigurationError("search_boxes must be an object when present.")

    result: dict[str, dict[str, int]] = {}
    for key in config.get("buffs", {}):
        configured = (
            configured_boxes.get(key) if isinstance(configured_boxes, dict) else None
        )
        if configured is None:
            configured = fallback
        if configured is None:
            raise ConfigurationError(
                f"Calibration is missing a search box for {key!r}; recalibrate."
            )
        result[key] = coerce_search_box(configured, f"search box for {key}")
    return result


def absolute_capture_box(
    monitor: dict[str, int], relative_box: dict[str, int]
) -> dict[str, int]:
    return {
        "left": monitor["left"] + int(relative_box["left"]),
        "top": monitor["top"] + int(relative_box["top"]),
        "width": int(relative_box["width"]),
        "height": int(relative_box["height"]),
    }


def grab_bgr(
    sct: Any, monitor: dict[str, int], relative_box: dict[str, int]
) -> np.ndarray:
    shot = sct.grab(absolute_capture_box(monitor, relative_box))
    bgra = np.asarray(shot)
    if bgra.ndim != 3 or bgra.shape[2] < 3:
        raise RuntimeError("Unexpected screenshot format from MSS.")
    return np.ascontiguousarray(bgra[:, :, :3])


def fit_for_selection(
    image: np.ndarray,
    max_width: int = 1760,
    max_height: int = 920,
    max_upscale: float = 2.0,
) -> tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    scale = min(max_width / width, max_height / height, max_upscale)
    scale = max(scale, 0.1)
    if abs(scale - 1.0) < 0.01:
        return image.copy(), 1.0
    interpolation = cv2.INTER_NEAREST if scale > 1.0 else cv2.INTER_AREA
    resized = cv2.resize(
        image,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=interpolation,
    )
    return resized, scale


def select_roi(
    image: np.ndarray,
    title: str,
    *,
    max_width: int = 1760,
    max_height: int = 920,
    max_upscale: float = 2.0,
) -> tuple[int, int, int, int]:
    display, scale = fit_for_selection(
        image,
        max_width=max_width,
        max_height=max_height,
        max_upscale=max_upscale,
    )
    print(f"\n{title}")
    print("Drag a box, then press ENTER or SPACE. Press C to cancel and retry.")
    window = title[:110]
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.imshow(window, display)
    roi = cv2.selectROI(window, display, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window)
    cv2.waitKey(1)
    x, y, width, height = (int(value) for value in roi)
    if width <= 0 or height <= 0:
        raise ConfigurationError("Selection cancelled or empty.")

    converted = (
        int(round(x / scale)),
        int(round(y / scale)),
        max(1, int(round(width / scale))),
        max(1, int(round(height / scale))),
    )
    return clamp_roi(converted, image.shape[1], image.shape[0])


def clamp_roi(
    roi: tuple[int, int, int, int], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    x, y, width, height = roi
    x = min(max(0, x), max(0, image_width - 1))
    y = min(max(0, y), max(0, image_height - 1))
    width = min(max(1, width), image_width - x)
    height = min(max(1, height), image_height - y)
    return x, y, width, height


def padded_roi(
    roi: tuple[int, int, int, int],
    pad: int,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int]:
    x, y, width, height = roi
    return clamp_roi(
        (x - pad, y - pad, width + pad * 2, height + pad * 2),
        image_width,
        image_height,
    )


def crop_roi(image: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    x, y, width, height = roi
    return image[y : y + height, x : x + width]


def find_tesseract(configured: str = "") -> Path:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(os.path.expandvars(configured)).expanduser())

    located = shutil.which("tesseract")
    if located:
        candidates.append(Path(located))

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.extend(
        [
            Path(program_files) / "Tesseract-OCR" / "tesseract.exe",
            Path(program_files_x86) / "Tesseract-OCR" / "tesseract.exe",
            Path(local_app_data) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ConfigurationError(
        "Tesseract OCR was not found. Install Tesseract 5, then rerun calibration.\n"
        "The script checks PATH plus the usual C:\\Program Files\\Tesseract-OCR location."
    )


def configure_tesseract(configured: str = "") -> Path:
    executable = find_tesseract(configured)
    pytesseract.pytesseract.tesseract_cmd = str(executable)
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise ConfigurationError(f"Tesseract exists but could not run: {exc}") from exc
    return executable


def preprocess_timer(image: np.ndarray) -> dict[str, np.ndarray]:
    if image.size == 0:
        return {}
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    # Tiny HUD digits become far easier for OCR after aggressive nearest/cubic
    # enlargement. Keep the resulting height in a useful 80–120px range.
    target_height = 100
    scale = min(14.0, max(5.0, target_height / max(1, gray.shape[0])))
    enlarged = cv2.resize(
        gray,
        (
            max(8, int(round(gray.shape[1] * scale))),
            max(8, int(round(gray.shape[0] * scale))),
        ),
        interpolation=cv2.INTER_CUBIC,
    )
    enlarged = cv2.GaussianBlur(enlarged, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(enlarged)

    _, otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, otsu_inv = cv2.threshold(
        clahe, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    adaptive_inv = cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        5,
    )

    def border(img: np.ndarray, value: int) -> np.ndarray:
        pad = max(12, int(round(img.shape[0] * 0.18)))
        return cv2.copyMakeBorder(
            img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=value
        )

    return {
        "gray": border(enlarged, 255),
        "clahe": border(clahe, 255),
        "otsu": border(otsu, 0),
        "otsu_inv": border(otsu_inv, 255),
        "adaptive_inv": border(adaptive_inv, 255),
    }


def parse_timer_text(text: str) -> Optional[float]:
    compact = re.sub(r"\s+", "", text)
    compact = compact.replace(",", ".")
    if not compact:
        return None

    minute_match = re.search(r"(\d{1,2}):(\d{1,2})", compact)
    if minute_match:
        minutes = int(minute_match.group(1))
        seconds = int(minute_match.group(2))
        if seconds < 60:
            return float(minutes * 60 + seconds)

    number_match = re.search(r"\d+(?:\.\d+)?", compact)
    if not number_match:
        return None
    raw_number = number_match.group(0)
    try:
        seconds = float(raw_number)
    except ValueError:
        return None
    if "." not in raw_number and len(raw_number) == 3:
        decimal_seconds = seconds / 10.0
        if decimal_seconds <= MISSING_DECIMAL_MAX_SECONDS:
            return decimal_seconds
    return seconds


def mode_parts(mode: str) -> tuple[str, int]:
    try:
        variant, psm_text = mode.split(":", 1)
        return variant, int(psm_text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid OCR mode: {mode}") from exc


def ocr_candidates(
    image: np.ndarray,
    modes: Iterable[str],
    *,
    timeout: float = 0.55,
) -> list[OCRResult]:
    prepared = preprocess_timer(image)
    results: list[OCRResult] = []
    seen: set[tuple[float, str]] = set()
    for mode in modes:
        variant, psm = mode_parts(mode)
        candidate_image = prepared.get(variant)
        if candidate_image is None:
            continue
        config = (
            f"--oem 3 --psm {psm} "
            "-c tessedit_char_whitelist=0123456789.: "
            "-c load_system_dawg=0 -c load_freq_dawg=0"
        )
        try:
            text = pytesseract.image_to_string(
                candidate_image,
                config=config,
                timeout=timeout,
            ).strip()
        except RuntimeError:  # OCR timeout.
            continue
        except Exception:
            continue
        seconds = parse_timer_text(text)
        if seconds is None:
            continue
        marker = (seconds, text)
        if marker in seen:
            continue
        seen.add(marker)
        results.append(OCRResult(seconds=seconds, text=text, mode=mode))
    return results


def choose_ocr_result(
    candidates: list[OCRResult],
    expected: Optional[float],
    max_reasonable: float,
) -> Optional[OCRResult]:
    candidates = [
        candidate
        for candidate in candidates
        if 0.0 <= candidate.seconds <= max_reasonable
    ]
    if not candidates:
        return None
    if expected is None:
        return candidates[0]

    close = [candidate for candidate in candidates if abs(candidate.seconds - expected) <= 7.0]
    if close:
        return min(close, key=lambda candidate: abs(candidate.seconds - expected))

    # No normal countdown reading matched. Preserve preferred-mode ordering so
    # a genuine refresh (large upward jump) can still be detected.
    return candidates[0]


def read_timer(
    image: np.ndarray,
    preferred_mode: str,
    *,
    expected: Optional[float],
    max_reasonable: float,
    exhaustive: bool = False,
) -> Optional[OCRResult]:
    """Read a timer while avoiding a pile of Tesseract subprocesses.

    The calibrated mode is tried first and normally costs one OCR call. Fallback
    modes are attempted only when that mode fails or returns an implausible value.
    """
    ordered_modes: list[str] = []
    if preferred_mode:
        ordered_modes.append(preferred_mode)
    ordered_modes.extend(mode for mode in OCR_MODES if mode not in ordered_modes)
    if not exhaustive:
        ordered_modes = ordered_modes[:3]

    suspicious: list[OCRResult] = []
    for mode in ordered_modes:
        candidates = ocr_candidates(image, [mode])
        if not candidates:
            continue
        candidate = candidates[0]
        if not (0.0 <= candidate.seconds <= max_reasonable):
            continue
        if expected is None:
            return candidate
        # Normal countdown: return immediately. For a large jump, try the
        # fallback modes first; one of them may correctly read 12 when the
        # preferred mode briefly says 112. If every mode reports a jump, the
        # first result is returned below and TrackerState confirms it twice.
        if abs(candidate.seconds - expected) <= 7.0:
            return candidate
        suspicious.append(candidate)

    return choose_ocr_result(suspicious, expected, max_reasonable)


def template_detection(
    search_gray: np.ndarray, template_gray: np.ndarray
) -> Optional[Detection]:
    template_height, template_width = template_gray.shape[:2]
    if template_height > search_gray.shape[0] or template_width > search_gray.shape[1]:
        return None
    result = cv2.matchTemplate(search_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _min_value, max_value, _min_location, max_location = cv2.minMaxLoc(result)
    return Detection(
        x=int(max_location[0]),
        y=int(max_location[1]),
        width=int(template_width),
        height=int(template_height),
        score=float(max_value),
    )


def edge_map(gray: np.ndarray) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.Canny(blurred, 40, 120)


def locate_template(
    search: np.ndarray, template: np.ndarray, match_mode: str = "gray"
) -> Optional[Detection]:
    if search.size == 0 or template.size == 0:
        return None
    search_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    best = template_detection(search_gray, template_gray)
    if match_mode != "gray_or_edges":
        return best

    template_edges = edge_map(template_gray)
    if int(np.count_nonzero(template_edges)) < 8:
        return best
    search_edges = edge_map(search_gray)
    edge_detection = template_detection(search_edges, template_edges)
    if edge_detection is not None and (
        best is None or edge_detection.score > best.score
    ):
        return edge_detection
    return best


def matched_icon_is_dimmed(
    frame: np.ndarray,
    detection: Detection,
    template: np.ndarray,
    brightness_ratio: float,
    contrast_ratio: float,
) -> bool:
    crop = crop_roi(
        frame,
        (detection.x, detection.y, detection.width, detection.height),
    )
    if crop.size == 0 or template.size == 0:
        return False
    if crop.shape[:2] != template.shape[:2]:
        template = cv2.resize(
            template,
            (crop.shape[1], crop.shape[0]),
            interpolation=cv2.INTER_AREA,
        )

    crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    cutoff = float(np.percentile(template_gray, 85))
    bright_mask = template_gray >= cutoff
    if int(np.count_nonzero(bright_mask)) < 8:
        bright_mask = template_gray >= float(np.percentile(template_gray, 75))
    if int(np.count_nonzero(bright_mask)) < 8:
        return False

    crop_bright = float(np.mean(crop_gray[bright_mask]))
    template_bright = max(1.0, float(np.mean(template_gray[bright_mask])))
    crop_contrast = float(
        np.percentile(crop_gray, 90) - np.percentile(crop_gray, 10)
    )
    template_contrast = max(
        1.0,
        float(np.percentile(template_gray, 90) - np.percentile(template_gray, 10)),
    )
    return (
        crop_bright <= template_bright * brightness_ratio
        and crop_contrast <= template_contrast * contrast_ratio
    )


def ready_icon_actionable(
    frame: np.ndarray,
    detection: Detection,
    template: np.ndarray,
    buff: dict[str, Any],
) -> bool:
    if not bool(buff.get("suppress_dim_ready_icon", True)):
        return True
    return not matched_icon_is_dimmed(
        frame,
        detection,
        template,
        float(buff.get("dim_ready_brightness_ratio", 0.72)),
        float(buff.get("dim_ready_contrast_ratio", 0.75)),
    )


def locate_ready_icon(
    frame: np.ndarray,
    ready_template: np.ndarray,
    disabled_template: Optional[np.ndarray],
    buff: dict[str, Any],
) -> tuple[Optional[Detection], bool]:
    match_mode = str(buff.get("match_mode", "gray_or_edges"))
    ready_threshold = float(buff.get("match_threshold", 0.68))
    ready_detection = locate_template(frame, ready_template, match_mode)
    ready_ok = (
        ready_detection is not None and ready_detection.score >= ready_threshold
    )

    if disabled_template is not None:
        disabled_detection = locate_template(frame, disabled_template, match_mode)
        disabled_threshold = float(
            buff.get("disabled_match_threshold", ready_threshold)
        )
        disabled_ok = (
            disabled_detection is not None
            and disabled_detection.score >= disabled_threshold
        )
        if disabled_ok:
            ready_score = ready_detection.score if ready_detection is not None else 0.0
            margin = float(buff.get("disabled_match_margin", 0.02))
            if not ready_ok or disabled_detection.score >= ready_score + margin:
                return disabled_detection, False

    if ready_ok:
        actionable = ready_icon_actionable(frame, ready_detection, ready_template, buff)
        return ready_detection, actionable
    return None, True


def safe_timer_crop(
    frame: np.ndarray,
    detection: Detection,
    timer_offset: dict[str, int],
    timer_size: dict[str, int],
) -> Optional[np.ndarray]:
    x = detection.x + int(timer_offset["x"])
    y = detection.y + int(timer_offset["y"])
    width = int(timer_size["width"])
    height = int(timer_size["height"])
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    if x + width > frame.shape[1] or y + height > frame.shape[0]:
        return None
    return frame[y : y + height, x : x + width]


def foreground_window_title() -> str:
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def warframe_is_foreground() -> bool:
    return "warframe" in foreground_window_title().lower()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise ConfigurationError(
            "No calibration found. Run calibrate.bat or use --calibrate first."
        )
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read {CONFIG_PATH.name}: {exc}") from exc
    if config.get("version") != CONFIG_VERSION:
        raise ConfigurationError("Calibration version is unsupported; recalibrate.")
    for required in ("monitor", "monitor_geometry", "buffs"):
        if required not in config:
            raise ConfigurationError(f"Calibration is missing '{required}'; recalibrate.")
    if "search_box" not in config and "search_boxes" not in config:
        raise ConfigurationError("Calibration is missing search boxes; recalibrate.")
    search_boxes_for_config(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def preview_timer_and_get_expected(timer_crop: np.ndarray, name: str) -> Optional[float]:
    prepared = preprocess_timer(timer_crop)
    preview = prepared.get("clahe")
    if preview is None:
        return None
    title = f"{name} timer preview"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.imshow(title, preview)
    cv2.waitKey(1)
    print(
        f"Look at the enlarged {name} timer preview. Type the number shown so "
        "the script can choose the best OCR mode."
    )
    while True:
        raw = input("Visible timer value (or Enter to skip): ").strip()
        if not raw:
            value = None
            break
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            print("Enter only the visible number, such as 27 or 7.5.")
            continue
        break
    cv2.destroyWindow(title)
    cv2.waitKey(1)
    return value


def calibrate_roar_disabled_template(
    frame: np.ndarray, defaults: dict[str, Any]
) -> str:
    name = str(defaults["name"])
    template_path = BASE_DIR / str(defaults["disabled_template"])

    while True:
        try:
            tile_roi = select_roi(
                frame,
                "STEP 1 - Select the whole bottom-right Roar no-energy indicator",
                max_upscale=1.6,
            )
            tile_roi = padded_roi(tile_roi, 8, frame.shape[1], frame.shape[0])
            tile = crop_roi(frame, tile_roi)

            icon_local = select_roi(
                tile,
                "STEP 2 - Select ONLY the grayed/no-energy Roar icon, tightly",
                max_width=1100,
                max_height=850,
                max_upscale=12.0,
            )
        except ConfigurationError as exc:
            print(exc)
            retry = input("Retry the Roar no-energy icon? [Y/n]: ").strip().lower()
            if retry in ("", "y", "yes"):
                continue
            raise

        tile_x, tile_y, _, _ = tile_roi
        icon_x, icon_y, icon_width, icon_height = icon_local
        icon_absolute = (
            tile_x + icon_x,
            tile_y + icon_y,
            icon_width,
            icon_height,
        )
        template = crop_roi(frame, icon_absolute)
        if template.shape[0] < 6 or template.shape[1] < 6:
            print("That icon selection is too small; select it again.")
            continue

        template_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(template_path), template):
            raise RuntimeError(f"Could not save {template_path}")
        print(f"{name}: no-energy icon calibrated.")
        return str(defaults["disabled_template"])


def calibrate_buff(
    frame: np.ndarray,
    key: str,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    name = str(defaults["name"])
    alert_mode = str(defaults.get("alert_mode", "thresholds"))
    needs_timer = alert_mode == "thresholds"

    while True:
        try:
            tile_description = "icon and timer" if needs_timer else "icon"
            tile_subject = (
                f"bottom-right {name} ready/off indicator"
                if key == "roar"
                else f"{name} top buff tile"
            )
            tile_roi = select_roi(
                frame,
                f"STEP 1 - Select the whole {tile_subject} ({tile_description})",
                max_upscale=1.6,
            )
            tile_roi = padded_roi(tile_roi, 8, frame.shape[1], frame.shape[0])
            tile = crop_roi(frame, tile_roi)

            icon_local = select_roi(
                tile,
                f"STEP 2 — Select ONLY the {name} icon, tightly",
                max_width=1100,
                max_height=850,
                max_upscale=12.0,
            )
            timer_local: Optional[tuple[int, int, int, int]] = None
            if needs_timer:
                timer_local = select_roi(
                    tile,
                    f"STEP 3 — Select ONLY the {name} timer digits",
                    max_width=1100,
                    max_height=850,
                    max_upscale=12.0,
                )
        except ConfigurationError as exc:
            print(exc)
            retry = input("Retry this buff? [Y/n]: ").strip().lower()
            if retry in ("", "y", "yes"):
                continue
            raise

        tile_x, tile_y, _, _ = tile_roi
        icon_x, icon_y, icon_width, icon_height = icon_local
        icon_absolute = (
            tile_x + icon_x,
            tile_y + icon_y,
            icon_width,
            icon_height,
        )
        template = crop_roi(frame, icon_absolute)
        if template.shape[0] < 6 or template.shape[1] < 6:
            print("That icon selection is too small; select it again.")
            continue

        template_path = BASE_DIR / str(defaults["template"])
        template_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(template_path), template):
            raise RuntimeError(f"Could not save {template_path}")

        buff_config: dict[str, Any] = {
            "name": name,
            "template": str(defaults["template"]),
            "sound": str(defaults["sound"]),
            "alert_mode": alert_mode,
            "warnings": list(defaults.get("warnings", [])),
            "match_threshold": float(defaults["match_threshold"]),
            "match_mode": str(defaults.get("match_mode", "gray")),
            "max_reasonable_seconds": float(defaults["max_reasonable_seconds"]),
            "missing_grace_seconds": float(
                defaults.get("missing_grace_seconds", 2.2)
            ),
            "presence_confirm_hits": int(defaults.get("presence_confirm_hits", 2)),
            "expiry_repeat": int(defaults.get("expiry_repeat", 1)),
            "inactive_reminder_seconds": float(
                defaults.get("inactive_reminder_seconds", 0.0)
            ),
            "inactive_reminder_repeat": int(
                defaults.get("inactive_reminder_repeat", 1)
            ),
            "max_inactive_alerts": int(defaults.get("max_inactive_alerts", 0)),
        }
        if alert_mode == "on_ready":
            buff_config.update(
                {
                    "disabled_template": str(defaults["disabled_template"]),
                    "disabled_match_threshold": float(
                        defaults.get("disabled_match_threshold", 0.68)
                    ),
                    "disabled_match_margin": float(
                        defaults.get("disabled_match_margin", 0.02)
                    ),
                    "suppress_dim_ready_icon": bool(
                        defaults.get("suppress_dim_ready_icon", True)
                    ),
                    "dim_ready_brightness_ratio": float(
                        defaults.get("dim_ready_brightness_ratio", 0.72)
                    ),
                    "dim_ready_contrast_ratio": float(
                        defaults.get("dim_ready_contrast_ratio", 0.75)
                    ),
                }
            )
        if "urgent_sound" in defaults:
            buff_config["urgent_sound"] = str(defaults["urgent_sound"])

        # Roar is deliberately icon-only. It uses the stable bottom-right
        # ready/off icon and alerts when that icon returns after a cast.
        if not needs_timer:
            print(f"{name}: icon calibrated; no timer OCR is used for this buff.")
            return buff_config

        assert timer_local is not None
        timer_x, timer_y, timer_width, timer_height = timer_local
        timer_absolute = (
            tile_x + timer_x,
            tile_y + timer_y,
            timer_width,
            timer_height,
        )
        timer_crop = crop_roi(frame, timer_absolute)
        if timer_crop.shape[0] < 3 or timer_crop.shape[1] < 3:
            print("That timer selection is too small; select it again.")
            continue

        expected = preview_timer_and_get_expected(timer_crop, name)
        all_candidates = ocr_candidates(timer_crop, OCR_MODES, timeout=0.8)
        if expected is not None:
            chosen = choose_ocr_result(
                all_candidates,
                expected=expected,
                max_reasonable=float(defaults["max_reasonable_seconds"]),
            )
        else:
            chosen = all_candidates[0] if all_candidates else None

        if chosen is None:
            print("OCR could not read that selection.")
            retry = input("Reselect the timer digits? [Y/n]: ").strip().lower()
            if retry in ("", "y", "yes"):
                continue
            preferred_mode = OCR_MODES[0]
        else:
            print(
                f"OCR test for {name}: read {chosen.seconds:g} "
                f"using {chosen.mode} (raw: {chosen.text!r})"
            )
            if expected is not None and abs(chosen.seconds - expected) > 2.0:
                retry = input("That looks wrong. Reselect the timer? [Y/n]: ").strip().lower()
                if retry in ("", "y", "yes"):
                    continue
            preferred_mode = chosen.mode

        buff_config.update(
            {
                "timer_offset": {
                    "x": timer_absolute[0] - icon_absolute[0],
                    "y": timer_absolute[1] - icon_absolute[1],
                },
                "timer_size": {
                    "width": timer_absolute[2],
                    "height": timer_absolute[3],
                },
                "ocr_mode": preferred_mode,
            }
        )
        return buff_config

def beep_capture_complete() -> None:
    if os.name == "nt" and winsound is not None:
        try:
            winsound.Beep(1200, 160)
            winsound.Beep(1600, 220)
        except Exception:
            pass


def normalize_calibration_target(raw: Optional[str]) -> str:
    if raw is None:
        raise ConfigurationError("No calibration target was provided.")
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    key = key.replace("__", "_")
    target = CALIBRATION_TARGET_ALIASES.get(key)
    if target is None:
        raise ConfigurationError(
            "Calibration target must be all, splinter_storm, or roar."
        )
    return target


def choose_calibration_target(raw: Optional[str]) -> str:
    if raw is not None:
        return normalize_calibration_target(raw)

    print("\nWhat do you want to recalibrate?")
    print("  1. all")
    print("  2. Splinter Storm only")
    print("  3. Roar ready/no-energy icons only")
    while True:
        choice = input("Choose [1/all]: ").strip()
        try:
            return normalize_calibration_target(choice)
        except ConfigurationError as exc:
            print(exc)


def calibration_keys_for_target(target: str) -> list[str]:
    if target == "all":
        return list(DEFAULT_BUFFS)
    return [target]


def calibration_defaults(
    key: str, existing_config: Optional[dict[str, Any]]
) -> dict[str, Any]:
    defaults = dict(DEFAULT_BUFFS[key])
    if existing_config is not None:
        existing_buffs = existing_config.get("buffs", {})
        existing = existing_buffs.get(key) if isinstance(existing_buffs, dict) else None
        if isinstance(existing, dict):
            defaults.update(existing)
    if key == "roar":
        defaults["alert_mode"] = "on_ready"
        defaults["warnings"] = []
        defaults.setdefault("match_mode", "gray_or_edges")
        defaults.setdefault("disabled_template", "templates/roar_disabled.png")
        defaults.setdefault("disabled_match_threshold", 0.68)
        defaults.setdefault("disabled_match_margin", 0.02)
        defaults.setdefault("max_inactive_alerts", 5)
        defaults.setdefault("suppress_dim_ready_icon", True)
        defaults.setdefault("dim_ready_brightness_ratio", 0.72)
        defaults.setdefault("dim_ready_contrast_ratio", 0.75)
    return defaults


def monitor_for_calibration(
    monitors: list[dict[str, int]],
    requested: Optional[int],
    existing_config: Optional[dict[str, Any]],
    partial: bool,
) -> int:
    if requested is not None:
        return choose_monitor(monitors, requested)
    if existing_config is not None:
        monitor_index = int(existing_config["monitor"])
        if monitor_index <= 0 or monitor_index >= len(monitors):
            raise ConfigurationError(
                "The calibrated monitor no longer exists; run full calibration."
            )
        if partial:
            verify_monitor_geometry(existing_config["monitor_geometry"], monitors[monitor_index])
        return monitor_index
    return choose_monitor(monitors, None)


def calibration_instructions(keys: list[str]) -> str:
    prep: list[str] = []
    if "splinter_storm" in keys:
        prep.append("activate Splinter Storm")
    if "roar" in keys:
        prep.append("leave Roar off/ready")
    if len(prep) == 1:
        action = prep[0]
    else:
        action = ", ".join(prep[:-1]) + ", and " + prep[-1]

    capture_parts: list[str] = []
    if "splinter_storm" in keys:
        capture_parts.append("the full top buff bar for Splinter Storm")
    if "roar" in keys:
        capture_parts.append("the bottom-right ready icon for Roar")
    if len(capture_parts) == 1:
        capture_text = capture_parts[0]
    else:
        capture_text = ", and ".join(capture_parts)

    return (
        "\nBefore the countdown:\n"
        "  - Set Warframe to Borderless Fullscreen or Windowed.\n"
        "  - Enter a mission as Gara with Roar equipped.\n"
        "  - Keep your HUD scale and resolution at the values you normally use.\n"
        f"\nAfter pressing Enter, switch to Warframe, {action}. "
        f"The script will capture {capture_text}."
    )


def run_calibration(args: argparse.Namespace) -> None:
    print(f"\n=== {APP_NAME}: calibration ===")
    target = choose_calibration_target(args.calibrate_buff)
    calibration_keys = calibration_keys_for_target(target)
    partial = target != "all"

    existing_config: Optional[dict[str, Any]] = None
    if CONFIG_PATH.is_file():
        try:
            existing_config = load_config()
        except ConfigurationError:
            if partial:
                raise
            existing_config = None
    elif partial:
        raise ConfigurationError(
            "Partial calibration needs an existing config.json. Choose all first."
        )

    executable = configure_tesseract(
        "" if existing_config is None else str(existing_config.get("tesseract_cmd", ""))
    )
    print(f"Tesseract: {executable}")
    monitors = list_monitors()
    monitor_index = monitor_for_calibration(
        monitors,
        args.monitor,
        existing_config,
        partial,
    )
    monitor = monitors[monitor_index]
    search_boxes = default_search_boxes(monitor)

    print(calibration_instructions(calibration_keys))
    input("Press Enter to begin the countdown...")
    delay = max(3, int(args.capture_delay))
    for remaining in range(delay, 0, -1):
        print(f"Capturing in {remaining:2d}s...", end="\r", flush=True)
        time.sleep(1.0)
    print("Capturing now!          ")

    with mss.MSS() as sct:
        calibration_frames = {
            key: grab_bgr(sct, monitor, search_boxes[key])
            for key in calibration_keys
        }
    beep_capture_complete()
    print("Screenshot frozen. Alt-Tab back to the selector windows if needed.")

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    if existing_config is None:
        buff_configs: dict[str, Any] = {}
    else:
        buff_configs = dict(existing_config.get("buffs", {}))

    if partial:
        missing = [
            key
            for key in DEFAULT_BUFFS
            if key not in buff_configs and key not in calibration_keys
        ]
        if missing:
            names = ", ".join(DEFAULT_BUFFS[key]["name"] for key in missing)
            raise ConfigurationError(
                f"Existing calibration is missing {names}; choose all calibration."
            )

    for key in calibration_keys:
        defaults = calibration_defaults(key, existing_config)
        buff_configs[key] = calibrate_buff(calibration_frames[key], key, defaults)

    if isinstance(buff_configs.get("roar"), dict):
        if "roar" in calibration_keys:
            choice = input(
                "\nCalibrate Roar's grayed/no-energy icon too? [Y/n]: "
            ).strip().lower()
            if choice in ("", "y", "yes"):
                print(
                    "\nSwitch to Warframe and make Roar's bottom-right icon "
                    "gray because you do not have enough energy."
                )
                input("Press Enter to begin the no-energy capture countdown...")
                delay = max(3, int(args.capture_delay))
                for remaining in range(delay, 0, -1):
                    print(f"Capturing in {remaining:2d}s...", end="\r", flush=True)
                    time.sleep(1.0)
                print("Capturing now!          ")
                with mss.MSS() as sct:
                    disabled_frame = grab_bgr(sct, monitor, search_boxes["roar"])
                beep_capture_complete()
                print(
                    "No-energy screenshot frozen. Alt-Tab back to the selector "
                    "window if needed."
                )
                defaults = calibration_defaults(
                    "roar", {"buffs": {"roar": buff_configs["roar"]}}
                )
                buff_configs["roar"][
                    "disabled_template"
                ] = calibrate_roar_disabled_template(disabled_frame, defaults)

        buff_configs["roar"]["alert_mode"] = "on_ready"
        buff_configs["roar"]["warnings"] = []
        buff_configs["roar"].setdefault("match_mode", "gray_or_edges")
        buff_configs["roar"].setdefault(
            "disabled_template", "templates/roar_disabled.png"
        )
        buff_configs["roar"].setdefault("disabled_match_threshold", 0.68)
        buff_configs["roar"].setdefault("disabled_match_margin", 0.02)
        buff_configs["roar"].setdefault("max_inactive_alerts", 5)
        buff_configs["roar"].setdefault("suppress_dim_ready_icon", True)
        buff_configs["roar"].setdefault("dim_ready_brightness_ratio", 0.72)
        buff_configs["roar"].setdefault("dim_ready_contrast_ratio", 0.75)

    config: dict[str, Any] = dict(existing_config or {})
    config.update(
        {
            "version": CONFIG_VERSION,
            "monitor": monitor_index,
            "monitor_geometry": monitor,
            "search_box": search_boxes["splinter_storm"],
            "search_boxes": search_boxes,
            "tesseract_cmd": str(executable),
            "buffs": buff_configs,
        }
    )
    config.setdefault("scan_interval_seconds", 0.45)
    config.setdefault("ocr_interval_seconds", 0.72)
    config.setdefault("only_when_warframe_focused", True)
    save_config(config)
    print(f"\nCalibration saved to {CONFIG_PATH}")
    print("Run start_alarm.bat. Keep this folder together; config uses relative paths.")


def verify_monitor_geometry(
    configured: dict[str, int], actual: dict[str, int]
) -> None:
    keys = ("left", "top", "width", "height")
    if any(int(configured[key]) != int(actual[key]) for key in keys):
        raise ConfigurationError(
            "The calibrated monitor geometry changed. Recalibrate after changing "
            "display resolution, scaling, orientation, or monitor arrangement."
        )


def build_runtime(
    config: dict[str, Any]
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict[str, TrackerState]]:
    templates: dict[str, np.ndarray] = {}
    disabled_templates: dict[str, np.ndarray] = {}
    states: dict[str, TrackerState] = {}
    for key, buff in config["buffs"].items():
        path = BASE_DIR / str(buff["template"])
        template = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if template is None:
            raise ConfigurationError(f"Missing template image: {path}")
        templates[key] = template
        if key == "roar":
            disabled_template = str(buff.get("disabled_template", "")).strip()
            if disabled_template:
                disabled_path = BASE_DIR / disabled_template
                if disabled_path.is_file():
                    disabled_image = cv2.imread(str(disabled_path), cv2.IMREAD_COLOR)
                    if disabled_image is not None:
                        disabled_templates[key] = disabled_image
        warnings = [float(value) for value in buff.get("warnings", [])]

        # Backward compatibility: old configs had Roar warning thresholds and
        # timer coordinates. They are ignored; Roar now uses the stable
        # bottom-right ready/off icon and alerts when that icon returns.
        default_mode = "on_ready" if key == "roar" else "thresholds"
        alert_mode = str(buff.get("alert_mode", default_mode))
        if key == "roar":
            alert_mode = "on_ready"
            warnings = []
        if alert_mode not in {"thresholds", "on_expire", "on_ready"}:
            raise ConfigurationError(
                f"Unsupported alert_mode {alert_mode!r} for {buff.get('name', key)}."
            )

        states[key] = TrackerState(
            key=key,
            name=str(buff["name"]),
            warnings=warnings,
            max_reasonable_seconds=float(buff.get("max_reasonable_seconds", 180)),
            alert_mode=alert_mode,
            missing_grace_seconds=max(
                0.4,
                float(
                    buff.get(
                        "missing_grace_seconds",
                        1.8 if alert_mode in {"on_expire", "on_ready"} else 2.2,
                    )
                ),
            ),
            inactive_reminder_seconds=max(
                0.0,
                float(
                    buff.get(
                        "inactive_reminder_seconds",
                        8.0 if alert_mode in {"on_expire", "on_ready"} else 0.0,
                    )
                ),
            ),
            presence_confirm_hits=max(
                1,
                int(buff.get("presence_confirm_hits", 2)),
            ),
            max_inactive_alerts=max(
                0,
                int(
                    buff.get(
                        "max_inactive_alerts",
                        5 if alert_mode == "on_ready" else 0,
                    )
                ),
            ),
        )
    return templates, disabled_templates, states


def debug_annotate(
    frame: np.ndarray,
    states: dict[str, TrackerState],
    config: dict[str, Any],
    now: float,
) -> np.ndarray:
    annotated = frame.copy()
    search_boxes = search_boxes_for_config(config)
    for key, state in states.items():
        search_box = search_boxes[key]
        search_left = int(search_box["left"])
        search_top = int(search_box["top"])
        search_width = int(search_box["width"])
        search_height = int(search_box["height"])
        cv2.rectangle(
            annotated,
            (search_left, search_top),
            (search_left + search_width, search_top + search_height),
            (90, 90, 90),
            1,
        )

        detection = state.last_detection
        if detection is None:
            continue
        buff = config["buffs"][key]
        x1 = search_left + detection.x
        y1 = search_top + detection.y
        x2, y2 = x1 + detection.width, y1 + detection.height
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (255, 255, 255), 1)

        if state.alert_mode == "thresholds":
            offset = buff.get("timer_offset")
            size = buff.get("timer_size")
            if offset is not None and size is not None:
                tx1 = search_left + detection.x + int(offset["x"])
                ty1 = search_top + detection.y + int(offset["y"])
                tx2 = tx1 + int(size["width"])
                ty2 = ty1 + int(size["height"])
                cv2.rectangle(
                    annotated, (tx1, ty1), (tx2, ty2), (180, 180, 180), 1
                )
            estimate = state.estimate(now)
            state_text = "?" if estimate is None else f"{estimate:.1f}s"
        elif state.alert_mode == "on_ready":
            state_text = "READY" if state.ready_actionable else "NO ENERGY"
        else:
            state_text = "ACTIVE"

        label = f"{state.name}: {state_text} {detection.score:.2f}"
        cv2.putText(
            annotated,
            label,
            (max(0, x1 - 5), max(16, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return annotated


def status_text(
    states: dict[str, TrackerState], now: float, paused: bool = False
) -> str:
    if paused:
        title = foreground_window_title() or "another window"
        return f"Paused — foreground window is {title!r}"
    parts: list[str] = []
    for state in states.values():
        if state.alert_mode == "on_expire":
            if state.active and state.last_detection is not None:
                parts.append(
                    f"{state.name}: ACTIVE [{state.last_detection.score:.2f}]"
                )
            elif state.ever_active:
                parts.append(f"{state.name}: READY")
            else:
                parts.append(f"{state.name}: --")
            continue

        if state.alert_mode == "on_ready":
            if state.last_detection is not None:
                state_label = "READY" if state.ready_actionable else "NO ENERGY"
                parts.append(
                    f"{state.name}: {state_label} [{state.last_detection.score:.2f}]"
                )
            elif state.active:
                parts.append(f"{state.name}: ACTIVE")
            elif state.ever_active:
                parts.append(f"{state.name}: READY")
            else:
                parts.append(f"{state.name}: --")
            continue

        if state.last_detection is None or not state.active:
            parts.append(f"{state.name}: --")
            continue
        estimate = state.estimate(now)
        timer = "?" if estimate is None else f"{estimate:4.1f}s"
        score = state.last_detection.score
        parts.append(f"{state.name}: {timer} [{score:.2f}]")
    return " | ".join(parts)


def print_status(line: str, previous_width: int) -> int:
    width = max(previous_width, len(line))
    print("\r" + line.ljust(width), end="", flush=True)
    return width


def alert_sound_path(buff: dict[str, Any], urgent: bool = False) -> Path:
    if urgent:
        urgent_sound = str(buff.get("urgent_sound", "")).strip()
        if urgent_sound:
            return BASE_DIR / urgent_sound
    return BASE_DIR / str(buff["sound"])


def run_sound_test(config: dict[str, Any]) -> None:
    worker = SoundWorker()
    try:
        for buff in config["buffs"].values():
            name = str(buff["name"])
            path = alert_sound_path(buff)
            print(f"Playing {name}: {path}")
            worker.play(path, 1, name)
            time.sleep(1.8)
            urgent_sound = str(buff.get("urgent_sound", "")).strip()
            if urgent_sound:
                urgent_path = alert_sound_path(buff, urgent=True)
                print(f"Playing {name} urgent: {urgent_path}")
                worker.play(urgent_path, 1, f"{name} urgent")
                time.sleep(1.8)
    finally:
        worker.close()


def alert_text(state: TrackerState, event: str) -> str:
    if event == "expired":
        if state.alert_mode == "on_ready":
            return f"{state.name} ready - recast now"
        return f"{state.name} ended - recast now"
    if state.alert_mode == "on_ready":
        return f"{state.name} is still ready - recast it"
    return f"{state.name} is still down - recast it"


def run_monitor(args: argparse.Namespace) -> None:
    config = load_config()
    executable = configure_tesseract(str(config.get("tesseract_cmd", "")))
    print(f"Tesseract: {executable}")

    monitors = list_monitors()
    monitor_index = int(config["monitor"])
    if monitor_index <= 0 or monitor_index >= len(monitors):
        raise ConfigurationError("The calibrated monitor no longer exists; recalibrate.")
    monitor = monitors[monitor_index]
    verify_monitor_geometry(config["monitor_geometry"], monitor)

    if args.test_sounds:
        run_sound_test(config)
        return

    templates, disabled_templates, states = build_runtime(config)
    search_boxes = search_boxes_for_config(config)
    sound_worker = SoundWorker()
    scan_interval = max(0.15, float(config.get("scan_interval_seconds", 0.45)))
    ocr_interval = max(0.35, float(config.get("ocr_interval_seconds", 0.72)))
    focus_guard = bool(config.get("only_when_warframe_focused", True)) and not args.ignore_focus
    last_ocr_attempt: dict[str, float] = {key: 0.0 for key in states}
    last_debug_write = 0.0
    status_width = 0

    print(
        "\nMonitoring visible HUD pixels only. Ctrl+C stops the alarm.\n"
        "Splinter Storm warns before expiry; Roar alerts when its ready icon returns."
    )
    try:
        with mss.MSS() as sct:
            while True:
                loop_started = time.monotonic()
                if focus_guard and not warframe_is_foreground():
                    status_width = print_status(
                        status_text(states, loop_started, paused=True), status_width
                    )
                    time.sleep(0.35)
                    continue

                now = time.monotonic()
                for key, state in states.items():
                    buff = config["buffs"][key]
                    frame = grab_bgr(sct, monitor, search_boxes[key])
                    actionable = True
                    if state.alert_mode == "on_ready":
                        detection, actionable = locate_ready_icon(
                            frame,
                            templates[key],
                            disabled_templates.get(key),
                            buff,
                        )
                    else:
                        detection = locate_template(
                            frame,
                            templates[key],
                            str(
                                buff.get(
                                    "match_mode",
                                    "gray_or_edges" if key == "roar" else "gray",
                                )
                            ),
                        )
                        threshold = float(buff.get("match_threshold", 0.68))
                        if detection is not None and detection.score < threshold:
                            detection = None

                    if detection is None:
                        event = state.mark_missing(now)
                        if event is not None:
                            sound_path = BASE_DIR / str(buff["sound"])
                            if event == "expired":
                                default_repeat = (
                                    1 if state.alert_mode == "on_ready" else 2
                                )
                                repeat = max(
                                    1,
                                    int(buff.get("expiry_repeat", default_repeat)),
                                )
                                message = f"{state.name} ended — recast now"
                            else:
                                repeat = max(
                                    1,
                                    int(buff.get("inactive_reminder_repeat", 1)),
                                )
                                message = f"{state.name} is still down — recast it"
                            print(f"\n[ALERT] {message}")
                            sound_worker.play(sound_path, repeat, state.name)
                            state.record_inactive_alert()
                        continue

                    event = state.mark_seen(detection, now, actionable=actionable)
                    if event is not None:
                        sound_path = BASE_DIR / str(buff["sound"])
                        if event == "expired":
                            default_repeat = (
                                1 if state.alert_mode == "on_ready" else 2
                            )
                            repeat = max(
                                1,
                                int(buff.get("expiry_repeat", default_repeat)),
                            )
                        else:
                            repeat = max(
                                1,
                                int(buff.get("inactive_reminder_repeat", 1)),
                            )
                        message = alert_text(state, event)
                        print(f"\n[ALERT] {message}")
                        sound_worker.play(sound_path, repeat, state.name)
                        state.record_inactive_alert()

                    # Roar is deliberately icon-only. We do not OCR its timer;
                    # the bottom-right ready icon tells us when it can be recast.
                    if state.alert_mode in {"on_expire", "on_ready"}:
                        continue

                    if now - last_ocr_attempt[key] < ocr_interval:
                        continue
                    last_ocr_attempt[key] = now

                    timer_offset = buff.get("timer_offset")
                    timer_size = buff.get("timer_size")
                    if timer_offset is None or timer_size is None:
                        continue
                    timer_crop = safe_timer_crop(
                        frame,
                        detection,
                        timer_offset,
                        timer_size,
                    )
                    if timer_crop is None:
                        continue

                    expected = state.estimate(now) if state.active else None
                    result = read_timer(
                        timer_crop,
                        str(buff.get("ocr_mode", OCR_MODES[0])),
                        expected=expected,
                        max_reasonable=float(buff.get("max_reasonable_seconds", 180)),
                    )
                    if result is None:
                        continue

                    crossed, urgent = state.accept_timer(result, now)
                    if crossed is not None:
                        sound_path = alert_sound_path(buff, urgent=urgent)
                        repeat = 1
                        print(
                            f"\n[ALERT] {state.name}: approximately {crossed:g}s remaining"
                            + (" — URGENT" if urgent else "")
                        )
                        label = f"{state.name} urgent" if urgent else state.name
                        sound_worker.play(sound_path, repeat, label)

                    if args.debug:
                        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(DEBUG_DIR / f"{key}_timer.png"), timer_crop)

                status_width = print_status(status_text(states, now), status_width)

                if args.debug and now - last_debug_write >= 1.0:
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    frame = grab_bgr(sct, monitor, full_monitor_box(monitor))
                    annotated = debug_annotate(frame, states, config, now)
                    cv2.imwrite(str(DEBUG_DIR / "latest.png"), annotated)
                    last_debug_write = now

                elapsed = time.monotonic() - loop_started
                if elapsed < scan_interval:
                    time.sleep(scan_interval - elapsed)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sound_worker.close()


def main() -> int:
    args = parse_args()
    try:
        if args.list_monitors:
            print_monitors(list_monitors())
            return 0
        if args.test_sounds:
            try:
                sound_config = load_config()
            except ConfigurationError:
                sound_config = {"buffs": DEFAULT_BUFFS}
            run_sound_test(sound_config)
            return 0
        if args.calibrate:
            run_calibration(args)
            return 0
        run_monitor(args)
        return 0
    except ConfigurationError as exc:
        print(f"\n{APP_NAME}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\n{APP_NAME} failed: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    finally:
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
