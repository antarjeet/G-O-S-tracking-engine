"""Voice typing: dictates recognized speech into a text buffer.

Runs its own background thread so speech recognition — which blocks on
microphone I/O and a network round-trip to Google's recognizer — never
stalls the per-frame gesture loop in ultimate_gesture_control.py.

A handful of spoken words (SPACE/ENTER/CLEAR/backspace) act as commands
instead of being typed literally — see COMMANDS and DELETE_WORDS below.
A further set of phrases ("summarize screen", "what's on my screen") trigger
engine-level actions unrelated to typing at all — see ACTION_PHRASES.
"""

import re
import threading


class VoiceTypingModule:
    """Listens for speech and routes it into a dictated-text buffer.

    `text_buffer` only needs three methods — `.apply(key)` (accepting the
    "CLEAR"/"SPACE"/"ENTER" tokens), `.insert_text(text)` and
    `.delete_last_word()` — see DictationBuffer in ultimate_gesture_control.py,
    the only implementation this ships with.
    """

    COMMANDS = {
        "clear": "CLEAR", "clear text": "CLEAR", "clear all": "CLEAR",
        "space": "SPACE",
        "enter": "ENTER", "new line": "ENTER", "newline": "ENTER",
    }
    DELETE_WORDS = {"delete", "backspace", "delete word", "delete that", "undo"}

    # A leading "Jarvis" / "Hey Jarvis" hands the rest of the phrase to the
    # Claude-backed assistant (see jarvis.py) instead of typing or matching
    # it as a fixed command — arbitrary natural language, not a fixed
    # phrase list. Matched against the raw phrase (not _normalize()'s
    # output) so the query text Jarvis receives keeps its original
    # punctuation and casing.
    JARVIS_WAKE_RE = re.compile(r"^\s*(hey\s+)?jarvis\b[,:]?\s*", re.IGNORECASE)

    # Phrases that trigger an engine action (via `on_action`) instead of
    # being typed or edited as text. Matched against punctuation-stripped
    # text, so "what's on my screen?" and "whats on my screen" both hit the
    # same entry — see _normalize().
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

    def __init__(self, text_buffer, on_action=None, on_jarvis_query=None):
        self.text_buffer = text_buffer
        # Optional callback(action: str) for ACTION_PHRASES matches — the
        # engine actions those phrases name (currently just
        # "SUMMARIZE_SCREEN") live in ultimate_gesture_control.py's main(),
        # not here, so this module doesn't need to know how to run them.
        self.on_action = on_action
        # Optional callback(query: str) for a "Jarvis, <query>" wake phrase —
        # routes to the Claude-backed assistant in jarvis.py rather than
        # this module's own fixed phrase matching.
        self.on_jarvis_query = on_jarvis_query
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
        """Route one recognized phrase into the text buffer.

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

        wake_match = self.JARVIS_WAKE_RE.match(text)
        if wake_match and self.on_jarvis_query:
            query = text[wake_match.end():].strip()
            if query:
                self.status = f"Jarvis: {query[:40]}"
                self.on_jarvis_query(query)
            else:
                self.status = "Jarvis: (didn't catch a question)"
            return

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
        """Lowercase and drop punctuation (esp. apostrophes) so recognizer
        variance — "what's on my screen?" vs "whats on my screen" — still
        matches one ACTION_PHRASES/COMMANDS entry."""
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
