import mediapipe as mp
import cv2
import numpy as np
import time
import math
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume


class HandVolumeControl:
    def __init__(self):
        self.wcam, self.hcam = 640, 480
        self.cap = cv2.VideoCapture(0)
        self.cap.set(3, self.wcam)
        self.cap.set(4, self.hcam)
        self.c_time = 0
        self.p_time = 0
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands()
        self.mp_draw = mp.solutions.drawing_utils

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.volume = interface.QueryInterface(IAudioEndpointVolume)
        volRange = self.volume.GetVolumeRange()
        self.minVol = volRange[0]
        self.maxVol = volRange[1]

    def control_volume(self):
        while True:
            success, img = self.cap.read()
            if not success:
                print("Failed to grab frame")
                break
            img = self.find_hands(img)
            lmList = self.find_position(img, draw=False)

            if len(lmList) != 0:
                x1, y1 = lmList[4][1], lmList[4][2]
                x2, y2 = lmList[8][1], lmList[8][2]
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

                cv2.circle(img, (x1, y1), 15, (255, 0, 255), cv2.FILLED)
                cv2.circle(img, (x2, y2), 15, (255, 0, 255), cv2.FILLED)
                cv2.line(img, (x1, y1), (x2, y2), (255, 0, 255), 3)
                cv2.circle(img, (cx, cy), 15, (255, 0, 255), cv2.FILLED)

                length = math.hypot(x2 - x1, y2 - y1)

                vol = np.interp(length, [50, 218], [self.minVol, self.maxVol])

                volbar = np.interp(length, [50, 218], [400, 150])
                volper = np.interp(length, [50, 218], [0, 100])
                print(vol)

                self.volume.SetMasterVolumeLevel(vol, None)

                if length < 50:
                    cv2.circle(img, (cx, cy), 15, (0, 255, 0), cv2.FILLED)

                cv2.rectangle(img, (50, 150), (85, 400), (0, 255, 0), 3)
                cv2.rectangle(img, (50, int(volbar)), (85, 400), (0, 255, 0), cv2.FILLED)
                cv2.putText(img, f'{int(volper)} %', (40, 450), cv2.FONT_HERSHEY_PLAIN, 2,
                            (0, 255, 0), 3)

                self.c_time = time.time()
                fps = 1 / (self.c_time - self.p_time)
                self.p_time = self.c_time

                cv2.putText(img, str(int(fps)), (10, 70), cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 255), 3)
            cv2.imshow("HandVolumeControl", img)
            k = cv2.waitKey(1)

            if k % 256 == 27:
                print("Escape hit, closing the app")
                break

        self.cap.release()
        cv2.destroyAllWindows()

    def find_hands(self, img, draw=True):
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.results = self.hands.process(img_rgb)

        if self.results.multi_hand_landmarks:
            for hand_landmarks in self.results.multi_hand_landmarks:
                if draw:
                    self.mp_draw.draw_landmarks(img, hand_landmarks,
                                                self.mp_hands.HAND_CONNECTIONS)
        return img

    def find_position(self, img, handno=0, draw=True):
        lmList = []
        if self.results.multi_hand_landmarks:
            myHand = self.results.multi_hand_landmarks[handno]
            for id, lm in enumerate(myHand.landmark):
                h, w, c = img.shape
                cx, cy = int(lm.x * w), int(lm.y * h)
                lmList.append([id, cx, cy])
                if draw:
                    cv2.circle(img, (cx, cy), 7, (255, 0, 255), cv2.FILLED)
        return lmList


if __name__ == "__main__":
    hand_volume_control = HandVolumeControl()
    hand_volume_control.control_volume()

