import re
import threading


class VoiceTypingModule:
    """`text_buffer` only needs three methods — `.apply(key)` (accepting the
    "CLEAR"/"SPACE"/"ENTER" tokens), `.insert_text(text)` and
    `.delete_last_word()` — see DictationBuffer in ultimate_gesture_control.py,
    the only implementation this ships with."""

    COMMANDS = {
        "clear": "CLEAR", "clear text": "CLEAR", "clear all": "CLEAR",
        "space": "SPACE",
        "enter": "ENTER", "new line": "ENTER", "newline": "ENTER",
    }
    DELETE_WORDS = {"delete", "backspace", "delete word", "delete that", "undo"}

    ACTION_PHRASES = {
        "summarize screen": "SUMMARIZE_SCREEN",
        "summarize the screen": "SUMMARIZE_SCREEN",
        "summarize my screen": "SUMMARIZE_SCREEN",
        "what is on my screen": "SUMMARIZE_SCREEN",
        "whats on my screen": "SUMMARIZE_SCREEN",
        "what is on the screen": "SUMMARIZE_SCREEN",
        "whats on the screen": "SUMMARIZE_SCREEN",
        "what is on screen": "SUMMARIZE_SCREEN",
        "what's on screen": "SUMMARIZE_SCREEN",
        "describe my screen": "SUMMARIZE_SCREEN",
        "describe the screen": "SUMMARIZE_SCREEN",
        "read my screen": "SUMMARIZE_SCREEN",
        "read the screen": "SUMMARIZE_SCREEN",
    }

    def __init__(self, text_buffer, on_action=None):
        self.text_buffer = text_buffer
        # The engine actions ACTION_PHRASES names (currently just
        # "SUMMARIZE_SCREEN") live in ultimate_gesture_control.py's main(),
        # not here, so this module doesn't need to know how to run them.
        self.on_action = on_action
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
        # Public because phrases can come from two different microphones:
        # this module's own PC-mic listening thread (_listen_loop below), or
        # the browser's Web Speech API sending a recognized phrase up over
        # Socket.io as a "VOICE_PHRASE:<text>" engine command. Both paths
        # land here so the space/enter/clear/delete command words behave
        # identically either way.
        if not text:
            return
        self.last_text = text
        self.status = "Voice captured: " + text[:32]

        normalized = self._normalize(text)
        if normalized in self.ACTION_PHRASES:
            action = self.ACTION_PHRASES[normalized]
            self.status = f"Voice command: {action}"
            if self.on_action:
                self.on_action(action)
        elif normalized in self.COMMANDS:
            self.text_buffer.apply(self.COMMANDS[normalized])
        elif normalized in self.DELETE_WORDS:
            self.text_buffer.delete_last_word()
        else:
            self.text_buffer.insert_text(text)

    @staticmethod
    def _normalize(text):
        # Lowercase and drop punctuation (esp. apostrophes) so recognizer
        # variance — "what's on my screen?" vs "whats on my screen" — still
        # matches one ACTION_PHRASES/COMMANDS entry.
        return re.sub(r"[^\w\s]", "", text.strip().lower()).strip()

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
