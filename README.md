# 🖐️ AI-GOS V2: Ultimate Gesture Control

Run the complete touchless control system with:

```bash
pip install opencv-python mediapipe numpy autopy pyautogui pycaw comtypes
python ultimate_gesture_control.py
```

`ultimate_gesture_control.py` runs mouse, click, volume, tabs, multi-hand
controls, profiles, confidence analytics, and optional voice input in one
camera window. Voice input also needs `SpeechRecognition` and `PyAudio`;
toggle it with `V`. Text entry is voice-only — there is no on-screen or
gesture-typed keyboard.

## 🖱️ Primary-hand controls

Mouse gestures follow finger count, like the buttons and wheel on a physical
mouse:

| Gesture | Action |
|---|---|
| Index finger only | Move mouse |
| Index + middle close | Left click |
| Index + middle + ring | Right click |
| Open hand (all 5) | Next tab/window |
| Closed hand (fist) | Previous tab/window |
| Thumb + index | Volume |
| Index + pinky ("rock on"), move hand up/down | Scroll |

The primary hand retains the original controls. Advanced actions use the
second hand to avoid gesture conflicts. Press `H` when both hands are visible
to swap the primary role, so either hand can operate mouse, volume, and
scroll controls.

## ✨ Advanced second-hand controls

| Gesture | Action |
|---|---|
| Thumb/index pinch hold | Drag; release to drop |
| Index + middle | Vertical air scroll |
| Thumb + index pinch, then move vertically | Scroll |
| Three fingers, rotating around primary index | Zoom |
| Four fingers | Maximize active window |
| Open palm | Context shortcut |

### 🤏 Pinch-and-move scroll

1. Keep both hands visible.
2. Pinch thumb and index finger on the second hand.
3. Move the pinched hand up or down to scroll.

Start moving soon after the pinch. Holding the pinch still for a moment
continues to start drag/drop; release the pinch to finish either action.

## 📊 Recognition and dashboard

The AI-GOS dashboard recognizes single, two, three, four, and five fingers;
pinch, grab, swipe, rotation, air tap, air hold, air scroll, pinch scrolling,
and trained custom poses. It reports confidence, action count, typing
WPM, profile/context, voice status, and the latest gesture.

Confidence is a hand-visibility heuristic, not a trained-model probability.

## ⚙️ Settings and personalization

| Key | Action |
|---|---|
| `G` | Toggle AI-GOS dashboard/advanced layer |
| `M` | Cycle General, Browser, Coding, Media contexts |
| `P` | Cycle Default and Accessible profiles |
| `T` | Train the currently visible pose as a custom gesture |
| `V` | Start/stop optional voice capture |
| `H` | Swap the primary control hand when two hands are visible |
| `C` | Clear dictated text |
| `S` | Summarize the current screen (OCR + window scan) |
| `Esc` | Exit |

Profiles and trained poses are saved to `gesture_profiles.json` when possible.
The secondary open-palm shortcut shows desktop in General, focuses the address
bar in Browser, opens the command palette in Coding, and play/pauses in Media.

## 📁 Files

| File | Purpose |
|---|---|
| `ultimate_gesture_control.py` | Main all-in-one application |
| `ai_gos_features.py` | Recognition, profiles, analytics, advanced gestures, voice |
| `HandTrackingModule.py` | MediaPipe single/multi-hand tracking |

Windows is required for the pycaw volume control. Use good lighting and test
desktop automation in a safe window before using it for important work.

## 📱 Using a phone as the camera (web HUD only)

When launched from the `gos` web dashboard (not run standalone), you
can use a phone's camera instead of the PC's webcam — no app install needed:

1. Start the backend from `../gos/backend` (`npm start`) — it serves the
   `gos` frontend (`index.html`) itself on the same origin, no separate dev
   server needed.
2. In the HUD, click the phone icon next to **Start Engine** to get a QR code.
3. Scan it with your phone's camera app. Phone and PC must be on the **same
   Wi-Fi network**.
4. Your phone will show a certificate warning the first time — tap
   **Advanced → Proceed**. This is expected: the backend serves HTTPS with a
   self-signed certificate (required for phone browsers to allow camera
   access at all), not a sign of a real problem.
5. Once the phone's page shows "Streaming to AI-GOS", click **Start Engine** —
   it will automatically prefer the phone's camera over the PC webcam.

You can also point `AI_GOS_CAMERA_SOURCE` (an environment variable the
backend passes through) at an IP-camera stream URL (e.g. the Android "IP
Webcam" app) or a virtual-webcam device index (e.g. DroidCam/iVCam) instead.

## 🔐 Web HUD account (login required)

The web dashboard requires an account — anyone reaching the backend (e.g.
another device on your Wi-Fi, now that phone-camera pairing opens it up to
the LAN) can't start the engine, connect a phone camera, or see telemetry
without logging in first.

- First visit: click **Sign Up** (username, email, password — 8+ characters)
  to create an account, or **Log In** if you already have one.
- Accounts are stored locally in `../gos/backend/data/users.json`, passwords
  hashed with bcrypt — nothing is sent anywhere external. Sessions are stored
  in `../gos/backend/data/sessions.json` and last 30 days.
- There's no "admin" distinction; any account can control the engine and
  phone camera. This gates out strangers on your network, not different
  permission levels between people you trust.
- Logging in sets a session cookie that also authenticates the Socket.io
  telemetry connection — an expired or missing session gets disconnected and
  sent back to the login screen automatically.

## 🎙️ Live voice state

Once the engine is running, the telemetry stream includes `voice` (`enabled`,
`text`) — this is what drives the HUD's VOICE status chip. Dictated speech
types straight into whatever field has OS focus; it isn't buffered or
displayed in the HUD.

## 🗂️ Existing standalone applications

The all-in-one application is the recommended entry point, but the original
examples remain in this repository and can still be run independently.

| Command | Current purpose |
|---|---|
| `python AI_virtual_mouse.py` | Original mouse movement and click example |
| `python combined_gesture_control.py` | Original combined mouse, volume, and tab controller |
| `python HandTracking.py` | Original hand-volume controller |
| `python hand_volume_control.py` | Class-based hand-volume controller |
| `python HandTrackingMin.py` | Minimal landmark-tracking/debug example |

## 🧰 Legacy configuration reference

The original all-in-one controls retain their established values:

```python
wcam, hcam = 640, 480
frameR = 100
smoothening = 7
mouse_click_distance = 40
volume_distance_range = [50, 218]
tab_switch_debounce = 0.5
right_click_cooldown = 0.6
scroll_sensitivity = 2.2
```

`HandTrackingModule.py` remains the shared MediaPipe wrapper. Its original
`find_hands`, `find_position`, `fingersUp`, and `find_Distance` APIs remain
available, and `find_all_positions` extends it for AI-GOS multi-hand support.


## 🔗 Related repositories

The `gos` web dashboard (frontend + backend) that can drive this engine over
the phone-camera/web-HUD flow described above lives in a separate repo:
[G-O-S-tracking](https://github.com/antarjeet/G-O-S-tracking).
