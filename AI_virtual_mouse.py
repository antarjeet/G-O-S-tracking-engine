import cv2
import numpy as np
import time
import autopy
import HandTrackingModule as htm

###############
wcam, hcam = 640, 480
frameR = 100
smoothening = 7
###############
cTime = 0
pTime = 0
plocX, plocY = 0, 0
clocX, clocY = 0, 0

cap = cv2.VideoCapture(0)
cap.set(3, wcam)
cap.set(4, hcam)


detector = htm.HandDetector()
wScr, hScr = autopy.screen.size()
# print(wScr, hScr)
while True:
    # Find hand Landmarks
    success, img = cap.read()
    if not success:
        print("failed to grab frame")
        break

    # Flip camera horizontally for mirror effect
    img = cv2.flip(img, 1)

    img = detector.find_hands(img)
    lmList, bbox = detector.find_position(img)

    # GET THE TIP OF THE INDEX AND MIDDLE FINGERS
    if len(lmList) != 0:
        x1, y1 = lmList[8][1:]
        x2, y2 = lmList[12][1:]
        # print(x1, y1, x2, y2)

        # CHECK WHICH FINGERS ARE UP
        fingers = detector.fingersUp()
        # print(fingers)
        # ONLY INDEX FINGERS : MOVE
        if fingers[1] == 1 and fingers[2] == 0:

            # CONVERT COORDINATES
            cv2.rectangle(img, (frameR, frameR), (wcam-frameR, hcam-frameR),
                          (255, 0, 255), 2)
            x3 = np.interp(x1, (frameR, wcam-frameR), (0, wScr))
            y3 = np.interp(y1, (frameR, hcam-frameR), (0, hScr))

            # SMOOTH VALUE
            clocX = plocX + (x3 - plocX) / smoothening
            clocY = plocY + (y3 - plocY) / smoothening

            # MOVE MOUSE (fixed direction)
            autopy.mouse.move(clocX, clocY)
            cv2.circle(img, (x1, y1), 15,  (255, 0, 255), cv2.FILLED)
            plocX, plocY = clocX, clocY

        # BOTH INDEX AND MIDDLE FINGERS ARE UP : CLICK MODE
        if fingers[1] == 1 and fingers[2] == 1:
            length, img, lineinfo = detector.find_Distance(8, 12, img)
            print(length)
            # FIND DISTANCE BETWEEN FINGERS
            if length < 40:
                cv2.circle(img, (lineinfo[4], lineinfo[5]), 15, (255, 0, 255), cv2.FILLED)
                autopy.mouse.click()

    # Frame Rate
    cTime = time.time()
    fps = 1 / (cTime - pTime) if (cTime - pTime) > 0 else 0
    pTime = cTime

    cv2.putText(img, str(int(fps)), (10, 70), cv2.FONT_HERSHEY_PLAIN, 2, (255, 0, 0), 3)

    cv2.imshow("Image", img)

    k = cv2.waitKey(1)

    if k % 256 == 27:
        print("Escape hit , closing the app")
        break

cap.release()

cv2.destroyAllWindows()
