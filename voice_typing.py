"""Voice typing: dictates recognized speech into a keyboard's text buffer.

Runs its own background thread so speech recognition — which blocks on
microphone I/O and a network round-trip to Google's recognizer — never
stalls the per-frame gesture loop in ultimate_gesture_control.py.

A handful of spoken words act as commands instead of being typed literally,
mirroring the swipe-gesture shortcuts that already do the same actions
(SPACE/ENTER/CLEAR/backspace) so voice and gesture typing feel consistent.
"""

import threading


class VoiceTypingModule:
    """Listens for speech and routes it into a keyboard-like text buffer.

    `keyboard` only needs three methods — `.apply(key)` (accepting the
    "CLEAR"/"SPACE"/"ENTER" tokens), `.insert_text(text)` and
    `.delete_last_word()` — see AIGOSKeyboard in ultimate_gesture_control.py,
    the only implementation this ships with.
    """

    COMMANDS = {
        "clear": "CLEAR", "clear text": "CLEAR", "clear all": "CLEAR",
        "space": "SPACE",
        "enter": "ENTER", "new line": "ENTER", "newline": "ENTER",
    }
    DELETE_WORDS = {"delete", "backspace", "delete word", "delete that", "undo"}

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.enabled = False
        self.last_text = ""
        self.status = "Voice typing off"
        self._thread = None
        self._stop = threading.Event()

    def toggle(self):
        self.stop() if self.enabled else self.start()

    def start(self):
        if self.enabled:
            return
        self.enabled = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        self.status = "Listening for voice input..."

    def stop(self):
        self.enabled = False
        self._stop.set()
        self.status = "Voice typing off"

    def handle_phrase(self, text):
        """Route one recognized phrase into the keyboard buffer.

        Public because phrases can come from two different microphones:
        this module's own PC-mic listening thread (_listen_loop below), or
        the browser's Web Speech API sending a recognized phrase up over
        Socket.io as a "VOICE_PHRASE:<text>" engine command (see the
        VOICE_PHRASE handler in ultimate_gesture_control.py's
        apply_command) — the web HUD's mic, which could be a phone or a
        different PC than the one running the engine. Both paths land here
        so the space/enter/clear/delete command words behave identically
        either way.
        """
        if not text:
            return
        self.last_text = text
        self.status = "Voice captured: " + text[:32]
        normalized = text.strip().lower()
        if normalized in self.COMMANDS:
            self.keyboard.apply(self.COMMANDS[normalized])
        elif normalized in self.DELETE_WORDS:
            self.keyboard.delete_last_word()
        else:
            self.keyboard.insert_text(text)

    def _listen_loop(self):
        try:
            import speech_recognition as sr
        except ImportError:
            self.enabled = False
            self.status = "Install SpeechRecognition + PyAudio to use voice input"
            return
        recognizer = sr.Recognizer()
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                while not self._stop.is_set():
                    try:
                        audio = recognizer.listen(source, timeout=1, phrase_time_limit=5)
                        self.handle_phrase(recognizer.recognize_google(audio))
                    except (sr.WaitTimeoutError, sr.UnknownValueError):
                        continue
                    except sr.RequestError:
                        self.status = "Voice service unavailable"
                        break
        except OSError:
            self.status = "Microphone is unavailable"
        self.enabled = False
