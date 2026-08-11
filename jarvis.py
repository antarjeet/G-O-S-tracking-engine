"""Jarvis: a real conversational assistant for AI-GOS, backed by the Claude API.

Everything else in this repo that answers a spoken question — the fixed
phrase-matching in VoiceTypingModule.ACTION_PHRASES, screen_understanding.py's
heuristic UI-region detection — works entirely offline with no external
service and no per-call cost. Jarvis is deliberately different: it actually
understands arbitrary natural language, by sending the query to Claude, and
it costs money per call (a short voice Q&A is a small fraction of a cent, but
it is not free, unlike the rest of the engine).

Triggered by saying "Jarvis" (see VoiceTypingModule.on_jarvis_query in
voice_typing.py, which recognizes the wake word identically whether spoken
into the PC mic or the web HUD's browser mic) or the ASK_JARVIS:<query>
engine command. Requires ANTHROPIC_API_KEY — see .env.example. Without it,
`JarvisAssistant.available` is False and ask() returns a plain explanation
instead of failing silently.

Jarvis is given tools built from capabilities that already exist elsewhere
in this codebase — screen_understanding.py for perception, apply_command()
(via the `run_command` callback) for engine control — rather than
reimplementing anything. It does not maintain conversation memory across
calls; every ask() is a fresh, independent turn.
"""

import os
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import anthropic
from anthropic import beta_tool
import pyautogui

try:
    import pyttsx3
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

JARVIS_MODEL = "claude-opus-5"

SYSTEM_PROMPT = (
    "You are Jarvis, the voice assistant built into AI-GOS, a touchless "
    "webcam-gesture computer control system. The user is speaking to you out "
    "loud and your reply is read aloud by text-to-speech, so keep answers to "
    "one or two short sentences unless the user clearly wants more detail. "
    "You have tools to see what is on the user's screen and to control the "
    "gesture engine — use them instead of guessing when the question depends "
    "on live state."
)

# The apply_command() commands that are safe to run with no live hand data
# (matches exactly what apply_command() itself already accepts from the web
# HUD's buttons and VOICE_PHRASE-triggered actions — see
# ultimate_gesture_control.py). TRAIN and SUMMARIZE_SCREEN are deliberately
# excluded: TRAIN needs real hand landmarks to do anything, and screen
# summarization has its own direct tool below instead of going through the
# background-thread dispatch apply_command() uses for it.
CONTROLLABLE_COMMANDS = {
    "TOGGLE_AI_GOS", "CYCLE_CONTEXT",
    "CYCLE_PROFILE", "TOGGLE_VOICE", "CLEAR",
}


class JarvisAssistant:
    """One Claude-backed conversational turn at a time.

    `run_command(command)` should dispatch the same engine-command strings
    apply_command() in ultimate_gesture_control.py already understands, and
    `get_status()` should return a short live-status string (FPS/gesture/
    confidence) — both are injected rather than imported so this module
    doesn't need to reach into main()'s local state directly.
    """

    def __init__(self, screen_ai, run_command, get_status):
        self.screen_ai = screen_ai
        self.run_command = run_command
        self.get_status = get_status

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        self.status = (
            "Jarvis idle" if self.client
            else "Jarvis unavailable — set ANTHROPIC_API_KEY in AI-VIRTUAL-MOUSE/.env"
        )
        self.last_query = ""
        self.last_response = ""

        self._tts_lock = threading.Lock()
        self._tts_engine = None
        if TTS_AVAILABLE:
            try:
                self._tts_engine = pyttsx3.init()
            except Exception:
                self._tts_engine = None

    @property
    def available(self):
        return self.client is not None

    def _build_tools(self):
        """Fresh tool closures per call — cheap, and keeps them bound to this
        instance's screen_ai/run_command/get_status without needing a class
        with @beta_tool on bound methods (the decorator expects a plain
        function it can introspect for a JSON schema)."""
        screen_ai = self.screen_ai
        run_command = self.run_command
        get_status = self.get_status

        @beta_tool
        def summarize_screen() -> str:
            """Describe what is currently on the screen: the active window,
            other open windows, the menu bar, notable on-screen text, and a
            rough count of button/icon-shaped regions. Use this whenever the
            user asks what's on their screen, to describe it, or to read it
            aloud to them."""
            return screen_ai.summarize()["summary"]

        @beta_tool
        def find_and_click(label: str) -> str:
            """Find on-screen text matching `label` and click it. Use this
            when the user asks you to click, press, or open something
            identified by its visible label — a button, a link, a menu item.

            Args:
                label: The visible text to look for, e.g. "Save" or "File".
            """
            words = screen_ai.extract_text(screen_ai.capture())
            matches = screen_ai.locate_text(label, words=words)
            if not matches:
                return f'Could not find "{label}" anywhere on the current screen.'
            match = matches[0]
            pyautogui.click(match["clickX"], match["clickY"])
            return f'Clicked "{match["text"]}" at ({match["clickX"]}, {match["clickY"]}).'

        @beta_tool
        def control_engine(command: str) -> str:
            """Send a command to the AI-GOS gesture engine.

            Args:
                command: One of TOGGLE_AI_GOS (toggle the AI-GOS gesture
                    layer), CYCLE_CONTEXT (switch gesture context:
                    General/Browser/Coding/Media), CYCLE_PROFILE (switch
                    Default/Accessible profile), TOGGLE_VOICE (toggle PC-mic
                    voice typing), CLEAR (clear the dictated text buffer).
            """
            cmd = command.strip().upper()
            if cmd not in CONTROLLABLE_COMMANDS:
                return (f"Unknown command '{command}'. Valid commands: "
                        f"{', '.join(sorted(CONTROLLABLE_COMMANDS))}.")
            run_command(cmd)
            return f"Ran {cmd}."

        @beta_tool
        def engine_status() -> str:
            """Get the gesture engine's current live status: FPS, current
            gesture, confidence, and whether the on-screen keyboard is
            open."""
            return get_status()

        return [summarize_screen, find_and_click, control_engine, engine_status]

    def ask(self, query):
        """Run one conversational turn: send `query` to Claude with the
        tools above, let it call tools as needed, then speak and return the
        final text answer.

        Synchronous and network-bound (typically 1-5s) — always call this
        from a background thread, never the gesture loop's thread. See
        _run_jarvis_query() in ultimate_gesture_control.py.
        """
        if not self.available:
            return self.status

        self.last_query = query
        self.status = f"Jarvis thinking: {query[:40]}"
        try:
            runner = self.client.beta.messages.tool_runner(
                model=JARVIS_MODEL,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
                system=SYSTEM_PROMPT,
                tools=self._build_tools(),
                messages=[{"role": "user", "content": query}],
            )
            final_text = ""
            for message in runner:
                for block in message.content:
                    if block.type == "text" and block.text:
                        final_text = block.text
            if not final_text:
                final_text = "I didn't have anything to say to that."
        except Exception as exc:
            final_text = f"Jarvis hit an error: {exc}"

        self.last_response = final_text
        self.status = "Jarvis idle"
        self.speak(final_text)
        return final_text

    def speak(self, text):
        if not text or not self._tts_engine:
            return
        with self._tts_lock:
            try:
                self._tts_engine.say(text)
                self._tts_engine.runAndWait()
            except Exception:
                pass
