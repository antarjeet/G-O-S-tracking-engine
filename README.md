# AI-GOS V2: Ultimate Gesture Control

Run the complete touchless control system with:

```bash
pip install opencv-python mediapipe numpy autopy pyautogui pycaw comtypes
python ultimate_gesture_control.py
```

`ultimate_gesture_control.py` runs mouse, click, volume, tabs, adaptive
keyboard, multi-hand controls, profiles, confidence analytics, and optional
voice input in one camera window. Voice input also needs `SpeechRecognition`
and `PyAudio`; toggle it with `V`.

## Primary-hand controls

Mouse gestures follow finger count, like the buttons and wheel on a physical
mouse:

| Gesture | Action |
|---|---|
| Index finger only | Move mouse |
| Index + middle close | Left click |
| Index + middle + ring | Right click |
| Index + middle + ring + pinky (thumb down) | Open/close AI-GOS keyboard |
| Open hand (all 5) | Next tab/window |
| Closed hand (fist) | Previous tab/window |
| Thumb + index | Volume |
| Index + pinky ("rock on"), move hand up/down | Scroll |
| `K` command | Also opens/closes AI-GOS keyboard mode |

The primary hand retains the original controls. Advanced actions use the
second hand to avoid gesture conflicts. Press `H` when both hands are visible
to swap the primary role, so either hand can operate mouse, volume, and
keyboard controls.

## AI-GOS keyboard

Show four fingers (index + middle + ring + pinky, thumb down) or press `K` to
open keyboard mode; it never opens merely because both hands or three fingers
are visible. Then hover the index over a key and pinch thumb-to-index to
type. Dwell typing, word predictions, adaptive key sizes, and swipe shortcuts
are included.

| Keyboard gesture | Action |
|---|---|
| Swipe left/right | Delete/space |
| Swipe up/down | Enter/clear |
| Pinch a predicted word | Insert the complete word |

Text stays in the AI-GOS panel and is not inserted automatically in another
application.

## Advanced second-hand controls

| Gesture | Action |
|---|---|
| Thumb/index pinch hold | Drag; release to drop |
| Index + middle | Vertical air scroll |
| Thumb + index pinch, then move vertically | Scroll |
| Three fingers, rotating around primary index | Zoom |
| Four fingers | Maximize active window |
| Open palm | Context shortcut |

### Pinch-and-move scroll

1. Keep both hands visible.
2. Pinch thumb and index finger on the second hand.
3. Move the pinched hand up or down to scroll.

Start moving soon after the pinch. Holding the pinch still for a moment
continues to start drag/drop; release the pinch to finish either action.

## Recognition and dashboard

The AI-GOS dashboard recognizes single, two, three, four, and five fingers;
pinch, grab, swipe, rotation, air tap, air hold, air scroll, pinch scrolling,
and trained custom poses. It reports confidence, action count, typing
WPM, profile/context, voice status, and the latest gesture.

Confidence is a hand-visibility heuristic, not a trained-model probability.

## Settings and personalization

| Key | Action |
|---|---|
| `G` | Toggle AI-GOS dashboard/advanced layer |
| `M` | Cycle General, Browser, Coding, Media contexts |
| `P` | Cycle Default and Accessible profiles |
| `T` | Train the currently visible pose as a custom gesture |
| `V` | Start/stop optional voice capture |
| `H` | Swap the primary control hand when two hands are visible |
| `C` | Clear keyboard text |
| `K` | Open/close AI-GOS keyboard mode |
| `Esc` | Exit |

Profiles and trained poses are saved to `gesture_profiles.json` when possible.
The secondary open-palm shortcut shows desktop in General, focuses the address
bar in Browser, opens the command palette in Coding, and play/pauses in Media.

## Files

| File | Purpose |
|---|---|
| `ultimate_gesture_control.py` | Main all-in-one application |
| `ai_gos_features.py` | Recognition, profiles, analytics, advanced gestures, voice |
| `HandTrackingModule.py` | MediaPipe single/multi-hand tracking |

Windows is required for the pycaw volume control. Use good lighting and test
desktop automation in a safe window before using it for important work.

## Using a phone as the camera (web HUD only)

When launched from the `ai-gos-hud` web dashboard (not run standalone), you
can use a phone's camera instead of the PC's webcam — no app install needed:

1. Start `backend/` (`npm start`) and `ai-gos-hud/` (`npm run dev`) as usual.
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

## Web HUD account (login required)

The web dashboard requires an account — anyone reaching the backend (e.g.
another device on your Wi-Fi, now that phone-camera pairing opens it up to
the LAN) can't start the engine, connect a phone camera, or see telemetry
without logging in first.

- First visit: click **Sign Up** (username, email, password — 8+ characters)
  to create an account, or **Log In** if you already have one.
- Accounts are stored locally in `backend/data/users.json`, passwords hashed
  with bcrypt — nothing is sent anywhere external. Sessions are stored in
  `backend/data/sessions.json` and last 30 days.
- There's no "admin" distinction; any account can control the engine and
  phone camera. This gates out strangers on your network, not different
  permission levels between people you trust.
- Logging in sets a session cookie that also authenticates the Socket.io
  telemetry connection — an expired or missing session gets disconnected and
  sent back to the login screen automatically.

## Live keyboard and voice state

Once the engine is running, the telemetry stream includes `keyboard` (`active`,
`text`, `predictions`, `status`) and `voice` (`enabled`, `text`) — this is what
drives the HUD's on-screen predictive keyboard and the VOICE status chip.
Clicking a key or a predicted word in the HUD sends a `TYPE:<key>` or
`INSERT_PREDICTION:<word>` command that types into the same `AIGOSKeyboard`
buffer that pinch/dwell gesture typing uses, so gesture typing and clicking
the on-screen keyboard both work on the same live text.

## Existing standalone applications

The all-in-one application is the recommended entry point, but the original
examples remain in this repository and can still be run independently.

| Command | Current purpose |
|---|---|
| `python AI_virtual_mouse.py` | Original mouse movement and click example |
| `python combined_gesture_control.py` | Original combined mouse, volume, and tab controller |
| `python HandTracking.py` | Original hand-volume controller |
| `python hand_volume_control.py` | Class-based hand-volume controller |
| `python virtual_keyboard.py` | Standalone legacy virtual keyboard |
| `python HandTrackingMin.py` | Minimal landmark-tracking/debug example |

## Legacy configuration reference

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




to start use------------ .\.venv\Scripts\python.exe .\ultimate_gesture_control.py
