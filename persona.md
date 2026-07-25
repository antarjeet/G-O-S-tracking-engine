# AI-GOS V2 Project Persona

## Identity

AI-GOS V2 is an all-in-one, webcam-based touchless computer controller. Its
entry point is `ultimate_gesture_control.py`, which combines legacy controls
with an adaptive keyboard, multi-hand gestures, personalization, confidence
analytics, and optional voice input.

## Architecture

```text
Webcam -> HandTrackingModule -> landmarks for up to two hands
       -> Ultimate Gesture Control: primary-hand legacy input + keyboard
       -> AdvancedGestureEngine: secondary-hand actions, profiles, analytics
       -> Windows mouse, keyboard, volume, and window automation
```

The first hand remains the legacy controller. The second hand is dedicated to
advanced actions, reducing accidental conflicts. When two hands are visible,
press `H` to swap the primary role; either hand can therefore run the legacy
mouse/keyboard controls while the other performs advanced actions.

## Primary-hand controls

Mouse gestures mirror finger count, like the buttons and wheel of a physical
mouse:

- Index only: smooth mouse movement
- Index + middle close: left click
- Index + middle + ring: right click
- Index + middle + ring + pinky (thumb down): open/close AI-GOS keyboard
- Open hand (all 5): next tab/window
- Closed hand (fist): previous tab/window
- Thumb + index: system volume
- Index + pinky ("rock on"), move hand up/down: scroll
- `K` command: also opens/closes AI-GOS keyboard mode

Right click uses a cooldown rather than a per-frame pinch check, since a
held pose firing every frame would spam the context menu open/closed. Scroll
deliberately avoids the thumb: `fingersUp()` detects the thumb by x-position
(left/right of the joint below it), which only works when the thumb points
sideways, so a vertical pose like "shaka" reads unreliably. Index and pinky
use the more reliable y-position check instead. Scroll tracks the index
fingertip's vertical movement while the pose is held, and resets its
reference point the moment the pose is released so re-entering it doesn't
jump.

## AI-GOS keyboard

Keyboard mode opens and closes with `K` or the four-finger pose above
(edge-triggered on pose entry, so holding it doesn't repeatedly toggle). It
supports index hover, thumb/index pinch typing, dwell typing, predictive
word insertion, swipe editing, and session-local adaptive key sizing.
Default and Accessible profiles adjust dwell duration and pinch sensitivity.
Typed text stays inside the AI-GOS panel.

## Secondary-hand advanced controls

- Thumb/index pinch hold: drag and drop
- Index + middle: vertical air scroll
- Index-only circular movement around the wrist: circular scroll
  - clockwise: scroll down
  - counter-clockwise: scroll up
- Three fingers rotating around the primary index: zoom
- Four fingers: maximize active window
- Open palm: context-aware shortcut

Circular scrolling requires two visible hands. Extend only the secondary-hand
index, then draw a clear circle around that same hand's wrist.

## Recognition layer

`AdvancedGestureEngine` recognizes and displays:

- One through five fingers
- Pinch, grab, swipe, circle, and rotation
- Air tap, air hold, air scroll, and circular scroll
- User-trained custom static poses

Confidence is based on the visible landmark area. It is a tracking-quality
heuristic, not a probability from a trained gesture-classification model.

## Personalization, context, and analytics

- `T`: save current hand pose as a custom gesture
- `P`: cycle Default and Accessible profiles
- `M`: cycle General, Browser, Coding, and Media contexts
- `V`: toggle optional SpeechRecognition/PyAudio voice capture
- `G`: toggle the advanced AI-GOS layer and dashboard
- `H`: swap the primary control hand when two hands are visible

Profiles are persisted in `gesture_profiles.json` when writable. The dashboard
reports recognition confidence, actions, keyboard WPM, profile, context, voice
state, and the latest recognition result.

## Phone camera pairing (web HUD)

`AI_GOS_CAMERA_SOURCE` (set by the backend when it spawns the engine) selects
the video source: empty for the PC webcam, `"phone"` to poll the backend's
`/api/phone-frame/latest` for frames pushed by a paired phone, a URL for an
IP-camera stream, or a bare integer for a virtual-webcam device index.
`PhoneCameraCapture` in `ultimate_gesture_control.py` presents the same
`.read()`/`.isOpened()` interface as `cv2.VideoCapture` so the rest of the
pipeline is unaware of the frame's origin. Because a phone stream's
resolution won't match the fixed `wcam`/`hcam` the mouse-move mapping and
telemetry normalization assume, every frame is resized back to that fixed
size right after capture, regardless of source.

Pairing itself is QR-code driven: the backend serves a phone-facing capture
page (`backend/public/phone-cam.html`, no build step, no app install) at
`/phone-cam/<sessionId>`, generated fresh each time the HUD's phone-connect
button is clicked. The phone's page uses `getUserMedia()` and POSTs JPEG
frames to `/api/phone-frame/<sessionId>` a few times a second; only one
session is active at a time, matching the app's single-engine-instance
design. Because `getUserMedia()` requires a secure context and the phone
reaches the backend by LAN IP (not `localhost`), the backend serves
everything over HTTPS with a self-signed certificate cached in
`backend/certs/` — both this PC's browser and the phone's browser must
click through a one-time "not trusted" warning before anything else works.

## Web HUD authentication

`backend/db.js` is a JSON-file-backed user/session store (`backend/data/`) —
deliberately not SQLite: `better-sqlite3` needs native compilation, which
isn't guaranteed to be available (no C++ build toolchain) everywhere this
runs. Given this app's actual usage pattern (a personal control panel, very
low write concurrency), a JSON file with atomic write-then-rename is
adequate; a real database wasn't worth the native-dependency risk.
`backend/auth.js` hashes passwords with `bcryptjs` (pure JS, same reasoning)
and issues a random session token stored both in a cookie and in
`sessions.json`. `requireAuth` middleware gates every REST route except the
phone's own capture page and frame upload (protected implicitly: you can't
mint a phone-pairing session without already being logged in, and session
ids are unguessable). Socket.io authenticates the same session cookie during
its handshake via a `io.use()` middleware, since the realtime telemetry/
control channel needs the same gate as the REST API. The one deliberately
ungated route, `/api/phone-frame/latest`, is instead restricted to loopback
callers only — it's polled by the locally-spawned Python process, not by any
browser, so there's no user session to check there at all.

## Code modules

| Module | Responsibility |
|---|---|
| `ultimate_gesture_control.py` | All-in-one application loop and keyboard |
| `ai_gos_features.py` | Recognition, profiles, contexts, analytics, voice, advanced actions |
| `HandTrackingModule.py` | MediaPipe tracking and multi-hand landmark access |

## Constraints

- Windows is required for pycaw volume control.
- A webcam, good lighting, and clear hand visibility are required.
- Desktop automation acts on the focused application; test controls in a safe
  window before using them in important work.

## Existing project applications

The AI-GOS entry point does not remove the original learning and focused-use
applications. They remain available in the project:

| File | Existing responsibility |
|---|---|
| `AI_virtual_mouse.py` | Mouse move/click example |
| `combined_gesture_control.py` | Mouse, volume, and tab-switching example |
| `HandTracking.py` | Direct hand-volume example |
| `hand_volume_control.py` | Object-oriented volume controller |
| `virtual_keyboard.py` | Standalone legacy virtual keyboard |
| `HandTrackingMin.py` | Minimal hand-landmark debugging example |

`HandTrackingModule.py` keeps its original public methods:
`find_hands`, `find_position`, `fingersUp`, and `find_Distance`. The added
`find_all_positions` method makes all detected hands available to AI-GOS while
leaving the original first-hand workflow compatible.

## Retired ultimate-keyboard documentation

The original three-finger (index + middle + ring) keyboard-entry gesture was
retired in favor of `K`-only, then later reintroduced on a *different*,
non-conflicting pose (four fingers, thumb down) once three fingers was
assigned to right click. The standalone `virtual_keyboard.py` remains
documented and unchanged.
