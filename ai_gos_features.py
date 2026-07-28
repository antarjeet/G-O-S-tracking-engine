"""Opt-in AI-GOS services used by the Ultimate Gesture Control application.

This module intentionally keeps advanced interaction on a *second* hand.  The
legacy first-hand mouse, volume, click and tab gestures therefore continue to
work exactly as before.
"""

import json
import math
import time
from pathlib import Path

import pyautogui


PROFILE_PATH = Path(__file__).with_name("gesture_profiles.json")


class AdvancedGestureEngine:
    """Multi-hand interaction, profiles, confidence and live analytics."""

    def __init__(self):
        self.enabled = True
        self.contexts = ("GENERAL", "BROWSER", "CODING", "MEDIA")
        self.context_index = 0
        self.profile_data = self._load_profiles()
        self.profile_names = list(self.profile_data["profiles"])
        self.profile_index = 0
        self.status = "AI-GOS recognizer ready"
        self.started_at = time.time()
        self.gesture_count = 0
        self.confidences = []
        self.last_pointer = None
        self.last_secondary_y = None
        self.dragging = False
        self.pinch_started_at = None
        self.pinch_scroll_y = None
        self.pinch_scrolling = False
        self.last_action = 0.0
        self.last_rotation = None
        self.last_wheel_angle = None
        self.last_wheel_time = 0.0
        self.wheel_points = []
        self.motion_history = []
        self.hold_started = None
        self.recognized = []

    @property
    def context(self):
        return self.contexts[self.context_index]

    @property
    def profile_name(self):
        return self.profile_names[self.profile_index]

    @property
    def profile(self):
        return self.profile_data["profiles"][self.profile_name]

    def _load_profiles(self):
        default = {
            "profiles": {
                "Default": {
                    "accessibility": {"sensitivity": 1.0, "dwell_seconds": 0.65},
                    "trained_gestures": {},
                },
                "Accessible": {
                    "accessibility": {"sensitivity": 1.35, "dwell_seconds": 1.0},
                    "trained_gestures": {},
                },
            }
        }
        try:
            loaded = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            if loaded.get("profiles"):
                return loaded
        except (OSError, ValueError):
            pass
        return default

    def _save_profiles(self):
        try:
            PROFILE_PATH.write_text(json.dumps(self.profile_data, indent=2), encoding="utf-8")
        except OSError:
            self.status = "Profile changes are active but could not be saved"

    @staticmethod
    def finger_state(hand):
        """Return [thumb, index, middle, ring, pinky] without handedness assumptions."""
        if not hand or len(hand) < 21:
            return [0, 0, 0, 0, 0]
        return [
            int(abs(hand[4][1] - hand[2][1]) > 25),
            *[int(hand[tip][2] < hand[tip - 2][2]) for tip in (8, 12, 16, 20)],
        ]

    @staticmethod
    def point(hand, landmark):
        return hand[landmark][1], hand[landmark][2]

    def confidence(self, hand):
        if not hand or len(hand) < 21:
            return 0.0
        xs = [item[1] for item in hand]
        ys = [item[2] for item in hand]
        area = max(xs) - min(xs)
        area *= max(ys) - min(ys)
        # A hand that occupies a useful camera area produces a higher confidence.
        return round(min(0.99, max(0.35, area / 12500)), 2)

    def train_current_pose(self, hand):
        if not hand:
            self.status = "Show a hand before training a gesture"
            return
        name = f"gesture_{len(self.profile['trained_gestures']) + 1}"
        self.profile["trained_gestures"][name] = {
            "fingers": self.finger_state(hand),
            "created_at": int(time.time()),
        }
        self._save_profiles()
        self.status = f"Trained {name} in {self.profile_name}"

    def _recognize(self, hands, now):
        """Classify static, temporal and user-trained gestures for the HUD.

        Classification is intentionally separate from actions: the first hand
        is observed only, leaving the legacy control loop authoritative.
        """
        hand = hands[0]
        fingers = self.finger_state(hand)
        raised = sum(fingers)
        labels = [{1: "Single Finger", 2: "Two Fingers", 3: "Three Fingers",
                   4: "Four Fingers", 5: "Five Fingers"}.get(raised, "")]
        labels = [label for label in labels if label]
        thumb, index = self.point(hand, 4), self.point(hand, 8)
        if math.hypot(index[0] - thumb[0], index[1] - thumb[1]) < 38:
            labels.append("Pinch")
        if sum(fingers) == 0:
            labels.append("Grab")
        if fingers[1:3] == [1, 1] and not fingers[3] and not fingers[4]:
            labels.append("Air Scroll")

        self.motion_history.append((now, index[0], index[1]))
        self.motion_history = [item for item in self.motion_history if now - item[0] <= 0.8]
        if len(self.motion_history) >= 2:
            start, end = self.motion_history[0], self.motion_history[-1]
            dx, dy = end[1] - start[1], end[2] - start[2]
            if now - start[0] < 0.45 and max(abs(dx), abs(dy)) > 105:
                labels.append("Swipe")
            if abs(dx) < 12 and abs(dy) < 12:
                self.hold_started = self.hold_started or start[0]
                if now - self.hold_started >= 0.7:
                    labels.append("Air Hold")
            else:
                self.hold_started = None
            # An air tap is a quick, mostly vertical index-finger strike.
            if now - start[0] < 0.22 and dy > 55 and abs(dx) < 45:
                labels.append("Air Tap")
            # Circle recognition: motion wraps around its recent centroid.
            if len(self.motion_history) >= 8:
                xs = [item[1] for item in self.motion_history]
                ys = [item[2] for item in self.motion_history]
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                radii = [math.hypot(x - cx, y - cy) for x, y in zip(xs, ys)]
                angles = [math.degrees(math.atan2(y - cy, x - cx)) for x, y in zip(xs, ys)]
                coverage = max(angles) - min(angles)
                if min(radii) > 20 and max(radii) / min(radii) < 2.2 and coverage > 250:
                    labels.append("Circle")

        pose = self.finger_state(hand)
        for name, trained in self.profile["trained_gestures"].items():
            if trained.get("fingers") == pose:
                labels.append(f"Custom: {name}")
                break
        self.recognized = labels
        return labels

    def handle_key(self, key, primary_hand):
        if key == ord("g"):
            self.enabled = not self.enabled
            self.status = "AI-GOS assistant enabled" if self.enabled else "AI-GOS assistant off"
        elif key == ord("m"):
            self.context_index = (self.context_index + 1) % len(self.contexts)
            self.status = f"Context: {self.context}"
        elif key == ord("p"):
            self.profile_index = (self.profile_index + 1) % len(self.profile_names)
            self.status = f"Profile: {self.profile_name}"
        elif key == ord("t"):
            self.train_current_pose(primary_hand)

    def _context_shortcut(self):
        if self.context == "BROWSER":
            pyautogui.hotkey("ctrl", "l")
        elif self.context == "CODING":
            pyautogui.hotkey("ctrl", "shift", "p")
        elif self.context == "MEDIA":
            pyautogui.press("space")
        else:
            pyautogui.hotkey("win", "d")

    def process(self, image, hands, keyboard_text, now):
        """Run secondary-hand gestures and return current confidence.

        Primary hand remains untouched: its legacy behavior is handled by the
        original application loop.  The secondary hand acts as the controller.
        """
        began = time.perf_counter()
        if not self.enabled or not hands:
            return 0.0
        primary = hands[0]
        secondary = hands[1] if len(hands) > 1 else primary
        confidence = (self.confidence(primary) + self.confidence(secondary)) / (2 if len(hands) > 1 else 1)
        self.confidences.append(confidence)
        self.confidences = self.confidences[-300:]
        labels = self._recognize(hands, now)
        if len(hands) > 1 and self.finger_state(secondary)[1:4] == [1, 1, 1]:
            labels.append("Rotation")
        self.status = "Recognized: " + (", ".join(labels) if labels else "hand")
        if len(hands) < 2:
            return confidence
        fingers = self.finger_state(secondary)
        thumb, index = self.point(secondary, 4), self.point(secondary, 8)
        middle = self.point(secondary, 12)
        pinch = math.hypot(index[0] - thumb[0], index[1] - thumb[1])
        cooldown = 0.45 / self.profile["accessibility"]["sensitivity"]

        # Pinch-and-move scroll replaces the less reliable air-wheel gesture.
        # Move vertically immediately after pinching to scroll.  Hold a still
        # pinch briefly to begin the existing drag/drop interaction instead.
        if pinch < 36:
            if self.pinch_started_at is None:
                self.pinch_started_at = now
                self.pinch_scroll_y = index[1]
                self.pinch_scrolling = False
            delta = index[1] - self.pinch_scroll_y
            if abs(delta) > 6:
                if self.dragging:
                    pyautogui.mouseUp()
                    self.dragging = False
                pyautogui.scroll(int(-delta / 7))
                self.pinch_scroll_y = index[1]
                self.pinch_scrolling = True
                self.status = "Pinch scroll: down" if delta > 0 else "Pinch scroll: up"
                self.gesture_count += 1
            elif not self.pinch_scrolling and not self.dragging and now - self.pinch_started_at >= 0.35:
                pyautogui.mouseDown()
                self.dragging = True
                self.gesture_count += 1
                self.status = "Secondary hand: dragging"
        else:
            if self.dragging:
                pyautogui.mouseUp()
                self.dragging = False
                self.gesture_count += 1
                self.status = "Secondary hand: dropped"
            self.pinch_started_at = None
            self.pinch_scroll_y = None
            self.pinch_scrolling = False

        # Two fingers on the second hand scroll vertically.
        if fingers[1:3] == [1, 1] and sum(fingers[3:]) == 0:
            if self.last_secondary_y is not None:
                delta = index[1] - self.last_secondary_y
                if abs(delta) > 7:
                    pyautogui.scroll(int(-delta / 8))
                    self.status = "Secondary hand: scroll"
            self.last_secondary_y = index[1]
        else:
            self.last_secondary_y = None


        # Four fingers maximizes the active window; open palm invokes the
        # selected context shortcut.  Both are debounce protected.
        if now - self.last_action > cooldown:
            if fingers[1:] == [1, 1, 1, 1]:
                pyautogui.hotkey("win", "up")
                self.status = "Window management: maximize"
                self.last_action = now
                self.gesture_count += 1
            elif sum(fingers) == 5:
                self._context_shortcut()
                self.status = f"{self.context} shortcut"
                self.last_action = now
                self.gesture_count += 1

        # Rotation between two index fingertips maps to browser/editor zoom.
        if fingers[1:4] == [1, 1, 1] and not fingers[4]:
            p_index = self.point(primary, 8)
            angle = math.degrees(math.atan2(index[1] - p_index[1], index[0] - p_index[0]))
            if self.last_rotation is not None and now - self.last_action > cooldown:
                turn = (angle - self.last_rotation + 180) % 360 - 180
                if abs(turn) > 30:
                    pyautogui.hotkey("ctrl", "+" if turn > 0 else "-")
                    self.status = "Rotate: zoom in" if turn > 0 else "Rotate: zoom out"
                    self.last_action = now
                    self.gesture_count += 1
            self.last_rotation = angle
        else:
            self.last_rotation = None
        self.status += f" | confidence {confidence:.0%} | {int((time.perf_counter() - began) * 1000)} ms"
        return confidence
