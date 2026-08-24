"""Screen understanding: OCR, window/UI recognition, and screen summarization.

This runs on demand (triggered by the SUMMARIZE_SCREEN engine command), never
per-frame: a full capture + OCR pass takes on the order of 100-500ms, far too
slow for the ~30fps gesture loop.

What's real here vs. heuristic, so callers know how much to trust each field:
- Window / active-app / multi-window understanding (list_windows,
  get_active_window) is exact — it comes from the OS via pygetwindow, not
  inferred from pixels.
- OCR / text extraction (extract_text) is real text recognition via
  Tesseract, when installed (OCR_AVAILABLE is False otherwise, and every
  method degrades to returning no text rather than raising).
- locate_text() resolves a target ("the Save button") against live OCR
  output and gets back click coordinates, instead of a fixed
  gesture-to-coordinate mapping. It is not wired into voice/gesture command
  parsing here — that's a separate, not-yet-built NLU layer on top of this.
- Button/icon "detection" (detect_ui_regions) is a geometric heuristic over
  OpenCV contours (size/aspect-ratio filtering), not a trained UI-element
  classifier — it finds plausible rectangular regions, it does not know what
  a button says or what an icon depicts. Treat it as a rough visual outline,
  not ground truth. Same caveat for menu-item detection: a row of short OCR
  words near the top of a window, not a real menu-tree read.
- General object detection (arbitrary real-world objects, not UI chrome) is
  out of scope — that needs a trained detector (e.g. YOLO) this project
  doesn't ship.
"""

import shutil
import time
from pathlib import Path

import cv2
import numpy as np
import pyautogui
import pygetwindow as gw

try:
    import pytesseract
except ImportError:
    pytesseract = None


def _find_tesseract():
    # A freshly winget-installed exe often isn't on PATH until the
    # shell/session restarts, so check the common install locations too.
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = [
        Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


_TESSERACT_PATH = _find_tesseract()
if pytesseract is not None and _TESSERACT_PATH:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_PATH

OCR_AVAILABLE = pytesseract is not None and _TESSERACT_PATH is not None


class ScreenUnderstanding:
    def __init__(self):
        self.last_result = None

    def list_windows(self):
        windows = []
        for w in gw.getAllWindows():
            if not w.title.strip() or w.width <= 0 or w.height <= 0 or not w.visible:
                continue
            windows.append({
                "title": w.title,
                "left": w.left, "top": w.top,
                "width": w.width, "height": w.height,
                "isActive": bool(w.isActive),
            })
        return windows

    def get_active_window(self):
        w = gw.getActiveWindow()
        if w is None or not w.title.strip():
            return None
        return {"title": w.title, "left": w.left, "top": w.top, "width": w.width, "height": w.height}

    def capture(self, region=None):
        shot = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
        return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)

    def extract_text(self, image):
        # Returns [] (not an error) when Tesseract isn't installed, so
        # callers degrade gracefully instead of crashing.
        if not OCR_AVAILABLE:
            return []
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        data = pytesseract.image_to_data(rgb, output_type=pytesseract.Output.DICT)
        words = []
        for i, text in enumerate(data["text"]):
            text = text.strip()
            raw_conf = data["conf"][i]
            conf = float(raw_conf) if str(raw_conf) not in ("-1", "") else -1
            if not text or conf < 40:
                continue
            words.append({
                "text": text,
                "left": data["left"][i], "top": data["top"][i],
                "width": data["width"][i], "height": data["height"][i],
                "confidence": conf,
            })
        return words

    def locate_text(self, query, words=None):
        # `words` can be passed in (e.g. reused from a prior extract_text()
        # call); otherwise this captures and OCRs the screen itself.
        if words is None:
            words = self.extract_text(self.capture())
        query = query.strip().lower()
        matches = []
        for w in words:
            if query in w["text"].lower():
                matches.append({
                    **w,
                    "clickX": w["left"] + w["width"] // 2,
                    "clickY": w["top"] + w["height"] // 2,
                })
        return matches

    def detect_ui_regions(self, image):
        # Not a trained classifier: filters OpenCV contours by size and
        # aspect ratio into "button"-shaped (wide, short, text-sized) vs
        # "icon"-shaped (small, near-square) buckets.
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        buttons, icons = [], []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area = w * h
            if area < 200 or area > 60000:
                continue
            aspect = w / float(h)
            region = {"left": x, "top": y, "width": w, "height": h}
            if 0.7 <= aspect <= 1.4 and 16 <= w <= 64 and 16 <= h <= 64:
                icons.append(region)
            elif 1.5 <= aspect <= 8 and 18 <= h <= 48 and w >= 40:
                buttons.append(region)
        return {"buttons": buttons[:40], "icons": icons[:40]}

    def detect_menu_items(self, words, window):
        if not window:
            return []
        band_top = window["top"]
        band_bottom = window["top"] + 40
        row = [w for w in words
               if band_top <= w["top"] <= band_bottom and len(w["text"]) <= 12]
        row.sort(key=lambda w: w["left"])
        return row

    def summarize(self, max_text_items=12):
        # No TTS engine is wired up here; this returns text for a caller to
        # speak/display.
        started = time.time()
        active = self.get_active_window()
        windows = self.list_windows()
        image = self.capture()
        words = self.extract_text(image)
        regions = self.detect_ui_regions(image)
        menu_items = self.detect_menu_items(words, active)

        lines = []
        if active:
            lines.append(f'Active window: "{active["title"]}" ({active["width"]}x{active["height"]}).')
        else:
            lines.append("No focused window detected.")

        other = [w["title"] for w in windows if not w.get("isActive")]
        if other:
            shown = ", ".join(f'"{t}"' for t in other[:5])
            more = f" and {len(other) - 5} more" if len(other) > 5 else ""
            lines.append(f"{len(windows)} windows open. Others: {shown}{more}.")
        else:
            lines.append(f"{len(windows)} window(s) open.")

        if menu_items:
            lines.append("Menu bar: " + " | ".join(w["text"] for w in menu_items[:10]))

        if words:
            top_text = [w["text"] for w in sorted(words, key=lambda w: -w["confidence"])[:max_text_items]]
            lines.append("On-screen text includes: " + ", ".join(top_text) + ".")
        elif OCR_AVAILABLE:
            lines.append("No readable text detected on screen.")
        else:
            lines.append("OCR unavailable (Tesseract not found) — text content not read.")

        lines.append(f"Detected ~{len(regions['buttons'])} button-shaped region(s) and "
                      f"~{len(regions['icons'])} icon-shaped region(s) (approximate, not classified).")

        result = {
            "summary": " ".join(lines),
            "activeWindow": active,
            "windowCount": len(windows),
            "windows": windows,
            "textItems": words,
            "menuItems": menu_items,
            "uiRegions": regions,
            "ocrAvailable": OCR_AVAILABLE,
            "elapsedMs": round((time.time() - started) * 1000, 1),
        }
        self.last_result = result
        return result


if __name__ == "__main__":
    su = ScreenUnderstanding()
    print(f"OCR available: {OCR_AVAILABLE} (tesseract_cmd: {_TESSERACT_PATH})")
    result = su.summarize()
    print(f"\n--- Screen summary ({result['elapsedMs']}ms) ---")
    print(result["summary"])
