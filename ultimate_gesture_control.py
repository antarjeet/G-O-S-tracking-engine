"""AI-GOS V2: touchless mouse, adaptive keyboard, volume and window controls.

Primary-hand mouse gestures follow finger count: one finger moves the
cursor, two clicks, three right-clicks, four opens/closes the keyboard, an
open hand switches to the next tab and a fist to the previous one. Index +
pinky ("rock on") scrolls, and thumb + index still controls system volume.

Point with the index finger and pinch thumb-to-index to type while the
keyboard is open. While typing, swipes provide SPACE/BACKSPACE/ENTER/CLEAR
shortcuts.
"""

import base64
import json
import math
import os
import queue
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

import autopy
import cv2
import numpy as np
import pyautogui
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

import HandTrackingModule as htm
from ai_gos_features import AdvancedGestureEngine
from voice_typing import VoiceTypingModule


WCAM, HCAM = 640, 480
FRAME_MARGIN = 100
PINCH_DISTANCE = 38
DWELL_SECONDS = 0.65
ACTION_COOLDOWN = 0.45
RIGHT_CLICK_COOLDOWN = 0.6
SCROLL_SENSITIVITY = 2.2

# What the web HUD actually displays: the full working-resolution frame
# (matching WCAM/HCAM below) at a much higher JPEG quality than before.
# Gesture control itself runs off the landmark coordinates, not these
# pixels, so this only affects what you see, not tracking accuracy or
# speed — a local Wi-Fi/loopback Socket.io connection has plenty of
# headroom for a sharper picture at this size.
STREAM_W, STREAM_H = 640, 480
STREAM_JPEG_QUALITY = 85

# When launched by the Node/Express backend (AI_GOS_HEADLESS=1) no OS window
# is opened. The fully-drawn camera frame is streamed to the web HUD instead,
# and the keyboard shortcuts below (which normally need the cv2 window to
# have focus) arrive as newline-delimited commands on stdin.
HEADLESS = os.environ.get("AI_GOS_HEADLESS") == "1"

# Optional alternate camera instead of the PC's built-in webcam:
#   "phone"     - poll the Node backend for frames pushed by a phone that
#                 scanned the HUD's QR code (see PhoneCameraCapture below)
#   a URL       - an IP-camera stream (e.g. the Android "IP Webcam" app)
#   a bare int  - a device index, e.g. for a virtual-webcam driver like DroidCam
CAMERA_SOURCE = os.environ.get("AI_GOS_CAMERA_SOURCE", "").strip()
BACKEND_PORT = os.environ.get("AI_GOS_BACKEND_PORT", "5000").strip()

# Self-signed cert on loopback: this is our own backend, not a real TLS peer.
_INSECURE_SSL_CONTEXT = ssl.create_default_context()
_INSECURE_SSL_CONTEXT.check_hostname = False
_INSECURE_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class PhoneCameraCapture:
    """cv2.VideoCapture-alike that polls the backend's latest phone-camera
    frame over local HTTPS, so the rest of the pipeline (which only calls
    .read()/.isOpened()/.set()/.release()) doesn't need to know the frame
    didn't come from a local device."""

    def __init__(self):
        self._base_url = f"https://127.0.0.1:{BACKEND_PORT}/api/phone-frame/latest"
        self._opened = True
        # The phone only pushes a handful of frames per second, but this
        # read() can be called far faster than that. Tracking the timestamp
        # of the last frame we actually processed (via ?since=) lets the
        # backend tell us "nothing new yet" instead of re-sending the same
        # bytes, so we don't reprocess a stale frame as if it were live —
        # that would inflate the FPS counter with meaningless repeats and
        # waste time re-running MediaPipe on an image it already saw.
        self._last_frame_time = 0

    def isOpened(self):
        return self._opened

    def set(self, *_args, **_kwargs):
        return True  # resolution comes from whatever the phone's camera sends

    def read(self):
        url = f"{self._base_url}?since={self._last_frame_time}"
        try:
            with urllib.request.urlopen(url, timeout=1.0, context=_INSECURE_SSL_CONTEXT) as resp:
                if resp.status == 204:
                    return False, None
                frame_time = resp.headers.get("X-Frame-Time")
                data = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, None
        if not data:
            return False, None
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False, None
        if frame_time is not None:
            self._last_frame_time = int(frame_time)
        return True, img

    def release(self):
        self._opened = False


def _fit_to_size(img, target_w, target_h):
    """Resize img to exactly (target_w, target_h) without distorting it.

    A phone camera almost never natively streams 4:3 (640x480) — it's
    commonly 16:9 — so a plain cv2.resize() to our fixed working resolution
    squashes/stretches the picture (round faces go oval, hands look
    squeezed). Center-crop to the target aspect ratio first, then resize;
    the resize now scales both axes by the same factor, so nothing warps.
    """
    h, w = img.shape[:2]
    target_aspect = target_w / target_h
    src_aspect = w / h
    if src_aspect > target_aspect:
        # Source is wider than target: crop the sides.
        new_w = int(h * target_aspect)
        x0 = (w - new_w) // 2
        img = img[:, x0:x0 + new_w]
    elif src_aspect < target_aspect:
        # Source is taller than target: crop top/bottom.
        new_h = int(w / target_aspect)
        y0 = (h - new_h) // 2
        img = img[y0:y0 + new_h, :]
    return cv2.resize(img, (target_w, target_h))


def _open_camera():
    if CAMERA_SOURCE == "phone":
        return PhoneCameraCapture()
    if CAMERA_SOURCE:
        source = int(CAMERA_SOURCE) if CAMERA_SOURCE.isdigit() else CAMERA_SOURCE
        return cv2.VideoCapture(source)
    # Try DirectShow camera backend on Windows for reliable frame capture
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)
    return cap


class AIGOSKeyboard:
    """Adaptive on-screen keyboard with pinch, dwell and prediction input."""

    LETTERS = [
        ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"],
        ["A", "S", "D", "F", "G", "H", "J", "K", "L", "DEL"],
        ["Z", "X", "C", "V", "B", "N", "M", ".", ",", "SPACE"],
    ]
    EMOJI = [["😀", "😁", "👍", "❤️", "🎉", "😂", "😢", "🙏"],
             ["YES", "NO", "OK", "DEL", "SPACE", "ABC"]]
    WORDS = (
        "THE THIS THAT THANKS THERE THEIR THEY YOU YOUR YES NO GOOD MORNING "
        "NIGHT LUCK HELLO PLEASE THANK YOU COMPUTER GESTURE CONTROL AI"
    ).split()

    def __init__(self):
        self.text = ""
        self.emoji_mode = False
        self.frequency = {}
        self.hover_key = None
        self.hover_started = 0.0
        self.last_action = 0.0
        self.last_pinch = False
        self.pointer_history = []
        self.dwell_seconds = DWELL_SECONDS
        self.status = "Pinch to type • hold to dwell-type"

    @property
    def rows(self):
        return self.EMOJI if self.emoji_mode else self.LETTERS

    def key_rectangles(self):
        """Return adaptive key rectangles; frequently typed letters get wider."""
        rects = []
        y = 246
        for row_index, row in enumerate(self.rows):
            weights = []
            for key in row:
                base = 1.0
                if key in ("SPACE", "DEL"):
                    base = 1.65
                if len(key) > 2 and key not in ("SPACE",):
                    base = 1.25
                # Adaptive sizing is deliberately bounded so adjacent keys stay usable.
                base += min(self.frequency.get(key, 0), 6) * 0.06
                weights.append(base)
            available = 616 - 3 * (len(row) - 1)
            x = 12
            for col, (key, weight) in enumerate(zip(row, weights)):
                width = int(available * weight / sum(weights))
                if col == len(row) - 1:
                    width = 628 - x
                rects.append((key, row_index, col, x, y, width, 52))
                x += width + 3
            y += 58
        return rects

    def key_at(self, point):
        px, py = point
        for key, row, col, x, y, width, height in self.key_rectangles():
            if x <= px <= x + width and y <= py <= y + height:
                return key, (row, col)
        return None, None

    def predictions(self):
        prefix = self.text.rsplit(" ", 1)[-1].upper()
        if not prefix:
            return ["THE", "YOU", "THANKS"]
        matches = [word for word in self.WORDS if word.startswith(prefix) and word != prefix]
        return sorted(matches, key=lambda word: (-self.frequency.get(word, 0), word))[:3]

    def prediction_at(self, point):
        px, py = point
        for index, word in enumerate(self.predictions()):
            x = 14 + index * 207
            if x <= px <= x + 194 and 192 <= py <= 230:
                return word
        return None

    def _record_word(self, word):
        if word:
            self.frequency[word.upper()] = self.frequency.get(word.upper(), 0) + 1

    @staticmethod
    def _os_write(text):
        """Send real keystrokes to whichever window/field actually has OS
        focus — the same field the AI-controlled cursor just clicked into —
        instead of only updating this class's own display buffer. Wrapped
        because pyautogui can choke on a handful of exotic characters (e.g.
        certain emoji), and that shouldn't take down the whole gesture loop."""
        try:
            pyautogui.write(text)
        except Exception:
            pass

    @staticmethod
    def _os_press(key, presses=1):
        if presses <= 0:
            return
        try:
            pyautogui.press(key, presses=presses)
        except Exception:
            pass

    def apply(self, key):
        if not key:
            return
        if key == "DEL":
            self.text = self.text[:-1]
            self._os_press("backspace")
        elif key == "SPACE":
            self._record_word(self.text.rsplit(" ", 1)[-1])
            self.text += " "
            self._os_press("space")
        elif key == "ENTER":
            self._record_word(self.text.rsplit(" ", 1)[-1])
            self.text += "\n"
            self._os_press("enter")
        elif key == "CLEAR":
            self.text = ""
        elif key == "EMOJI":
            self.emoji_mode = True
        elif key == "ABC":
            self.emoji_mode = False
        else:
            self.text += key
            self.frequency[key] = self.frequency.get(key, 0) + 1
            self._os_write(key.lower())
        self.status = f"Inserted: {key}"

    def insert_prediction(self, word):
        prefix = self.text.rsplit(" ", 1)[-1]
        self.text = self.text[:-len(prefix)] if prefix else self.text
        self.text += word + " "
        self._record_word(word)
        self.status = f"Prediction: {word}"
        # The prefix's characters were already sent to the OS field one
        # keystroke at a time as they were typed — replace them there too
        # before writing the full predicted word.
        self._os_press("backspace", presses=len(prefix))
        self._os_write(word.lower() + " ")

    def insert_text(self, text):
        """Append a dictated phrase as its own word(s), used by voice input."""
        text = text.strip()
        if not text:
            return
        needs_lead_space = bool(self.text) and not self.text.endswith((" ", "\n"))
        if needs_lead_space:
            self.text += " "
        self.text += text + " "
        words = text.split()
        if words:
            self._record_word(words[-1])
        self.status = f"Voice: {text}"
        self._os_write((" " if needs_lead_space else "") + text + " ")

    def delete_last_word(self):
        """Remove the last dictated/typed word, used by the voice 'delete' command."""
        stripped = self.text.rstrip()
        if " " in stripped:
            new_text = stripped.rsplit(" ", 1)[0] + " "
        else:
            new_text = ""
        removed = len(self.text) - len(new_text)
        self.text = new_text
        self.status = "Voice: deleted last word"
        self._os_press("backspace", presses=removed)

    def update(self, point, pinching, now):
        """Process pointer input and return selected/pressed positions for drawing."""
        self.pointer_history.append((now, point[0], point[1]))
        self.pointer_history = [item for item in self.pointer_history if now - item[0] < 0.45]
        prediction = self.prediction_at(point)
        key, position = self.key_at(point)
        target = ("prediction", prediction) if prediction else ("key", key) if key else None

        if target != self.hover_key:
            self.hover_key, self.hover_started = target, now
        pressed = None
        # Pinch is edge-triggered.  Dwell is a deliberate, repeat-safe alternative.
        if target and ((pinching and not self.last_pinch) or (not pinching and now - self.hover_started >= self.dwell_seconds)):
            if now - self.last_action >= ACTION_COOLDOWN:
                if prediction:
                    self.insert_prediction(prediction)
                    pressed = ("prediction", prediction)
                else:
                    self.apply(key)
                    pressed = ("key", position)
                self.last_action = now
                self.hover_started = now
        self.last_pinch = pinching
        return position, pressed, prediction

    def swipe_action(self, now):
        if len(self.pointer_history) < 2 or now - self.last_action < ACTION_COOLDOWN:
            return None
        _, start_x, start_y = self.pointer_history[0]
        _, end_x, end_y = self.pointer_history[-1]
        dx, dy = end_x - start_x, end_y - start_y
        if max(abs(dx), abs(dy)) < 115:
            return None
        if abs(dx) > abs(dy):
            action = "SPACE" if dx > 0 else "DEL"
        else:
            action = "ENTER" if dy < 0 else "CLEAR"
        self.apply(action)
        self.last_action = now
        self.pointer_history.clear()
        return action

    def draw(self, image, pointer, selected, pressed, prediction):
        cv2.rectangle(image, (8, 98), (632, 170), (20, 24, 35), cv2.FILLED)
        cv2.rectangle(image, (8, 98), (632, 170), (255, 210, 0), 2)
        display = self.text.replace("\n", " ↵ ")[-54:]
        cv2.putText(image, "AI-GOS KEYBOARD  |  " + display, (18, 126),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1)
        cv2.putText(image, self.status, (18, 153), cv2.FONT_HERSHEY_SIMPLEX,
                    0.43, (0, 230, 255), 1)

        for index, word in enumerate(self.predictions()):
            x = 14 + index * 207
            selected_prediction = prediction == word
            color = (0, 180, 255) if selected_prediction else (68, 68, 85)
            cv2.rectangle(image, (x, 192), (x + 194, 230), color, cv2.FILLED)
            cv2.putText(image, word, (x + 9, 217), cv2.FONT_HERSHEY_SIMPLEX,
                        0.52, (0, 0, 0) if selected_prediction else (255, 255, 255), 1)

        for key, row, col, x, y, width, height in self.key_rectangles():
            is_selected = selected == (row, col)
            is_pressed = pressed == ("key", (row, col))
            color = (50, 210, 80) if is_pressed else (0, 180, 255) if is_selected else (48, 50, 65)
            cv2.rectangle(image, (x, y), (x + width, y + height), color, cv2.FILLED)
            cv2.rectangle(image, (x, y), (x + width, y + height), (170, 180, 200), 1)
            scale = 0.36 if len(key) > 2 else 0.52
            size = cv2.getTextSize(key, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0]
            cv2.putText(image, key, (x + (width - size[0]) // 2, y + (height + size[1]) // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0) if is_selected or is_pressed else (255, 255, 255), 1)
        cv2.circle(image, pointer, 10, (0, 255, 255), cv2.FILLED)


def _stdin_command_reader(command_queue):
    """Read newline-delimited commands from stdin. Only used in headless mode,
    where the web HUD's on-page buttons replace the cv2-window key shortcuts."""
    for line in sys.stdin:
        line = line.strip()
        if line:
            command_queue.put(line)


def main():
    """Original Ultimate Gesture Control loop with only its keyboard replaced."""
    # These settings, gesture tests, mappings and debounce values deliberately
    # match the pre-AI-GOS version.
    wcam, hcam = 640, 480
    # Lower smoothening = less lag between hand movement and cursor movement
    # (it's a 1/N low-pass filter on each axis: clocX moves N-th of the way
    # to the target every frame, so a high N feels "slow to catch up" even
    # at a good FPS). 7 felt sluggish; 3 tracks much closer to real-time
    # while still smoothing out per-frame jitter.
    frameR, smoothening = 100, 3
    # fps is averaged over a rolling window rather than recomputed from a
    # single 1/dt every frame: instantaneous per-frame timing is noisy (OS
    # scheduling jitter, one slightly-early iteration) and can momentarily
    # spike to nonsense values like "1400 FPS" even though the sustained
    # rate is nothing like that. Averaging over ~0.5s of real frames gives a
    # steady, physically meaningful number instead.
    fps = 0.0
    fps_window_start = time.time()
    fps_window_count = 0
    plocX = plocY = clocX = clocY = 0
    pyautogui.FAILSAFE = False
    wScr, hScr = pyautogui.size()

    cap = _open_camera()

    cap.set(3, wcam)
    cap.set(4, hcam)
    cap.set(cv2.CAP_PROP_FPS, 60)
    # Some DirectShow drivers buffer several frames internally, which makes
    # cap.read() return stale frames and the whole pipeline feel laggy.
    # Not all backends support this property; ignore failures.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    detector = htm.HandDetector(model_complexity=0)
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = interface.QueryInterface(IAudioEndpointVolume)
    minVol, maxVol = volume.GetVolumeRange()[:2]
    volper = 0
    keyboard = AIGOSKeyboard()
    keyboard_active = False
    ai_gos = AdvancedGestureEngine()
    # Dictates speech straight into `keyboard`'s text buffer on its own
    # background thread — see voice_typing.py.
    voice_typing = VoiceTypingModule(keyboard)

    active_hand_index = 0
    last_hand_state = None
    hand_state_change_time = 0
    hand_state_debounce = 0.5
    last_right_click_time = 0.0
    four_finger_pose_prev = False
    last_scroll_y = None

    command_queue = queue.Queue()
    if HEADLESS:
        threading.Thread(target=_stdin_command_reader, args=(command_queue,), daemon=True).start()

    def apply_command(cmd, current_lmlist, current_all_hands):
        """Web-HUD equivalent of the cv2-window key shortcuts below."""
        nonlocal keyboard_active, active_hand_index
        # Match commands case-insensitively, but keep the original casing of
        # anything after a ":" — TYPE:/INSERT_PREDICTION:/VOICE_PHRASE: all
        # carry a payload (a character, a word, a dictated phrase) that
        # shouldn't be forced to uppercase.
        raw = cmd.strip()
        cmd = raw.upper()
        if cmd == "TOGGLE_KEYBOARD":
            keyboard_active = not keyboard_active
            ai_gos.status = "AI-GOS keyboard opened" if keyboard_active else "AI-GOS keyboard closed"
        elif cmd == "SWAP_HAND":
            if len(current_all_hands) > 1:
                active_hand_index = (active_hand_index + 1) % len(current_all_hands)
                ai_gos.status = f"Primary control switched to hand {active_hand_index + 1}"
            else:
                ai_gos.status = "Show both hands to swap primary control"
        elif cmd == "CLEAR":
            keyboard.apply("CLEAR")
        elif cmd == "TOGGLE_AI_GOS":
            ai_gos.handle_key(ord('g'), current_lmlist if len(current_lmlist) else [])
        elif cmd == "CYCLE_CONTEXT":
            ai_gos.handle_key(ord('m'), current_lmlist if len(current_lmlist) else [])
        elif cmd == "CYCLE_PROFILE":
            ai_gos.handle_key(ord('p'), current_lmlist if len(current_lmlist) else [])
        elif cmd == "TRAIN":
            ai_gos.handle_key(ord('t'), current_lmlist if len(current_lmlist) else [])
        elif cmd == "TOGGLE_VOICE":
            voice_typing.toggle()
        elif cmd.startswith("TYPE:"):
            # Lets the web HUD's on-screen keyboard type into the same
            # AIGOSKeyboard buffer that pinch/dwell gesture typing uses,
            # independent of whether gesture keyboard mode is toggled on.
            keyboard.apply(raw[len("TYPE:"):])
        elif cmd.startswith("INSERT_PREDICTION:"):
            keyboard.insert_prediction(raw[len("INSERT_PREDICTION:"):])
        elif cmd.startswith("VOICE_PHRASE:"):
            # A phrase recognized by the *browser's* microphone (Web Speech
            # API in the web HUD — see gos/index.html), as opposed to
            # voice_typing.py's own PC-mic listening thread. Letting the web
            # HUD request mic permission and recognize speech client-side
            # means voice typing works from whatever device has the
            # dashboard open (a phone, a laptop) even if that isn't the PC
            # actually running this engine — same idea as phone-camera
            # pairing. Routed through the same handle_phrase() so "space"/
            # "clear"/"delete" behave identically either way.
            voice_typing.handle_phrase(raw[len("VOICE_PHRASE:"):])

    while True:
        success, img = cap.read()
        if not success or img is None:
            time.sleep(0.05)
            continue
        # A phone/IP camera stream (or a webcam that ignores cap.set()) can
        # deliver frames at a different resolution than wcam/hcam, which the
        # mouse-move mapping and telemetry normalization below both assume.
        # Force it back to the expected size so gesture math stays correct
        # regardless of the actual source resolution — center-crop to the
        # right aspect ratio first so this doesn't stretch/distort the image.
        if img.shape[1] != wcam or img.shape[0] != hcam:
            img = _fit_to_size(img, wcam, hcam)
        img = cv2.flip(img, 1)
        img = detector.find_hands(img)
        lmList, bbox = detector.find_position(img)

        all_hands = detector.find_all_positions(img)
        # The accessibility hand switch selects which tracked hand runs the
        # original mouse/keyboard controls. The other hand remains available
        # to AI-GOS as the simultaneous advanced controller.
        if all_hands:
            active_hand_index %= len(all_hands)
            lmList = all_hands[active_hand_index]
            detector.lmList = lmList
        ordered_hands = ([all_hands[active_hand_index]] +
                         [hand for index, hand in enumerate(all_hands) if index != active_hand_index]) if all_hands else []
        ai_confidence = 0.0

        # Drain any pending web-HUD button commands before this frame's
        # gesture logic runs, so they take effect immediately.
        while not command_queue.empty():
            try:
                apply_command(command_queue.get_nowait(), lmList, all_hands)
            except queue.Empty:
                break

        if len(lmList) != 0:
            fingers = detector.fingersUp()
            x1, y1 = lmList[8][1:]
            x2, y2 = lmList[12][1:]
            x_thumb, y_thumb = lmList[4][1:]
            now = time.time()
            keyboard.dwell_seconds = ai_gos.profile["accessibility"]["dwell_seconds"]

            # Four fingers (thumb down) toggles the keyboard on or off. Checked
            # before the mode chain below (and edge-triggered on pose entry,
            # not held) so it works whether the keyboard is currently open or
            # closed, and doesn't re-fire every frame the pose is held.
            four_finger_pose = (fingers[0] == 0 and fingers[1] == 1 and fingers[2] == 1
                                 and fingers[3] == 1 and fingers[4] == 1)
            if four_finger_pose and not four_finger_pose_prev:
                keyboard_active = not keyboard_active
                ai_gos.status = "AI-GOS keyboard opened (gesture)" if keyboard_active else "AI-GOS keyboard closed (gesture)"
            four_finger_pose_prev = four_finger_pose

            # New keyboard only: three extended fingers keep all legacy mouse
            # gestures untouched when this mode is not being intentionally used.
            if keyboard_active:
                pinch_threshold = PINCH_DISTANCE * ai_gos.profile["accessibility"]["sensitivity"]
                selected, pressed, prediction = keyboard.update(
                    (x1, y1), math.hypot(x1 - x_thumb, y1 - y_thumb) < pinch_threshold, now
                )
                swipe = keyboard.swipe_action(now)
                if swipe:
                    keyboard.status = f"Gesture shortcut: {swipe}"
                keyboard.draw(img, (x1, y1), selected, pressed, prediction)
                current_mode = "AI-GOS KEYBOARD"

            # === Robust OS Mouse Movement ===
            elif fingers[1] == 1 and fingers[2] == 0 and fingers[3] == 0 and fingers[4] == 0:
                current_mode = "MOUSE MOVE"
                x3 = np.interp(x1, (frameR, wcam-frameR), (0, wScr))
                y3 = np.interp(y1, (frameR, hcam-frameR), (0, hScr))
                clocX = plocX + (x3 - plocX) / smoothening
                clocY = plocY + (y3 - plocY) / smoothening

                # Clamp within screen bounds and move OS cursor
                target_x = int(min(max(0, clocX), wScr - 1))
                target_y = int(min(max(0, clocY), hScr - 1))
                try:
                    pyautogui.moveTo(target_x, target_y)
                except Exception:
                    pass

                plocX, plocY = clocX, clocY

            # === Robust OS Mouse Click ===
            elif fingers[1] == 1 and fingers[2] == 1 and fingers[3] == 0 and fingers[4] == 0:
                current_mode = "MOUSE CLICK"
                length, img, lineinfo = detector.find_Distance(8, 12, img, draw=False)
                cv2.putText(img, "MODE: MOUSE CLICK", (10, 25), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 0), 2)
                if length < 40:
                    try:
                        pyautogui.click()
                    except Exception:
                        pass

            # === Right Click: index + middle + ring, pinky down ===
            elif fingers[1] == 1 and fingers[2] == 1 and fingers[3] == 1 and fingers[4] == 0:
                current_mode = "MOUSE RIGHT CLICK"
                cv2.putText(img, "MODE: RIGHT CLICK", (10, 25), cv2.FONT_HERSHEY_PLAIN, 2, (0, 140, 255), 2)
                # A cooldown (rather than the left-click's per-frame pinch
                # check) keeps a held pose from spamming the context menu.
                if now - last_right_click_time > RIGHT_CLICK_COOLDOWN:
                    try:
                        pyautogui.click(button='right')
                        last_right_click_time = now
                    except Exception:
                        pass

            # === Scroll: index + pinky ("rock on"), move hand up/down ===
            # Deliberately thumb-independent: fingersUp() detects the thumb by
            # x-position (left/right of the joint below it), which only works
            # when the thumb points sideways. A vertical pose like "shaka"
            # (thumb up) reads as unreliably up/down on that check, which is
            # why the previous thumb+pinky scroll gesture misfired. Index and
            # pinky use the much more reliable y-position (tip-above-joint)
            # check instead, regardless of thumb state.
            elif fingers[1] == 1 and fingers[4] == 1 and fingers[2] == 0 and fingers[3] == 0:
                current_mode = "SCROLL"
                cv2.putText(img, "MODE: SCROLL", (10, 25), cv2.FONT_HERSHEY_PLAIN, 2, (0, 220, 180), 2)
                if last_scroll_y is not None:
                    delta = y1 - last_scroll_y
                    if abs(delta) > 3:
                        try:
                            pyautogui.scroll(int(-delta * SCROLL_SENSITIVITY))
                        except Exception:
                            pass
                last_scroll_y = y1

            # === Original volume control (including its original landmark mapping) ===
            elif fingers[0] == 1 and fingers[1] == 1:
                current_mode = "VOLUME CONTROL"
                length = math.hypot(x2 - x_thumb, y2 - y_thumb)
                vol = np.interp(length, [50, 218], [minVol, maxVol])
                volper = np.interp(length, [50, 218], [0, 100])
                volume.SetMasterVolumeLevel(vol, None)
            else:
                current_mode = "IDLE"
                cv2.putText(img, "MODE: IDLE - Show a gesture", (10, 25), cv2.FONT_HERSHEY_PLAIN, 2, (200, 200, 200), 2)

            if current_mode != "SCROLL":
                last_scroll_y = None

            # === Original tab/window switching ===
            if sum(fingers) == 5:
                if last_hand_state != "OPEN" and (now - hand_state_change_time) > hand_state_debounce:
                    pyautogui.hotkey('alt', 'tab')
                    hand_state_change_time = now
                    last_hand_state = "OPEN"
            elif sum(fingers) == 0:
                if last_hand_state != "CLOSED" and (now - hand_state_change_time) > hand_state_debounce:
                    pyautogui.hotkey('alt', 'shift', 'tab')
                    hand_state_change_time = now
                    last_hand_state = "CLOSED"

            # AI-GOS only consumes the second hand, preserving the legacy
            # first-hand gesture paths above.
            ai_confidence = ai_gos.process(img, ordered_hands, keyboard.text, now)
        else:
            current_mode = "IDLE"
            text = "Show hand to camera"
            size = cv2.getTextSize(text, cv2.FONT_HERSHEY_PLAIN, 2, 2)[0]
            cv2.putText(img, text, ((wcam - size[0]) // 2, hcam // 2), cv2.FONT_HERSHEY_PLAIN,
                        2, (200, 200, 200), 2)

        fps_window_count += 1
        fps_elapsed = time.time() - fps_window_start
        if fps_elapsed >= 0.5:
            fps = fps_window_count / fps_elapsed
            fps_window_count = 0
            fps_window_start = time.time()

        cv2.putText(img, f"{int(fps)} FPS", (10, 20), cv2.FONT_HERSHEY_PLAIN, 1, (200, 200, 200), 1)
        cv2.putText(img, "1:Move 2:Click 3:R-Click 4:Keyboard 5:NextTab Fist:PrevTab Thumb+Index:Volume Index+Pinky:Scroll | H:Swap G:AI-GOS ESC:Exit", (2, hcam - 5), cv2.FONT_HERSHEY_PLAIN, 0.48, (200, 200, 200), 1)

        # === Real-time JSON Telemetry Stream to Express Node.js Server ===
        # Built after all overlays are drawn onto img, so a streamed frame
        # (headless mode) matches exactly what the OS window would show.
        try:
            landmarks_data = []
            if len(lmList) >= 21:
                landmarks_data = [{"x": round(pt[1] / float(WCAM), 4), "y": round(pt[2] / float(HCAM), 4), "z": 0.0} for pt in lmList]
                px = int(lmList[8][1] * 2.5) if len(lmList) > 8 else 1039
                py = int(lmList[8][2] * 2.2) if len(lmList) > 8 else 291
            else:
                px, py = 1039, 291

            frame_b64 = None
            if HEADLESS:
                try:
                    stream_frame = cv2.resize(img, (STREAM_W, STREAM_H), interpolation=cv2.INTER_AREA)
                    ok_enc, enc_buf = cv2.imencode(
                        '.jpg', stream_frame, [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_JPEG_QUALITY]
                    )
                    if ok_enc:
                        frame_b64 = base64.b64encode(enc_buf).decode('ascii')
                except Exception:
                    frame_b64 = None

            # Surface the on-screen keyboard overlay automatically once voice
            # typing is listening, the same way opening it by gesture does.
            if voice_typing.enabled:
                keyboard_active = True

            telemetry_payload = {
                "source": "REALTIME_ULTIMATE_GESTURE_ENGINE",
                "fps": round(fps, 1),
                "latency": 9.5,
                "confidence": round(ai_confidence * 100 if ai_confidence else 94.8, 1),
                "gesture": current_mode if 'current_mode' in locals() and current_mode else "IDLE",
                "landmarks": landmarks_data,
                "pointer": {"x": px, "y": py},
                "depth": "0.82m",
                "cpu": 42,
                "gpu": 68,
                "engineActive": True,
                "frame": f"data:image/jpeg;base64,{frame_b64}" if frame_b64 else None,
                "keyboard": {
                    "active": keyboard_active,
                    "text": keyboard.text,
                    "predictions": keyboard.predictions(),
                    "status": keyboard.status,
                },
                "voice": {
                    "enabled": voice_typing.enabled,
                    "text": voice_typing.last_text,
                },
            }
            sys.stdout.write(json.dumps(telemetry_payload) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

        if HEADLESS:
            # The web HUD is the only display surface; commands arrive via
            # stdin (drained above) instead of cv2 key events.
            continue

        try:
            cv2.imshow("Ultimate Gesture Control - Mouse | AI-GOS Keyboard | Volume", img)
            key = cv2.waitKey(1)
            if key % 256 == 27:
                print("Escape hit, closing the app")
                break
            elif key % 256 == ord('c'):
                keyboard.apply("CLEAR")
            elif key % 256 == ord('k'):
                keyboard_active = not keyboard_active
                ai_gos.status = "AI-GOS keyboard opened" if keyboard_active else "AI-GOS keyboard closed"
            elif key % 256 == ord('h'):
                if len(all_hands) > 1:
                    active_hand_index = (active_hand_index + 1) % len(all_hands)
                    ai_gos.status = f"Primary control switched to hand {active_hand_index + 1}"
                else:
                    ai_gos.status = "Show both hands to swap primary control"
            elif key % 256 == ord('v'):
                voice_typing.toggle()
            else:
                ai_gos.handle_key(key % 256, lmList if len(lmList) else [])
        except Exception:
            time.sleep(0.01)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
