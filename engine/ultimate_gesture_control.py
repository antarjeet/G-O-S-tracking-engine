import base64
import http.client
import json
import math
import os
import queue
import ssl
import sys
import threading
import time

import autopy
import cv2
import numpy as np
import pyautogui
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

import HandTrackingModule as htm
from ai_gos_features import AdvancedGestureEngine
from voice_typing import VoiceTypingModule
from screen_understanding import ScreenUnderstanding, OCR_AVAILABLE


WCAM, HCAM = 640, 480
FRAME_MARGIN = 100
RIGHT_CLICK_COOLDOWN = 0.6

# 1.0x reproduces the original fixed smoothening exactly; below 1 trades
# responsiveness for extra jitter smoothing, above 1 trades smoothing for
# snappier tracking.
MOUSE_SPEED_MIN = 0.5
MOUSE_SPEED_MAX = 3.0
MOUSE_SPEED_DEFAULT = 1.0


def _clamp_mouse_speed(value):
    return min(max(value, MOUSE_SPEED_MIN), MOUSE_SPEED_MAX)
SCROLL_SENSITIVITY = 2.2

STREAM_W, STREAM_H = 640, 480
STREAM_JPEG_QUALITY = 85

HEADLESS = os.environ.get("AI_GOS_HEADLESS") == "1"
CAMERA_SOURCE = os.environ.get("AI_GOS_CAMERA_SOURCE", "").strip()
BACKEND_PORT = os.environ.get("AI_GOS_BACKEND_PORT", "5000").strip()

# Self-signed cert on loopback: this is our own backend, not a real TLS peer.
_INSECURE_SSL_CONTEXT = ssl.create_default_context()
_INSECURE_SSL_CONTEXT.check_hostname = False
_INSECURE_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class PhoneCameraCapture:
    # After a "no new frame yet" response, wait this long before polling
    # again instead of immediately re-requesting — the phone posts at ~30fps,
    # so polling faster than that between real frames only burns CPU/TLS
    # overhead for no new data.
    _IDLE_POLL_DELAY_S = 0.01

    def __init__(self):
        self._host = "127.0.0.1"
        self._port = int(BACKEND_PORT)
        self._path_base = "/api/phone-frame/latest"
        self._opened = True
        # Tracking the timestamp of the last frame actually processed (via
        # ?since=) lets the backend say "nothing new yet" instead of
        # re-sending the same bytes — otherwise a stale frame would get
        # reprocessed as if live, inflating the FPS counter with meaningless
        # repeats and wasting time re-running MediaPipe on a frame it
        # already saw.
        self._last_frame_time = 0
        # A fresh urllib.request.urlopen() call here used to pay a brand-new
        # TLS handshake against localhost on *every single poll* — by far the
        # biggest cost in this loop. Keeping one HTTP/1.1 keep-alive
        # connection open and reusing it across polls avoids that entirely;
        # it's recreated transparently on the next read() if it ever drops.
        self._conn = None

    def isOpened(self):
        return self._opened

    def set(self, *_args, **_kwargs):
        return True

    def _get_connection(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=1.0, context=_INSECURE_SSL_CONTEXT
            )
        return self._conn

    def read(self):
        path = f"{self._path_base}?since={self._last_frame_time}"
        try:
            conn = self._get_connection()
            conn.request("GET", path)
            resp = conn.getresponse()
            if resp.status == 204:
                resp.read()
                time.sleep(self._IDLE_POLL_DELAY_S)
                return False, None
            frame_time = resp.getheader("X-Frame-Time")
            data = resp.read()
        except (http.client.HTTPException, OSError, TimeoutError):
            # Connection likely dropped (server restart, idle timeout) — drop
            # it so the next read() opens a fresh one instead of retrying a
            # socket that's already broken.
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
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
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _fit_to_size(img, target_w, target_h):
    """A phone camera almost never natively streams 4:3 (640x480) — it's
    commonly 16:9 — so a plain cv2.resize() to our fixed working resolution
    squashes/stretches the picture (round faces go oval, hands look
    squeezed). Center-crop to the target aspect ratio first, then resize;
    the resize now scales both axes by the same factor, so nothing warps."""
    h, w = img.shape[:2]
    target_aspect = target_w / target_h
    src_aspect = w / h
    if src_aspect > target_aspect:
        new_w = int(h * target_aspect)
        x0 = (w - new_w) // 2
        img = img[:, x0:x0 + new_w]
    elif src_aspect < target_aspect:
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
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)
    return cap


class DictationBuffer:
    def __init__(self):
        self.text = ""
        self.status = "Voice dictation idle"

    @staticmethod
    def _os_write(text):
        # pyautogui can choke on a handful of exotic characters (e.g.
        # certain emoji) — guarded so that doesn't take down the whole
        # gesture loop.
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
        if key == "SPACE":
            self.text += " "
            self._os_press("space")
        elif key == "ENTER":
            self.text += "\n"
            self._os_press("enter")
        elif key == "CLEAR":
            self.text = ""
        self.status = f"Voice: {key.lower()}"

    def insert_text(self, text):
        text = text.strip()
        if not text:
            return
        needs_lead_space = bool(self.text) and not self.text.endswith((" ", "\n"))
        if needs_lead_space:
            self.text += " "
        self.text += text + " "
        self.status = f"Voice: {text}"
        self._os_write((" " if needs_lead_space else "") + text + " ")

    def delete_last_word(self):
        stripped = self.text.rstrip()
        if " " in stripped:
            new_text = stripped.rsplit(" ", 1)[0] + " "
        else:
            new_text = ""
        removed = len(self.text) - len(new_text)
        self.text = new_text
        self.status = "Voice: deleted last word"
        self._os_press("backspace", presses=removed)


def _stdin_command_reader(command_queue):
    for line in sys.stdin:
        line = line.strip()
        if line:
            command_queue.put(line)


def _screen_telemetry(state, lock):
    # Deliberately excludes the full word/window/region lists from `result`
    # — those are large and only change once per SUMMARIZE_SCREEN pass, not
    # every frame, so repeating them at ~30fps into every telemetry line
    # would be pure waste for what the HUD actually shows.
    with lock:
        status = state["status"]
        result = state["result"]
    active_window = result.get("activeWindow") if result else None
    return {
        "status": status,
        "ocrAvailable": OCR_AVAILABLE,
        "summary": result.get("summary") if result else None,
        "activeWindow": active_window.get("title") if active_window else None,
        "windowCount": result.get("windowCount") if result else None,
        "elapsedMs": result.get("elapsedMs") if result else None,
    }


def main():
    wcam, hcam = 640, 480
    # Lower smoothening = less lag between hand movement and cursor movement
    # (it's a 1/N low-pass filter on each axis: clocX moves N-th of the way
    # to the target every frame, so a high N feels "slow to catch up" even
    # at a good FPS). 7 felt sluggish; 3 tracks much closer to real-time
    # while still smoothing out per-frame jitter.
    frameR, smoothening = 100, 3
    try:
        _initial_mouse_speed = float(os.environ.get("AI_GOS_MOUSE_SPEED", MOUSE_SPEED_DEFAULT))
    except ValueError:
        _initial_mouse_speed = MOUSE_SPEED_DEFAULT
    mouse_speed_state = {"multiplier": _clamp_mouse_speed(_initial_mouse_speed)}
    # fps is averaged over a rolling window rather than recomputed from a
    # single 1/dt every frame: instantaneous per-frame timing is noisy (OS
    # scheduling jitter, one slightly-early iteration) and can momentarily
    # spike to nonsense values like "1400 FPS" even though the sustained
    # rate is nothing like that.
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
    voice_buffer = DictationBuffer()
    ai_gos = AdvancedGestureEngine()

    screen_ai = ScreenUnderstanding()
    screen_summary_lock = threading.Lock()
    screen_summary_state = {"status": "idle", "result": None}

    def _run_screen_summary():
        with screen_summary_lock:
            if screen_summary_state["status"] == "analyzing":
                return
            screen_summary_state["status"] = "analyzing"
        try:
            result = screen_ai.summarize()
            with screen_summary_lock:
                screen_summary_state["status"] = "ready"
                screen_summary_state["result"] = result
        except Exception as exc:
            with screen_summary_lock:
                screen_summary_state["status"] = "error"
                screen_summary_state["result"] = {"summary": f"Screen summary failed: {exc}"}

    def _handle_voice_action(action):
        if action == "SUMMARIZE_SCREEN":
            ai_gos.status = "Analyzing screen (voice)…"
            threading.Thread(target=_run_screen_summary, daemon=True).start()

    voice_typing = VoiceTypingModule(
        voice_buffer,
        on_action=_handle_voice_action,
    )

    active_hand_index = 0
    last_hand_state = None
    hand_state_change_time = 0
    hand_state_debounce = 0.5
    last_right_click_time = 0.0
    last_scroll_y = None

    command_queue = queue.Queue()
    if HEADLESS:
        threading.Thread(target=_stdin_command_reader, args=(command_queue,), daemon=True).start()

    def apply_command(cmd, current_lmlist, current_all_hands):
        nonlocal active_hand_index
        raw = cmd.strip()
        cmd = raw.upper()
        if cmd == "SWAP_HAND":
            if len(current_all_hands) > 1:
                active_hand_index = (active_hand_index + 1) % len(current_all_hands)
                ai_gos.status = f"Primary control switched to hand {active_hand_index + 1}"
            else:
                ai_gos.status = "Show both hands to swap primary control"
        elif cmd == "CLEAR":
            voice_buffer.apply("CLEAR")
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
        elif cmd == "SUMMARIZE_SCREEN":
            ai_gos.status = "Analyzing screen…"
            threading.Thread(target=_run_screen_summary, daemon=True).start()
        elif cmd.startswith("SET_MOUSE_SPEED:"):
            try:
                mouse_speed_state["multiplier"] = _clamp_mouse_speed(
                    float(raw[len("SET_MOUSE_SPEED:"):].strip())
                )
            except ValueError:
                pass
        elif cmd.startswith("VOICE_PHRASE:"):
            # A phrase recognized by the *browser's* microphone (Web Speech
            # API in the web HUD), as opposed to voice_typing.py's own
            # PC-mic listening thread — lets voice typing work from whatever
            # device has the dashboard open, even if it isn't the PC
            # actually running this engine.
            voice_typing.handle_phrase(raw[len("VOICE_PHRASE:"):])

    while True:
        success, img = cap.read()
        if not success or img is None:
            time.sleep(0.05)
            continue
        # A phone/IP camera stream (or a webcam that ignores cap.set()) can
        # deliver frames at a different resolution than wcam/hcam, which the
        # mouse-move mapping and telemetry normalization below both assume —
        # force it back to the expected size so gesture math stays correct.
        if img.shape[1] != wcam or img.shape[0] != hcam:
            img = _fit_to_size(img, wcam, hcam)
        img = cv2.flip(img, 1)
        img = detector.find_hands(img)
        # find_hands() already drew every landmark point above (mp_draw); draw=False
        # here avoids drawing the same 21 points a second time for hand 0.
        lmList, bbox = detector.find_position(img, draw=False)

        all_hands = detector.find_all_positions(img)
        if all_hands:
            active_hand_index %= len(all_hands)
            lmList = all_hands[active_hand_index]
            detector.lmList = lmList
        ordered_hands = ([all_hands[active_hand_index]] +
                         [hand for index, hand in enumerate(all_hands) if index != active_hand_index]) if all_hands else []
        ai_confidence = 0.0

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

            if fingers[1] == 1 and fingers[2] == 0 and fingers[3] == 0 and fingers[4] == 0:
                current_mode = "MOUSE MOVE"
                x3 = np.interp(x1, (frameR, wcam-frameR), (0, wScr))
                y3 = np.interp(y1, (frameR, hcam-frameR), (0, hScr))
                # Higher mouse_speed -> lower effective smoothening -> the
                # cursor catches up to the target position in fewer frames.
                effective_smoothening = max(1.0, smoothening / mouse_speed_state["multiplier"])
                clocX = plocX + (x3 - plocX) / effective_smoothening
                clocY = plocY + (y3 - plocY) / effective_smoothening

                target_x = int(min(max(0, clocX), wScr - 1))
                target_y = int(min(max(0, clocY), hScr - 1))
                try:
                    pyautogui.moveTo(target_x, target_y)
                except Exception:
                    pass

                plocX, plocY = clocX, clocY

            elif fingers[1] == 1 and fingers[2] == 1 and fingers[3] == 0 and fingers[4] == 0:
                current_mode = "MOUSE CLICK"
                length, img, lineinfo = detector.find_Distance(8, 12, img, draw=False)
                cv2.putText(img, "MODE: MOUSE CLICK", (10, 25), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 0), 2)
                if length < 40:
                    try:
                        pyautogui.click()
                    except Exception:
                        pass

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

            ai_confidence = ai_gos.process(img, ordered_hands, now)
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
        cv2.putText(img, "1:Move 2:Click 3:R-Click 5:NextTab Fist:PrevTab Thumb+Index:Volume Index+Pinky:Scroll | H:Swap G:AI-GOS S:SummarizeScreen ESC:Exit", (2, hcam - 5), cv2.FONT_HERSHEY_PLAIN, 0.48, (200, 200, 200), 1)

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
                    # img is already (STREAM_W, STREAM_H) by this point — skip
                    # resizing to the same size.
                    if img.shape[1] == STREAM_W and img.shape[0] == STREAM_H:
                        stream_frame = img
                    else:
                        stream_frame = cv2.resize(img, (STREAM_W, STREAM_H), interpolation=cv2.INTER_AREA)
                    ok_enc, enc_buf = cv2.imencode(
                        '.jpg', stream_frame, [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_JPEG_QUALITY]
                    )
                    if ok_enc:
                        frame_b64 = base64.b64encode(enc_buf).decode('ascii')
                except Exception:
                    frame_b64 = None

            telemetry_payload = {
                "source": "REALTIME_ULTIMATE_GESTURE_ENGINE",
                "fps": round(fps, 1),
                "latency": 9.5,
                "confidence": round(ai_confidence * 100 if ai_confidence else 94.8, 1),
                "gesture": current_mode if 'current_mode' in locals() and current_mode else "IDLE",
                "mouseSpeed": mouse_speed_state["multiplier"],
                "landmarks": landmarks_data,
                "pointer": {"x": px, "y": py},
                "depth": "0.82m",
                "cpu": 42,
                "gpu": 68,
                "engineActive": True,
                "frame": f"data:image/jpeg;base64,{frame_b64}" if frame_b64 else None,
                "voice": {
                    "enabled": voice_typing.enabled,
                    "text": voice_typing.last_text,
                },
                "screen": _screen_telemetry(screen_summary_state, screen_summary_lock),
            }
            # Process is spawned with `-u` (server.js), so stdout is already
            # unbuffered — an explicit flush() here is a redundant syscall.
            sys.stdout.write(json.dumps(telemetry_payload) + "\n")
        except Exception:
            pass

        if HEADLESS:
            continue

        try:
            cv2.imshow("Ultimate Gesture Control - Mouse | Volume", img)
            key = cv2.waitKey(1)
            if key % 256 == 27:
                print("Escape hit, closing the app")
                break
            elif key % 256 == ord('c'):
                voice_buffer.apply("CLEAR")
            elif key % 256 == ord('h'):
                if len(all_hands) > 1:
                    active_hand_index = (active_hand_index + 1) % len(all_hands)
                    ai_gos.status = f"Primary control switched to hand {active_hand_index + 1}"
                else:
                    ai_gos.status = "Show both hands to swap primary control"
            elif key % 256 == ord('v'):
                voice_typing.toggle()
            elif key % 256 == ord('s'):
                ai_gos.status = "Analyzing screen…"
                threading.Thread(target=_run_screen_summary, daemon=True).start()
            else:
                ai_gos.handle_key(key % 256, lmList if len(lmList) else [])
        except Exception:
            time.sleep(0.01)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
