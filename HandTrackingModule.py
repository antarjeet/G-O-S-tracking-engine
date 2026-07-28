import cv2
import mediapipe as mp
import time
import math


class HandDetector():

    def __init__(self, max_num_hands=2, detection_confidence=0.5, tracking_confidence=0.5,
                 model_complexity=1):

        self.lmList = None
        self.results = None
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            static_image_mode=False,
            max_num_hands=max_num_hands,
            min_detection_confidence=detection_confidence,
            min_tracking_confidence=tracking_confidence,
            # 0 = the "lite" landmark model: notably faster CPU inference per
            # frame at a small accuracy cost, which is what actually gates
            # real-time FPS here (camera capture and JPEG streaming are cheap
            # by comparison).
            model_complexity=model_complexity,
        )
        self.mp_draw = mp.solutions.drawing_utils

        self.tipIds = [4, 8, 12, 16, 20]

    def find_hands(self, img, draw=True):
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.results = self.hands.process(img_rgb)

        # print(results.multi_hand_landmarks)

        if self.results.multi_hand_landmarks:
            for hand_landmarks in self.results.multi_hand_landmarks:
                if draw:
                    # No connections passed => no skeleton lines, just the
                    # landmark points themselves, drawn in solid white.
                    self.mp_draw.draw_landmarks(
                        img, hand_landmarks, connections=None,
                        landmark_drawing_spec=self.mp_draw.DrawingSpec(
                            color=(255, 255, 255), thickness=2, circle_radius=4))
        return img

    def find_position(self, img, handno=0, draw=True):
        xList = []
        yList = []
        bbox = []

        self.lmList = []

        if self.results.multi_hand_landmarks:
            myHand = self.results.multi_hand_landmarks[handno]
            for id, lm in enumerate(myHand.landmark):
                h, w, c = img.shape
                cx, cy = int(lm.x * w), int(lm.y * h)
                xList.append(cx)
                yList.append(cy)

                self.lmList.append([id, cx, cy])
                if draw:
                    cv2.circle(img, (cx, cy), 7, (255, 255, 255), cv2.FILLED)

            xmin, xmax = min(xList), max(xList)
            ymin, ymax = min(yList), max(yList)
            bbox = xmax, ymin, xmax, ymax

        return self.lmList, bbox

    def find_all_positions(self, img, draw=False):
        """Return landmarks for every tracked hand without changing legacy state.

        Existing applications continue to use :meth:`find_position` and the
        first hand.  AI-GOS uses this method to make the second hand available
        as a non-conflicting gesture controller.
        """
        all_hands = []
        if not self.results.multi_hand_landmarks:
            return all_hands
        h, w, _ = img.shape
        for hand_landmarks in self.results.multi_hand_landmarks:
            landmarks = []
            for landmark_id, landmark in enumerate(hand_landmarks.landmark):
                cx, cy = int(landmark.x * w), int(landmark.y * h)
                landmarks.append([landmark_id, cx, cy])
                if draw:
                    cv2.circle(img, (cx, cy), 5, (0, 255, 255), cv2.FILLED)
            all_hands.append(landmarks)
        return all_hands

    def fingersUp(self):

        fingers = []

        # thumb

        if self.lmList[self.tipIds[0]][1] > self.lmList[self.tipIds[0] - 1][1]:
            fingers.append(1)
        else:
            fingers.append(0)

        # fingers
        for id in range(1, 5):
            if self.lmList[self.tipIds[id]][2] < self.lmList[self.tipIds[id] - 2][2]:
                fingers.append(1)

            else:
                fingers.append(0)
        return fingers

    def find_Distance(self, p1, p2, img, draw=True, r=15, t=3):
        x1, y1 = self.lmList[p1][1:]
        x2, y2 = self.lmList[p2][1:]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

        if draw:
            cv2.line(img, (x1, y1), (x2, y2), (255, 0, 255), t)
            cv2.circle(img, (x2, y2), r, (255, 0, 255), cv2.FILLED)
            cv2.circle(img, (x2, y2), r, (255, 0, 255), cv2.FILLED)
            cv2.circle(img, (cx, cy), r, (255, 0, 255), cv2.FILLED)
        length = math.hypot(x2 - x1, y2 - y1)
        return length, img, [x1, y1, x2, y2, cx, cy]


def main():
    cap = cv2.VideoCapture(0)
    detector = HandDetector()
    c_time = 0
    p_time = 0
    while True:
        success, img = cap.read()
        if not success:
            print("failed to grab frame")
            break

        img = detector.find_hands(img)
        lmList, bbox = detector.find_position(img)
        if len(lmList) != 0:
            print(lmList[4])
        c_time = time.time()
        fps = 1 / (c_time - p_time) if (c_time - p_time) > 0 else 0
        p_time = c_time

        cv2.putText(img, str(int(fps)), (10, 70), cv2.FONT_HERSHEY_PLAIN, 2,
                    (255, 0, 255), 3)

        cv2.imshow("Image", img)

        k = cv2.waitKey(1)

        if k % 256 == 27:
            print("Escape hit , closing the app")
            break

    cap.release()

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()


def detector():
    return None
