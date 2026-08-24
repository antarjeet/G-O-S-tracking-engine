import cv2
import numpy as np
import time
import autopy
import math
import pyautogui
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
import HandTrackingModule as htm

wcam, hcam = 640, 480
frameR = 100
smoothening = 7

cTime = 0
pTime = 0
plocX, plocY = 0, 0
clocX, clocY = 0, 0

cap = cv2.VideoCapture(0)
cap.set(3, wcam)
cap.set(4, hcam)

detector = htm.HandDetector()

wScr, hScr = autopy.screen.size()

devices = AudioUtilities.GetSpeakers()
interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
volume = interface.QueryInterface(IAudioEndpointVolume)

volRange = volume.GetVolumeRange()
minVol = volRange[0]
maxVol = volRange[1]

vol = 0
volbar = 400
volper = 0

current_mode = "IDLE"

last_hand_state = None
hand_state_change_time = 0
hand_state_debounce = 0.5

while True:
    success, img = cap.read()
    if not success:
        print("Failed to grab frame")
        break

    img = cv2.flip(img, 1)

    img = detector.find_hands(img)
    lmList, bbox = detector.find_position(img)

    if len(lmList) != 0:
        fingers = detector.fingersUp()

        x1, y1 = lmList[8][1:]
        x2, y2 = lmList[12][1:]
        x_thumb, y_thumb = lmList[4][1:]

        if fingers[1] == 1 and fingers[2] == 0 and fingers[3] == 0 and fingers[4] == 0:
            current_mode = "MOUSE MOVE"

            cv2.rectangle(img, (frameR, frameR), (wcam-frameR, hcam-frameR),
                          (255, 0, 255), 2)

            x3 = np.interp(x1, (frameR, wcam-frameR), (0, wScr))
            y3 = np.interp(y1, (frameR, hcam-frameR), (0, hScr))

            clocX = plocX + (x3 - plocX) / smoothening
            clocY = plocY + (y3 - plocY) / smoothening

            autopy.mouse.move(clocX, clocY)
            cv2.circle(img, (x1, y1), 15, (255, 0, 255), cv2.FILLED)
            plocX, plocY = clocX, clocY

            cv2.putText(img, "MODE: MOUSE MOVE", (10, 30), cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 255), 2)

        elif fingers[1] == 1 and fingers[2] == 1 and fingers[3] == 0 and fingers[4] == 0:
            current_mode = "MOUSE CLICK"

            length, img, lineinfo = detector.find_Distance(8, 12, img)

            cv2.putText(img, "MODE: MOUSE CLICK", (10, 30), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 0), 2)

            if length < 40:
                cv2.circle(img, (lineinfo[4], lineinfo[5]), 15, (0, 255, 0), cv2.FILLED)
                autopy.mouse.click()

        elif fingers[0] == 1 and fingers[1] == 1:
            current_mode = "VOLUME CONTROL"

            length = math.hypot(x2 - x_thumb, y2 - y_thumb)

            cv2.circle(img, (x_thumb, y_thumb), 15, (0, 255, 255), cv2.FILLED)
            cv2.circle(img, (x1, y1), 15, (0, 255, 255), cv2.FILLED)
            cv2.line(img, (x_thumb, y_thumb), (x1, y1), (0, 255, 255), 3)

            vol = np.interp(length, [50, 218], [minVol, maxVol])
            volbar = np.interp(length, [50, 218], [400, 150])
            volper = np.interp(length, [50, 218], [0, 100])

            volume.SetMasterVolumeLevel(vol, None)

            if length < 50:
                cv2.circle(img, (int((x_thumb + x1) / 2), int((y_thumb + y1) / 2)), 15, (0, 255, 0), cv2.FILLED)

            cv2.rectangle(img, (50, 150), (85, 400), (0, 255, 255), 3)
            cv2.rectangle(img, (50, int(volbar)), (85, 400), (0, 255, 255), cv2.FILLED)
            cv2.putText(img, f'{int(volper)} %', (40, 450), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 255), 3)

            cv2.putText(img, f"MODE: VOLUME CONTROL | {int(volper)}%", (10, 30), cv2.FONT_HERSHEY_PLAIN, 2, (0, 255, 255), 2)

        else:
            current_mode = "IDLE"
            cv2.putText(img, "MODE: IDLE - Show a gesture", (10, 30), cv2.FONT_HERSHEY_PLAIN, 2, (200, 200, 200), 2)

        if sum(fingers) == 5:
            cv2.putText(img, "HAND: OPEN - Next Tab", (10, 80), cv2.FONT_HERSHEY_PLAIN, 1.5, (255, 255, 0), 2)

            current_time = time.time()
            if last_hand_state != "OPEN" and (current_time - hand_state_change_time) > hand_state_debounce:
                pyautogui.hotkey('alt', 'tab')
                hand_state_change_time = current_time
                last_hand_state = "OPEN"

        elif sum(fingers) == 0:
            cv2.putText(img, "HAND: CLOSED - Previous Tab", (10, 80), cv2.FONT_HERSHEY_PLAIN, 1.5, (255, 100, 0), 2)

            current_time = time.time()
            if last_hand_state != "CLOSED" and (current_time - hand_state_change_time) > hand_state_debounce:
                pyautogui.hotkey('alt', 'shift', 'tab')
                hand_state_change_time = current_time
                last_hand_state = "CLOSED"

    cTime = time.time()
    fps = 1 / (cTime - pTime) if (cTime - pTime) > 0 else 0
    pTime = cTime

    cv2.putText(img, f"FPS: {int(fps)}", (10, 70), cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 0), 3)

    cv2.putText(img, "Index: Mouse | Index+Middle: Click | Thumb+Index: Volume | Open Hand: Next Tab | Closed Hand: Prev Tab",
                (10, hcam - 10), cv2.FONT_HERSHEY_PLAIN, 1, (200, 200, 200), 1)

    cv2.imshow("Combined Gesture Control", img)

    k = cv2.waitKey(1)
    if k % 256 == 27:
        print("Escape hit, closing the app")
        break

cap.release()
cv2.destroyAllWindows()
