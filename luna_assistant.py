"""
Luna - A personal AI voice assistant for Windows.

Features:
- Time-based greeting
- Voice command recognition (speech_recognition) with fallback to text input
- Text-to-speech responses (pyttsx3) using female voice
- Open applications (Chrome, VS Code, YouTube, etc.)
- Web search (general + YouTube + custom queries)
- Date, time, and weather reporting (OpenWeatherMap; API key via env var OPENWEATHER_API_KEY)
- Notes taking, reminders, and playing songs (YouTube search)
- Learns preferences over time (tracks frequent commands & favorite topics/apps)
- Optional OpenAI integration stub (expand later)

Run directly: python luna_assistant.py

Author: (You)
"""
from __future__ import annotations
import speech_recognition as sr
import pyttsx3
import webbrowser
import threading
import time
import json
import os
import random
import pickle
import subprocess
import sys
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import requests
from urllib.parse import quote
from difflib import get_close_matches
from queue import Queue, Empty
import csv

# Optional dependencies (used if available)
try:
    import spacy  # type: ignore
    _NLP = None  # lazy-loaded
except Exception:
    spacy = None
    _NLP = None

try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw, ImageTk  # type: ignore
except Exception:
    pystray = None
    Image = None
    ImageDraw = None
    ImageTk = None

# Optional built-in UI (overlay) using tkinter
try:
    import tkinter as tk
except Exception:
    tk = None

# =============================
# Configuration Constants
# =============================
APP_MAP = {
    "chrome": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "google chrome": r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "chrome x86": r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "vscode": r"C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
    "visual studio code": r"C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
    "notepad": r"notepad.exe",
    "calculator": r"calc.exe",
}

# Common websites to open in Chrome with your profile
SITE_URLS: Dict[str, str] = {
    "spotify": "https://open.spotify.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chat.openai.com",
}

DATA_DIR = Path(__file__).parent
PREFERENCES_FILE = DATA_DIR / "preferences.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
NOTES_FILE = DATA_DIR / "notes.txt"
TRAINING_CSV = DATA_DIR / "Luna_Chatbot_Training_Data_Expanded.csv"

# Optional assets for icon/overlay (place these files next to luna_assistant.py)
ASSET_SABER = DATA_DIR / "saber.jpg"
ASSET_EXCALIBUR = DATA_DIR / "excalibur.png"

# Local music configuration
MUSIC_DIR = Path(os.environ.get("LUNA_MUSIC_DIR", str(Path.home() / "Music")))
MUSIC_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a"}

WEATHER_API_ENV = "OPENWEATHER_API_KEY"
DEFAULT_CITY_KEY = "default_city"

WAKE_WORDS = ["luna", "hey luna", "ok luna"]  # Could be expanded

# =============================
# Utility Functions
# =============================

def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        save_json(path, default)
        return default.copy()
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default.copy()


def save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Failed to save {path.name}: {e}")


def append_note(text: str) -> None:
    try:
        with NOTES_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {text}\n")
    except Exception as e:
        print(f"[WARN] Failed to write note: {e}")

# =============================
# Speech / TTS Setup
# =============================
class SpeechEngine:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.microphone_available = False
        self.device_index = get_configured_mic_index()
        try:
            with sr.Microphone(device_index=self.device_index) as source:
                self.microphone_available = True
        except Exception:
            self.microphone_available = False
        # TTS worker thread to avoid re-entrancy and blocking issues
        self.tts = None  # main-thread engine unused; worker owns the engine
        self._tts_queue: Queue[str] = Queue()
        self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self._tts_thread.start()

    def _tts_worker(self):
        # Initialize COM for SAPI on this thread if available
        try:
            import pythoncom  # type: ignore
            pythoncom.CoInitialize()
        except Exception:
            pass
        # Initialize engine in this thread for COM affinity
        def init_engine():
            try:
                eng = pyttsx3.init(driverName='sapi5')
            except Exception:
                eng = pyttsx3.init()
            # configure from preferences
            try:
                self._configure_voice_engine(eng)
            except Exception:
                pass
            return eng
        self._tts = init_engine()
        while True:
            try:
                text = self._tts_queue.get()
                if text is None:
                    # sentinel: restart engine
                    try:
                        self._tts = init_engine()
                    except Exception:
                        pass
                    continue
                # Mark speaking state
                global _is_speaking
                _is_speaking = True
                # Apply runtime volume each utterance
                try:
                    vol = float(preferences.get('tts_volume', 1.0))
                    vol = max(0.0, min(1.0, vol))
                    self._tts.setProperty('volume', vol)
                except Exception:
                    pass
                # Apply preferred voice and rate if configured
                try:
                    vid = preferences.get('tts_voice_id')
                    if vid:
                        self._tts.setProperty('voice', vid)
                    base_rate = self._tts.getProperty('rate') or 200
                    rate_mul = float(preferences.get('tts_rate', 0.95))
                    self._tts.setProperty('rate', int(base_rate * rate_mul))
                except Exception:
                    pass
                backend = preferences.get('tts_backend', 'pyttsx3')
                if preferences.get('tts_debug', False):
                    print(f"[TTS] backend={backend} len={len(text)}")
                if backend == 'sapi' or backend == 'sapi5':
                    # Direct SAPI fallback using win32com
                    try:
                        import win32com.client  # type: ignore
                        sapi = win32com.client.Dispatch("SAPI.SpVoice")
                        # Set volume and rate
                        try:
                            vol_pct = int(preferences.get('tts_volume', 1.0) * 100)
                            rate_mul = float(preferences.get('tts_rate', 0.95))
                            # Map multiplier to -10..10 more sensitively
                            rate_sapi = int(max(-10, min(10, (rate_mul - 1.0) * 20)))
                            sapi.Volume = vol_pct
                            sapi.Rate = rate_sapi
                        except Exception:
                            pass
                        # Try to select a female voice
                        try:
                            # Prefer young female if available
                            tokens = sapi.GetVoices("Gender=Female;Age=Teen", "")
                            if tokens is None or tokens.Count == 0:
                                tokens = sapi.GetVoices("Gender=Female;Age=Child", "")
                            if tokens is None or tokens.Count == 0:
                                tokens = sapi.GetVoices("Gender=Female", "")
                            chosen = None
                            # Prefer common names
                            prefer = ['aria', 'jenny', 'hazel', 'zira']
                            if tokens and tokens.Count > 0:
                                for i in range(tokens.Count):
                                    desc = tokens.Item(i).GetDescription()
                                    if any(p in desc.lower() for p in prefer):
                                        chosen = tokens.Item(i)
                                        break
                                if chosen is None:
                                    chosen = tokens.Item(0)
                                sapi.Voice = chosen
                        except Exception:
                            pass
                        sapi.Speak(text)
                    except Exception:
                        # fallback to engine if SAPI unavailable
                        self._tts.say(text)
                        self._tts.runAndWait()
                    finally:
                        _is_speaking = False
                elif backend in ('ps', 'powershell'):
                    # PowerShell .NET SpeechSynthesizer fallback (no extra deps)
                    try:
                        import subprocess, shlex
                        safe = text.replace("'", "''")
                        vol_pct = int(preferences.get('tts_volume', 1.0) * 100)
                        rate_mul = float(preferences.get('tts_rate', 0.95))
                        # Map multiplier to -10..10 more sensitively
                        rate_ps = [int(max(-10, min(10, (rate_mul - 1.0) * 20)))]
                        ps_cmd = (
                            "[void][Reflection.Assembly]::LoadWithPartialName('System.Speech');"
                            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                            f"$s.Volume = {vol_pct};"
                            # Prefer a female voice if available
                            "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female, [System.Speech.Synthesis.VoiceAge]::Adult) } catch { }"
                            # Rate ranges -10..10 in .NET
                            f"$s.Rate = {int(max(-10, min(10, (float({preferences.get('tts_rate', 0.95)}) - 1.0) * 20)))};"
                            f"$s.Speak('{safe}');"
                        )
                        subprocess.run([
                            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_cmd
                        ], check=False)
                    except Exception:
                        # fallback to engine
                        self._tts.say(text)
                        self._tts.runAndWait()
                    finally:
                        _is_speaking = False
                else:
                    try:
                        self._tts.say(text)
                        self._tts.runAndWait()
                    finally:
                        _is_speaking = False
            except Exception:
                # Try to recover by reinitializing engine
                try:
                    self._tts = pyttsx3.init(driverName='sapi5')
                except Exception:
                    try:
                        self._tts = pyttsx3.init()
                    except Exception:
                        pass
                try:
                    self._configure_voice_engine(self._tts)
                except Exception:
                    pass

    def _configure_voice_engine(self, eng):
        # Attempt to select a female voice by default and hide male voices
        try:
            voices = eng.getProperty('voices')
        except Exception:
            voices = []
        def is_female(desc: str) -> bool:
            d = desc.lower()
            if any(word in d for word in [" male ", "david", "mark", "george", "adam", "brian"]):
                return False
            return any(word in d for word in ["female", "zira", "aria", "jenny", "hazel", "girl", "woman"]) or "female" in d
        female_voice_id = None
        female_list: List[Any] = []
        for v in voices or []:
            vdesc = (getattr(v, 'name', '') + " " + getattr(v, 'id', '') + " " + getattr(v, 'gender', '')).lower()
            if is_female(vdesc):
                female_list.append(v)
                if not female_voice_id:
                    female_voice_id = getattr(v, 'id', None)
        try:
            prefer_id = preferences.get('tts_voice_id') if 'preferences' in globals() and isinstance(preferences, dict) else None
        except Exception:
            prefer_id = None
        try:
            chosen = None
            if prefer_id:
                for v in voices or []:
                    if getattr(v, 'id', None) == prefer_id:
                        vdesc = (getattr(v, 'name', '') + " " + getattr(v, 'id', '') + " " + getattr(v, 'gender', '')).lower()
                        if is_female(vdesc):
                            chosen = prefer_id
                        break
            if not chosen and female_voice_id:
                chosen = female_voice_id
            if chosen:
                eng.setProperty('voice', chosen)
        except Exception:
            pass
        # Monkey patch engine to only return female voices for listing
        try:
            if female_list:
                original_get = eng.getProperty
                def female_only_get(prop):
                    if prop == 'voices':
                        return female_list
                    return original_get(prop)
                eng.getProperty = female_only_get  # type: ignore
        except Exception:
            pass
        # Apply rate and volume from preferences
        try:
            base_rate = eng.getProperty('rate') or 200
            rate_mul = float(preferences.get('tts_rate', 0.90)) if 'preferences' in globals() else 0.90
            # For ethereal/soft mode, do not cap speed; allow user preference, but nudge slightly softer if desired
            if preferences.get('tts_style') == 'ethereal':
                rate_mul = max(0.70, min(1.10, rate_mul))
            eng.setProperty('rate', int(base_rate * rate_mul))
        except Exception:
            pass
        try:
            vol = float(preferences.get('tts_volume', 1.0)) if 'preferences' in globals() else 1.0
            vol = max(0.0, min(1.0, vol))
            # For soft mode, respect user volume but cap gently to keep it soft
            if preferences.get('soft_mode', False) or preferences.get('tts_style') == 'ethereal':
                vol = min(vol, 0.85)
            eng.setProperty('volume', vol)
        except Exception:
            pass

    def request_tts_restart(self):
        try:
            self._tts_queue.put(None)
        except Exception:
            pass

    def listen(self, prompt: Optional[str] = None) -> str:
        if prompt:
            speak(prompt)
        if not self.microphone_available:
            # Fallback to text input
            return input("(Mic unavailable) Type your command: ").strip().lower()
        with sr.Microphone(device_index=self.device_index) as source:
            print("[INFO] Listening...")
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = self.recognizer.listen(source, phrase_time_limit=8)
        try:
            text = self.recognizer.recognize_google(audio)
            print(f"[HEARD] {text}")
            return text.lower().strip()
        except sr.UnknownValueError:
            speak("I didn't catch that. Could you repeat?")
            return ""
        except sr.RequestError:
            speak("Speech service seems down. Type instead.")
            return input("Type command: ").strip().lower()

# Global speech engine instance (initialized later in main)
_speech_engine: Optional[SpeechEngine] = None
listening_enabled = True
stop_flag = False
_last_activity: datetime = datetime.now()
_last_suggest: Optional[datetime] = None
_chat_history: List[Tuple[str,str]] = []  # (user, assistant) recent exchanges
_overlay_ui: Optional["OverlayUI"] = None  # global reference to overlay for speak()
_is_speaking: bool = False
_last_spoken_text: str = ""

# =============================
# ML Intent Classifier (optional)
# =============================
try:
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore
    from sklearn.linear_model import LogisticRegression  # type: ignore
    from sklearn.pipeline import Pipeline  # type: ignore
    _ML_AVAILABLE = True
except Exception:
    _ML_AVAILABLE = False

ML_MODEL_FILE = DATA_DIR / "intent_model.pkl"
_ml_pipeline: Optional[Pipeline] = None
_resp_vectorizer: Optional[TfidfVectorizer] = None
_resp_matrix = None  # type: ignore
_resp_texts: List[str] = []
_resp_responses: List[str] = []
_resp_labels: List[str] = []

def _build_training_samples() -> List[Tuple[str,str]]:
    # Minimal seed dataset; user can retrain/extend later.
    samples = [
        ("what time is it", "time"),
        ("tell me the time", "time"),
        ("current time", "time"),
        ("what is today's date", "date"),
        ("what date is it", "date"),
        ("weather", "weather"),
        ("weather in london", "weather"),
        ("open chrome", "open_app"),
        ("launch spotify", "open_app"),
        ("search cats doing parkour", "search"),
        ("google python decorators", "search"),
        ("note buy milk", "note"),
        ("take note project idea", "note"),
        ("remind me in 5 minutes to stretch", "reminder"),
        ("set reminder at 14:30 take a break", "reminder"),
        ("play song bohemian rhapsody", "play_song"),
        ("play despacito", "play_song"),
        ("list microphones", "list_mics"),
        ("use mic 1", "set_mic"),
        ("change theme fate", "set_theme"),
        ("enable auto theme", "toggle_theme_auto"),
        ("exit", "exit"),
        ("quit", "exit"),
        ("goodbye", "exit"),
        ("stop", "exit"),
    ]
    return samples

def _load_training_csv() -> Tuple[List[Tuple[str,str]], List[Tuple[str,str,str]]]:
    """Load external CSV training data if available.
    Returns: (samples_for_classifier, chat_pairs(text,response,label))"""
    samples: List[Tuple[str,str]] = []
    chat_pairs: List[Tuple[str,str,str]] = []
    if not TRAINING_CSV.exists():
        return samples, chat_pairs
    try:
        with open(TRAINING_CSV, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                text = (row.get('user_input_example') or '').strip().lower()
                label = (row.get('intent') or '').strip()
                resp = (row.get('bot_response') or '').strip()
                if not text or not label:
                    continue
                samples.append((text, label))
                if label in {'greeting','fun_conversation','general_conversation'} and resp:
                    chat_pairs.append((text, resp, label))
    except Exception as e:
        print(f"[WARN] Failed reading CSV: {e}")
    return samples, chat_pairs

def train_intent_model():
    global _ml_pipeline
    global _resp_vectorizer, _resp_matrix, _resp_texts, _resp_responses, _resp_labels
    if not _ML_AVAILABLE:
        return False, "ML libraries not installed."
    data = _build_training_samples()
    # augment with CSV if available
    csv_samples, chat_pairs = _load_training_csv()
    added = 0
    if csv_samples:
        data += csv_samples
        added = len(csv_samples)
    texts = [t for t,_ in data]
    labels = [l for _,l in data]
    _ml_pipeline = Pipeline([
        ("vec", TfidfVectorizer(ngram_range=(1,2), min_df=1)),
        ("clf", LogisticRegression(max_iter=500, n_jobs=None))
    ])
    _ml_pipeline.fit(texts, labels)
    # Build retrieval chat vectors
    try:
        _resp_texts = [t for t,_,_ in chat_pairs]
        _resp_responses = [r for _,r,_ in chat_pairs]
        _resp_labels = [l for _,_,l in chat_pairs]
        if _resp_texts:
            _resp_vectorizer = TfidfVectorizer(ngram_range=(1,2), min_df=1)
            _resp_matrix = _resp_vectorizer.fit_transform(_resp_texts)
        else:
            _resp_vectorizer = None
            _resp_matrix = None
    except Exception as e:
        print(f"[WARN] Retrieval build failed: {e}")
    try:
        with ML_MODEL_FILE.open('wb') as f:
            pickle.dump(_ml_pipeline, f)
    except Exception:
        pass
    return True, f"Intent model trained: {len(data)} samples total (added {added} from CSV)."

def load_intent_model():
    global _ml_pipeline
    global _resp_vectorizer, _resp_matrix, _resp_texts, _resp_responses, _resp_labels
    if not _ML_AVAILABLE:
        return False
    if ML_MODEL_FILE.exists():
        try:
            with ML_MODEL_FILE.open('rb') as f:
                _ml_pipeline = pickle.load(f)
            return True
        except Exception:
            _ml_pipeline = None
            return False
    # Load retrieval from CSV
    try:
        _, chat_pairs = _load_training_csv()
        _resp_texts = [t for t,_,_ in chat_pairs]
        _resp_responses = [r for _,r,_ in chat_pairs]
        _resp_labels = [l for _,_,l in chat_pairs]
        if _resp_texts:
            _resp_vectorizer = TfidfVectorizer(ngram_range=(1,2), min_df=1)
            _resp_matrix = _resp_vectorizer.fit_transform(_resp_texts)
        else:
            _resp_vectorizer = None
            _resp_matrix = None
    except Exception as e:
        print(f"[WARN] Retrieval load failed: {e}")
    return False

load_intent_model()

# Overlay UI controller
class OverlayUI:
    """Always-visible compact overlay in top-right with pulsing glow.
    Replaces prior transient show/hide behavior."""
    def __init__(self):
        self.enabled = tk is not None
        self.root = None
        self.label = None
        self._photo_icon = None
        self._photo_logo = None
        self._glow_canvas = None
        self.visible = False
        self._pulse_active = False
        self._pulse_step = 0
        self._listening = False
        self._pending_text = "Initializing Luna…"
        self._drag_dx = 0
        self._drag_dy = 0
        self._fade_job = None
        self._cur_alpha = THEMES.get(get_theme_name(), THEMES['default']).get('alpha', 0.95)
        self._theme_name = get_theme_name()
        self._theme = THEMES.get(self._theme_name, THEMES['default'])
        self._idle_glow = tuple(self._theme.get('idle_glow', (70, 70, 90)))
        self._active_glow = tuple(self._theme.get('active_glow', (120, 86, 255)))
        self._bg_label = None
        self._bg_image = None
        if not self.enabled:
            return
        threading.Thread(target=self._run_ui, daemon=True).start()

    def _run_ui(self):
        if tk is None:
            return
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=self._theme.get('bg', '#1f1f23'))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self._w, self._h = 280, 70
        px = preferences.get('overlay_x')
        py = preferences.get('overlay_y')
        if isinstance(px, int) and isinstance(py, int):
            x = max(0, min(screen_w - self._w, px))
            y = max(0, min(screen_h - self._h, py))
        else:
            x = screen_w - self._w - 12
            y = 12
        self.root.geometry(f"{self._w}x{self._h}+{x}+{y}")
        try:
            self.root.attributes('-alpha', self._cur_alpha)
        except Exception:
            pass

        frame = tk.Frame(self.root, bg=self._theme.get('bg', '#1f1f23'))
        frame.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(frame, bg=self._theme.get('bg', '#1f1f23'))
        top.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)

        # Background gradient with rounded corners (Fate theme)
        self._build_background(self._w, self._h)

        # Glow canvas (larger to avoid crowding the icon)
        if Image is not None and ImageTk is not None:
            self._glow_canvas = tk.Canvas(top, width=54, height=54, bg=self._theme.get('bg', '#1f1f23'), highlightthickness=0)
            self._glow_canvas.pack(side=tk.LEFT)
        # starfield particles behind icon (kept subtle)
        self._particles = []
        self._init_particles(6)

        # Icon image or fallback letter
        placed_icon = False
        if Image is not None and ImageTk is not None and ASSET_SABER.exists():
            try:
                self._icon_size = 40
                self._icon_center = (27, 27)
                img = Image.open(ASSET_SABER).convert("RGBA").resize((self._icon_size, self._icon_size), Image.LANCZOS)
                mask = Image.new("L", (36, 36), 0)
                # regenerate mask to icon size
                mask = Image.new("L", (self._icon_size, self._icon_size), 0)
                mdraw = ImageDraw.Draw(mask)
                mdraw.ellipse((0, 0, self._icon_size, self._icon_size), fill=255)
                circ = Image.new("RGBA", (self._icon_size, self._icon_size))
                circ.paste(img, (0, 0), mask)
                self._photo_icon = ImageTk.PhotoImage(circ)
                if self._glow_canvas is not None:
                    self._glow_canvas.create_image(self._icon_center[0], self._icon_center[1], image=self._photo_icon)
                placed_icon = True
            except Exception:
                pass
        if not placed_icon:
            fallback = tk.Label(top, text="L", fg=self._theme.get('fg', '#ffffff'), bg=self._theme.get('bg', '#1f1f23'), font=("Segoe UI", 18, "bold"))
            fallback.pack(side=tk.LEFT, padx=(4, 8))

        self.label = tk.Label(top, text=self._pending_text, fg=self._theme.get('fg', '#eaeaea'), bg=self._theme.get('bg', '#1f1f23'),
                              font=self._theme.get('font', ("Segoe UI", 11, "bold")), wraplength=180, justify="left")
        self.label.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Settings gear
        try:
            self._gear = tk.Label(top, text="⚙", fg=self._theme.get('gear', '#c8c8d0'), bg=self._theme.get('bg', '#1f1f23'), font=("Segoe UI Symbol", 12))
        except Exception:
            self._gear = tk.Label(top, text="*", fg=self._theme.get('gear', '#c8c8d0'), bg=self._theme.get('bg', '#1f1f23'), font=("Segoe UI", 12))
        self._gear.pack(side=tk.RIGHT, padx=(6, 2))
        self._gear.bind('<Button-1>', self._on_gear_click)

        if Image is not None and ImageTk is not None and ASSET_EXCALIBUR.exists():
            try:
                logo = Image.open(ASSET_EXCALIBUR).convert("RGBA")
                ratio = min(200 / logo.width, 20 / logo.height, 1.0)
                logo = logo.resize((int(logo.width * ratio), int(logo.height * ratio)), Image.LANCZOS)
                self._photo_logo = ImageTk.PhotoImage(logo)
                tk.Label(frame, image=self._photo_logo, bg=self._theme.get('bg', '#1f1f23')).pack(side=tk.BOTTOM, pady=(0, 4))
            except Exception:
                pass

        self.visible = True
        self._pulse_active = True
        self._animate_pulse()
        # launch auto theme periodic check
        def auto_tick():
            self._apply_auto_theme()
            try:
                self.root.after(300000, auto_tick)  # every 5 min
            except Exception:
                pass
        auto_tick()

        # Drag bindings (anywhere on the overlay)
        for widget in (self.root, frame, top, self._glow_canvas, self.label, self._gear):
            try:
                if widget is not None:
                    widget.bind('<ButtonPress-1>', self._on_press)
                    widget.bind('<B1-Motion>', self._on_motion)
                    widget.bind('<ButtonRelease-1>', self._on_release)
            except Exception:
                pass
        self.root.mainloop()

    def _animate_pulse(self):
        if not self._pulse_active or self._glow_canvas is None:
            return
        self._pulse_step = (self._pulse_step + 1) % 60
        phase = self._pulse_step / 60.0
        if self._listening:
            base = self._active_glow
            delta = int(60 * (0.5 - abs(phase - 0.5)) * 2)
            col = (base[0], min(255, base[1] + delta), base[2])
        else:
            base = self._idle_glow
            delta = int(40 * (0.5 - abs(phase - 0.5)) * 2)
            col = (base[0] + delta, base[1] + delta // 2, base[2] + delta)
        hex_col = f"#{col[0]:02x}{col[1]:02x}{col[2]:02x}"
        try:
            # redraw oval (clear then new)
            self._glow_canvas.delete("all")
            # draw particles first (behind)
            self._update_particles()
            # draw pulsing ring (outline only, doesn't obscure icon)
            ring_w = 3 + int(2 * (0.5 - abs(phase - 0.5)) * 2)
            self._glow_canvas.create_oval(5, 5, 49, 49, outline=hex_col, width=ring_w)
            # finally draw icon on top, centered
            cx, cy = getattr(self, '_icon_center', (27, 27))
            if self._photo_icon is not None:
                self._glow_canvas.create_image(cx, cy, image=self._photo_icon)
        except Exception:
            pass
        if self.root is not None:
            self.root.after(50, self._animate_pulse)

    def _call(self, fn):
        if self.root is not None:
            try:
                self.root.after(0, fn)
            except Exception:
                pass

    def set_status(self, text: str, listening: bool = False):
        if not self.enabled:
            return
        def do_update():
            self._listening = listening
            # Fade out slightly, update text, fade back in
            self._fade_sequence(text)
        self._call(do_update)

    # Backwards compatibility for existing calls
    def show(self, text: str = "Listening..."):
        self.set_status(text, listening=True)

    def hide(self):
        # No longer hides; keeps always-on overlay
        pass

    # ----- Theme application -----
    def apply_theme_by_name(self, name: str):
        if name not in THEMES:
            return
        self._theme_name = name
        self._theme = THEMES[name]
        self._idle_glow = tuple(self._theme.get('idle_glow', (70, 70, 90)))
        self._active_glow = tuple(self._theme.get('active_glow', (120, 86, 255)))
        self._cur_alpha = float(self._theme.get('alpha', self._cur_alpha))
        self.apply_theme()

    def apply_theme(self):
        try:
            if self.root is None:
                return
            # Window + alpha
            try:
                self.root.attributes('-alpha', self._cur_alpha)
            except Exception:
                pass
            bg = self._theme.get('bg', '#1f1f23')
            self.root.configure(bg=bg)
            for w in (self.label,):
                if w is not None:
                    try:
                        w.configure(bg=bg, fg=self._theme.get('fg', '#eaeaea'), font=self._theme.get('font', ("Segoe UI", 11, "bold")))
                    except Exception:
                        pass
            if self._gear is not None:
                try:
                    self._gear.configure(bg=bg, fg=self._theme.get('gear', '#c8c8d0'))
                except Exception:
                    pass
            if self._glow_canvas is not None:
                try:
                    self._glow_canvas.configure(bg=bg)
                except Exception:
                    pass
            # Background image
            self._build_background(280, 70)
            # Repaint particles to harmonize with new colors
            self._init_particles(len(getattr(self, '_particles', []) or []) or 20)
        except Exception:
            pass

    # Auto theme switching
    def _apply_auto_theme(self, force: bool = False):
        try:
            if not preferences.get('theme_auto', False) and not force:
                return
            hour = datetime.now().hour
            target = 'fate-light' if 7 <= hour < 18 else 'fate'
            if self._theme_name != target:
                set_theme_name(target)
                self.apply_theme_by_name(target)
        except Exception:
            pass

    def _build_background(self, width: int, height: int):
        try:
            if Image is None or ImageTk is None:
                return
            bg1 = self._theme.get('bg1')
            bg2 = self._theme.get('bg2')
            border = self._theme.get('border', '#3a3a4a')
            if not bg1 or not bg2:
                # Solid background, optional border via canvas line not used
                return
            # Create gradient rounded background
            img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
            # vertical gradient
            from_color = tuple(int(bg1.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            to_color = tuple(int(bg2.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
            for y in range(height):
                t = y / max(1, height - 1)
                r = int(from_color[0] + (to_color[0] - from_color[0]) * t)
                g = int(from_color[1] + (to_color[1] - from_color[1]) * t)
                b = int(from_color[2] + (to_color[2] - from_color[2]) * t)
                ImageDraw.Draw(img).line([(0, y), (width, y)], fill=(r, g, b, 255))
            # rounded mask
            mask = Image.new('L', (width, height), 0)
            md = ImageDraw.Draw(mask)
            radius = 12
            md.rounded_rectangle((0, 0, width-1, height-1), radius=radius, fill=255)
            img.putalpha(mask)
            # gold trim
            md = ImageDraw.Draw(img)
            md.rounded_rectangle((1, 1, width-2, height-2), radius=radius, outline=border, width=2)
            self._bg_image = ImageTk.PhotoImage(img)
            if self._bg_label is None:
                self._bg_label = tk.Label(self.root, image=self._bg_image, bd=0)
                self._bg_label.place(x=0, y=0, width=width, height=height)
                self._bg_label.lower()  # send behind other widgets
            else:
                self._bg_label.configure(image=self._bg_image)
        except Exception:
            pass

    # ----- Particles -----
    def _init_particles(self, n: int):
        try:
            self._particles = []
            for _ in range(n):
                self._particles.append({
                    'x': random.uniform(8, 38),
                    'y': random.uniform(8, 38),
                    'dx': random.uniform(-0.2, 0.2),
                    'dy': random.uniform(-0.2, 0.2),
                    'r': random.uniform(0.8, 1.8),
                    'b': random.uniform(0.5, 1.0),
                })
        except Exception:
            self._particles = []

    def _update_particles(self):
        if self._glow_canvas is None:
            return
        for p in self._particles:
            p['x'] += p['dx']
            p['y'] += p['dy']
            if p['x'] < 6 or p['x'] > 40:
                p['dx'] *= -1
            if p['y'] < 6 or p['y'] > 40:
                p['dy'] *= -1
            p['b'] += random.uniform(-0.05, 0.05)
            p['b'] = max(0.3, min(1.0, p['b']))
            base = self._active_glow if self._listening else self._idle_glow
            r = int(base[0] * 0.6 * p['b'])
            g = int(base[1] * 0.6 * p['b'])
            b = int(base[2] * 0.8 * p['b'])
            color = f"#{r:02x}{g:02x}{b:02x}"
            x, y, rad = p['x'], p['y'], p['r']
            try:
                self._glow_canvas.create_oval(x-rad, y-rad, x+rad, y+rad, fill=color, outline="")
            except Exception:
                pass

    # ----- Dragging -----
    def _on_press(self, event):
        try:
            self._drag_dx = event.x_root - self.root.winfo_x()
            self._drag_dy = event.y_root - self.root.winfo_y()
        except Exception:
            self._drag_dx = self._drag_dy = 0

    def _on_motion(self, event):
        try:
            x = max(0, event.x_root - self._drag_dx)
            y = max(0, event.y_root - self._drag_dy)
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _on_release(self, event):
        # Save position to preferences on drag release
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            preferences['overlay_x'] = int(x)
            preferences['overlay_y'] = int(y)
            save_json(PREFERENCES_FILE, preferences)
        except Exception:
            pass

    # ----- Gear menu -----
    def _on_gear_click(self, event):
        try:
            menu = tk.Menu(self.root, tearoff=0)
            # Microphones submenu
            mic_menu = tk.Menu(menu, tearoff=0)
            try:
                names = sr.Microphone.list_microphone_names()
            except Exception:
                names = []
            mic_menu.add_command(label="Use default input", command=lambda: self._select_mic(-1))
            if names:
                for i, name in enumerate(names):
                    label = f"{i}: {name[:40]}" if len(name) > 40 else f"{i}: {name}"
                    mic_menu.add_command(label=label, command=lambda i=i: self._select_mic(i))
            else:
                mic_menu.add_command(label="(No devices)", state='disabled')
            menu.add_cascade(label="Microphones", menu=mic_menu)

            # Chrome Profile submenu
            prof_menu = tk.Menu(menu, tearoff=0)
            prof_menu.add_command(label="Set profile name…", command=self._prompt_profile_name)
            prof_menu.add_command(label="Set profile dir…", command=self._prompt_profile_dir)
            current = preferences.get('chrome_profile_name') or preferences.get('chrome_profile') or '(default)'
            prof_menu.add_command(label=f"Current: {current}", state='disabled')
            menu.add_cascade(label="Chrome Profile", menu=prof_menu)

            # Theme submenu
            theme_menu = tk.Menu(menu, tearoff=0)
            theme_var = tk.StringVar(value=self._theme_name)
            for key in ("default", "fate", "fate-light"):
                theme_menu.add_radiobutton(label=key.title(), value=key, variable=theme_var,
                                           command=lambda k=key: self._on_set_theme(k))
            theme_menu.add_separator()
            # Auto toggle
            auto_state = preferences.get('theme_auto', False)
            def _toggle_auto():
                preferences['theme_auto'] = not preferences.get('theme_auto', False)
                save_json(PREFERENCES_FILE, preferences)
                if preferences['theme_auto']:
                    self._apply_auto_theme(force=True)
                    speak("Auto theme enabled.")
                else:
                    speak("Auto theme disabled.")
            theme_menu.add_checkbutton(label="Auto (Day/Night)", onvalue=True, offvalue=False,
                                       variable=tk.BooleanVar(value=auto_state), command=_toggle_auto)
            cur = get_theme_name()
            theme_menu.add_command(label=f"Current: {cur}", state='disabled')
            menu.add_cascade(label="Theme", menu=theme_menu)

            menu.tk_popup(event.x_root, event.y_root)
            try:
                menu.grab_release()
            except Exception:
                pass
        except Exception:
            pass

    def _select_mic(self, idx: int):
        try:
            if idx < 0:
                preferences['mic_index'] = -1
                save_json(PREFERENCES_FILE, preferences)
                if _speech_engine is not None:
                    _speech_engine.refresh_mic()
                speak("Using default input device.")
                self.set_status("Ready.")
                return
            set_configured_mic_index(idx)
            if _speech_engine is not None:
                _speech_engine.refresh_mic()
            if _speech_engine is not None and _speech_engine.microphone_available:
                speak(f"Microphone set to device {idx}.")
            else:
                speak(f"Saved mic {idx}, but it's not available.")
            self.set_status("Ready.")
        except Exception as e:
            speak(f"Mic change failed: {e}")

    def _prompt_profile_name(self):
        self._prompt_entry("Chrome profile name", "Enter display name (e.g., John Doe):", self._save_profile_name)

    def _save_profile_name(self, name: str):
        name = (name or '').strip()
        if not name:
            return
        preferences['chrome_profile_name'] = name
        save_json(PREFERENCES_FILE, preferences)
        speak(f"Chrome profile name set to {name}.")

    def _prompt_profile_dir(self):
        self._prompt_entry("Chrome profile dir", "Enter profile directory (e.g., Default, Profile 1):", self._save_profile_dir)

    def _save_profile_dir(self, directory: str):
        directory = (directory or '').strip()
        if not directory:
            return
        preferences['chrome_profile'] = directory
        save_json(PREFERENCES_FILE, preferences)
        speak(f"Chrome profile directory set to {directory}.")

    def _prompt_entry(self, title: str, label_text: str, on_submit):
        try:
            win = tk.Toplevel(self.root)
            win.title(title)
            win.transient(self.root)
            try:
                win.attributes('-topmost', True)
            except Exception:
                pass
            frm = tk.Frame(win, padx=10, pady=10)
            frm.pack(fill=tk.BOTH, expand=True)
            tk.Label(frm, text=label_text).pack(anchor='w')
            entry = tk.Entry(frm, width=32)
            entry.pack(fill=tk.X, pady=(6, 10))
            entry.focus_set()
            btns = tk.Frame(frm)
            btns.pack(fill=tk.X)
            def ok():
                val = entry.get()
                win.destroy()
                try:
                    on_submit(val)
                except Exception as e:
                    speak(f"Failed to apply: {e}")
            def cancel():
                win.destroy()
            tk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT)
            tk.Button(btns, text="Cancel", command=cancel).pack(side=tk.RIGHT, padx=(0, 6))
        except Exception:
            pass

    def _on_set_theme(self, key: str):
        try:
            preferences['theme_auto'] = False
            save_json(PREFERENCES_FILE, preferences)
            set_theme_name(key)
            self.apply_theme_by_name(key)
            speak(f"Theme set to {key}.")
        except Exception as e:
            speak(f"Failed to set theme: {e}")

    # ----- Fade helpers -----
    def _fade_sequence(self, new_text: str):
        # fade a bit out, switch text, then fade back in
        self._fade_to(0.70, 100, after=lambda: self._set_text(new_text) or self._fade_to(0.95, 160))

    def _set_text(self, text: str):
        if self.label is not None:
            self.label.config(text=text)

    def _fade_to(self, target: float, duration_ms: int, after=None):
        if self.root is None:
            return
        steps = max(1, duration_ms // 20)
        start = self._cur_alpha
        delta = (target - start) / steps
        if self._fade_job is not None:
            try:
                self.root.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None
        def step(i=0):
            nonlocal steps, start, delta
            try:
                a = start + delta * i
                self.root.attributes('-alpha', a)
                self._cur_alpha = a
            except Exception:
                pass
            if i < steps:
                self._fade_job = self.root.after(20, step, i + 1)
            else:
                self._fade_job = None
                if after:
                    try:
                        after()
                    except Exception:
                        pass
        step()

def speak(message: str):
    print(f"Luna: {message}")
    # Mirror output in overlay if available
    global _overlay_ui
    global _last_spoken_text
    try:
        if _overlay_ui is not None:
            preview = message if len(message) <= 80 else message[:77] + "..."
            _overlay_ui.set_status(preview, listening=False)
    except Exception:
        pass
    # honor TTS preference
    if _speech_engine and preferences.get('tts_enabled', True):
        try:
            # Enqueue for background TTS to avoid blocking/COM reentrancy
            _speech_engine._tts_queue.put(message)
            if preferences.get('tts_debug', False):
                try:
                    vid = _speech_engine.tts.getProperty('voice')
                    rate = _speech_engine.tts.getProperty('rate')
                    volp = _speech_engine.tts.getProperty('volume')
                    print(f"[TTS] enqueued | voice={vid} rate={rate} volume={volp}")
                except Exception:
                    pass
            _last_spoken_text = message.lower().strip()
        except Exception as e:
            print(f"[WARN] TTS failed: {e}")
    # update last activity
    global _last_activity
    _last_activity = datetime.now()

# =============================
# Preference & Learning Logic
# =============================
PREFERENCES_DEFAULT = {
    "usage_counts": {},  # intent -> count
    "favorite_topics": {},  # keyword -> count
    "favorite_apps": {},  # app name -> count
    DEFAULT_CITY_KEY: "London",  # default city
    "chrome_profile": "abhay_k",  # default Chrome profile (directory name)
    "chrome_profile_name": "",  # visible profile name (e.g., Abhay Kumar)
    "mic_index": -1,  # -1 means default input device
    "theme": "default",  # UI theme
    "overlay_x": None,  # saved overlay position X
    "overlay_y": None,  # saved overlay position Y
    "theme_auto": False,  # auto day/night theme switching
    "tts_enabled": True,  # allow speaking responses
    "tts_rate": 0.80,  # slower by default for ethereal tone
    "tts_volume": 0.9,  # slightly reduced default volume
    "tts_voice_id": None,  # preferred voice id
    "tts_backend": "sapi",  # prefer direct SAPI for low latency
    "suggest_show": False,  # whether to print suggestions to console
    "suggest_speak": False,  # whether idle suggestions are spoken aloud
    "tts_style": "ethereal",  # default | ethereal (start ethereal)
    "soft_mode": True,  # additional softening flag enabled
}

preferences = load_json(PREFERENCES_FILE, PREFERENCES_DEFAULT)
# Ensure TTS keys exist and set a stable backend by default if missing
try:
    migrated = False
    if 'tts_enabled' not in preferences:
        preferences['tts_enabled'] = True
        migrated = True
    # Migrate away from slow PowerShell backend unless explicitly chosen later
    if preferences.get('tts_backend') == 'ps' or 'tts_backend' not in preferences:
        preferences['tts_backend'] = 'sapi'
        migrated = True
    # Default to ethereal soft style unless user already set something else
    if preferences.get('tts_style') not in ('ethereal', 'default'):
        preferences['tts_style'] = 'ethereal'
        migrated = True
    if 'soft_mode' not in preferences:
        preferences['soft_mode'] = True
        migrated = True
    if 'tts_rate' not in preferences or preferences.get('tts_rate', 1.0) > 0.9:
        preferences['tts_rate'] = 0.80
        migrated = True
    if 'suggest_show' not in preferences:
        preferences['suggest_show'] = False
        migrated = True
    if migrated:
        save_json(PREFERENCES_FILE, preferences)
except Exception:
    pass


# =============================
# Themes (UI/UX)
# =============================

THEMES: Dict[str, Dict[str, Any]] = {
    "default": {
        "bg": "#1f1f23",
        "fg": "#eaeaea",
        "border": "#3a3a4a",
        "alpha": 0.95,
        "idle_glow": (70, 70, 90),
        "active_glow": (120, 86, 255),
        "gear": "#c8c8d0",
        "font": ("Segoe UI", 11, "bold"),
    },
    # Fate series-inspired: deep navy/purple gradient with gold trim
    "fate": {
        "bg1": "#151728",
        "bg2": "#232542",
        "fg": "#e9e6ff",
        "border": "#c69c6d",  # gold
        "alpha": 0.96,
        "idle_glow": (90, 80, 140),
        "active_glow": (173, 132, 255),
        "gear": "#e7d8ad",
        "font": ("Garamond", 12, "bold"),
    },
    # Fate light (daytime) variant: parchment + subtle violet accents
    "fate-light": {
        "bg1": "#f7f5ef",
        "bg2": "#e7e3d8",
        "fg": "#1e1a2e",
        "border": "#c69c6d",  # same gold trim
        "alpha": 0.97,
        "idle_glow": (210, 190, 255),
        "active_glow": (180, 110, 255),
        "gear": "#7a5d2f",
        "font": ("Garamond", 12, "bold"),
    },
}

def get_theme_name() -> str:
    name = preferences.get("theme", "default")
    return name if name in THEMES else "default"

def set_theme_name(name: str):
    if name not in THEMES:
        return
    preferences["theme"] = name
    save_json(PREFERENCES_FILE, preferences)


def track_usage(intent: str, extra: Optional[str] = None):
    preferences['usage_counts'][intent] = preferences['usage_counts'].get(intent, 0) + 1
    if intent == 'search' and extra:
        # break query into keywords
        for word in re.findall(r"[a-zA-Z]{4,}", extra.lower()):
            preferences['favorite_topics'][word] = preferences['favorite_topics'].get(word, 0) + 1
    if intent == 'open_app' and extra:
        preferences['favorite_apps'][extra] = preferences['favorite_apps'].get(extra, 0) + 1
    save_json(PREFERENCES_FILE, preferences)


def get_top_items(d: Dict[str, int], n: int = 3) -> List[str]:
    return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]]

# =============================
# Microphone utilities
# =============================

def get_configured_mic_index() -> Optional[int]:
    env_val = os.environ.get("LUNA_MIC_INDEX")
    if env_val is not None:
        try:
            i = int(env_val)
            return i if i >= 0 else None
        except ValueError:
            pass
    pref_val = preferences.get("mic_index", -1)
    if isinstance(pref_val, int) and pref_val >= 0:
        return pref_val
    return None


def set_configured_mic_index(idx: int):
    preferences["mic_index"] = int(idx)
    save_json(PREFERENCES_FILE, preferences)


def list_microphones() -> List[str]:
    try:
        names = sr.Microphone.list_microphone_names()
        return [f"{i}: {name}" for i, name in enumerate(names)]
    except Exception as e:
        return [f"Error listing microphones: {e}"]

# =============================
# Greeting
# =============================

def time_based_greeting() -> str:
    hour = datetime.now().hour
    if hour < 5:
        return "Up late again. I'm here if you need anything."
    elif hour < 12:
        return "Good morning."  # concise
    elif hour < 18:
        return "Good afternoon."  # concise
    elif hour < 22:
        return "Good evening."  # concise
    else:
        return "Late night. I'm awake."  # concise

# =============================
# Weather
# =============================

def get_weather(city: str) -> str:
    api_key = os.environ.get(WEATHER_API_ENV)
    if not api_key:
        return f"Weather API key not set (env {WEATHER_API_ENV})."
    try:
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric"
        resp = requests.get(url, timeout=6)
        if resp.status_code != 200:
            return f"Couldn't fetch weather for {city}. ({resp.status_code})"
        data = resp.json()
        desc = data['weather'][0]['description']
        temp = data['main']['temp']
        feels = data['main']['feels_like']
        return f"Weather in {city}: {desc}, {temp:.0f}°C (feels {feels:.0f}°C)."
    except Exception as e:
        return f"Weather lookup failed: {e}"

# =============================
# Reminders
# =============================
REMINDER_POLL_INTERVAL = 5  # seconds
_reminder_lock = threading.Lock()


def load_reminders() -> List[Dict[str, Any]]:
    if not REMINDERS_FILE.exists():
        with REMINDERS_FILE.open('w', encoding='utf-8') as f:
            json.dump([], f)
        return []
    try:
        with REMINDERS_FILE.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_reminders(reminders: List[Dict[str, Any]]):
    try:
        with REMINDERS_FILE.open('w', encoding='utf-8') as f:
            json.dump(reminders, f, indent=2)
    except Exception as e:
        print(f"[WARN] Failed saving reminders: {e}")

reminders_cache = load_reminders()


def add_reminder(message: str, when: datetime):
    with _reminder_lock:
        reminders_cache.append({"message": message, "when": when.isoformat()})
        save_reminders(reminders_cache)
    speak(f"Reminder set for {when.strftime('%H:%M:%S')}: {message}")


def parse_reminder(command: str) -> Optional[Dict[str, Any]]:
    # Examples: "remind me in 5 minutes to stretch", "set reminder at 14:30 take a break"
    # Relative pattern
    rel = re.search(r"remind me in (\d+) (second|seconds|minute|minutes|hour|hours) (.*)", command)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2)
        rest = rel.group(3).strip()
        if rest.startswith("to "):
            rest = rest[3:]
        delta = {
            'second': timedelta(seconds=amount),
            'seconds': timedelta(seconds=amount),
            'minute': timedelta(minutes=amount),
            'minutes': timedelta(minutes=amount),
            'hour': timedelta(hours=amount),
            'hours': timedelta(hours=amount),
        }[unit]
        when = datetime.now() + delta
        return {"message": rest, "when": when}
    # Absolute time pattern HH:MM
    abs_match = re.search(r"(remind me|set reminder) at (\d{1,2}):(\d{2}) (.*)", command)
    if abs_match:
        hour = int(abs_match.group(2))
        minute = int(abs_match.group(3))
        msg = abs_match.group(4).strip()
        if msg.startswith("to "):
            msg = msg[3:]
        now = datetime.now()
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when < now:
            when += timedelta(days=1)  # schedule next day
        return {"message": msg, "when": when}
    return None


def reminder_worker():
    while True:
        time.sleep(REMINDER_POLL_INTERVAL)
        now = datetime.now()
        with _reminder_lock:
            pending = []
            remaining = []
            for r in reminders_cache:
                when = datetime.fromisoformat(r['when'])
                if when <= now:
                    pending.append(r)
                else:
                    remaining.append(r)
            if pending:
                for r in pending:
                    speak(f"Reminder: {r['message']}")
                reminders_cache[:] = remaining
                save_reminders(reminders_cache)

# =============================
# Intent Parsing & Actions
# =============================

def resolve_chrome_path() -> Optional[str]:
    # Allow override via env var
    override = os.environ.get("LUNA_CHROME_PATH")
    if override and os.path.exists(os.path.expandvars(override)):
        return os.path.expandvars(override)
    candidates = [
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def get_chrome_local_state_path() -> Optional[Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "Google" / "Chrome" / "User Data" / "Local State"
    return p if p.exists() else None


def resolve_profile_dir_by_name(display_name: str) -> Optional[str]:
    """Map a visible Chrome profile name (e.g., "Abhay Kumar" or "abhay_k") to its directory (e.g., "Profile 1")."""
    path = get_chrome_local_state_path()
    if not path:
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        info_cache = (data.get("profile") or {}).get("info_cache") or {}
        target = display_name.strip().lower()
        for dir_name, meta in info_cache.items():
            name = str(meta.get("name", "")).strip().lower()
            gaia = str(meta.get("gaia_name", "")).strip().lower()
            if target and (target == name or target == gaia):
                return dir_name
    except Exception as e:
        print(f"[WARN] Could not resolve Chrome profile by name: {e}")
    return None


def get_preferred_profile_dir() -> str:
    # 1) explicit directory via env
    explicit_dir = os.environ.get("LUNA_CHROME_PROFILE")
    if explicit_dir:
        return explicit_dir
    # 2) resolve by visible name via env
    display_name = os.environ.get("LUNA_CHROME_PROFILE_NAME")
    if display_name:
        resolved = resolve_profile_dir_by_name(display_name)
        if resolved:
            return resolved
    # 2b) resolve by visible name via preferences
    pref_name = preferences.get("chrome_profile_name")
    if pref_name:
        resolved = resolve_profile_dir_by_name(pref_name)
        if resolved:
            return resolved
    # 3) preferences fallback
    return preferences.get("chrome_profile", "Default")


def open_chrome_url(url: str, profile: Optional[str] = None) -> str:
    profile = profile or get_preferred_profile_dir()
    chrome_path = resolve_chrome_path()
    args = []
    if profile:
        args.append(f"--profile-directory={profile}")
    try:
        if chrome_path and os.path.exists(chrome_path):
            subprocess.Popen([chrome_path, *args, url], shell=False)
        else:
            # Fallback: rely on PATH 'chrome' with Windows start
            cmd = f'start "" chrome {" ".join(args)} "{url}"'
            subprocess.Popen(cmd, shell=True)
        # Keep speech brief; UI can show details
        return "Opening Chrome."
    except Exception as e:
        return f"Failed to open Chrome: {e}"


def open_app(app_name: str) -> str:
    # Expand environment variables
    app_key = app_name.lower()
    # Site shortcuts: open in Chrome with profile
    if app_key in SITE_URLS:
        return open_chrome_url(SITE_URLS[app_key])
    path = APP_MAP.get(app_key)
    if path:
        path = os.path.expandvars(path)
        if os.path.exists(path) or path.lower().endswith('.exe'):
            try:
                subprocess.Popen([path], shell=False)
                return "Opening app."
            except Exception as e:
                return f"Failed to open {app_name}: {e}"
        else:
            return f"Path for {app_name} not found on this system."
    # Fallback: try startfile with guessed executable
    guess = app_name + ".exe"
    try:
        os.startfile(guess)
        return "Opening app."
    except Exception:
        return f"I don't know how to open {app_name} yet."


def perform_search(query: str) -> str:
    if not query:
        return "Need something to search for."
    if query.startswith("youtube "):
        q = query[len("youtube "):].strip()
        url = f"https://www.youtube.com/results?search_query={quote(q)}"
        webbrowser.open(url)
    return "Searching YouTube."
    # general search
    url = f"https://www.google.com/search?q={quote(query)}"
    webbrowser.open(url)
    return "Searching the web."


def play_song(query: str) -> str:
    if not query:
        return "Provide a song or artist name."
    # Try local music first
    local_matches = find_local_tracks(query)
    if local_matches:
        try:
            os.startfile(str(local_matches[0]))
            return f"Playing {local_matches[0].name} from your library."
        except Exception as e:
            print(f"[WARN] Local play failed: {e}")
    # Fallback to YouTube search
    url = f"https://www.youtube.com/results?search_query={quote(query + ' song')}"
    webbrowser.open(url)
    return f"Opening YouTube results for {query}."


def integrate_openai(prompt: str) -> str:
    # Placeholder: Provide instructions
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI not configured. Set OPENAI_API_KEY env variable to enable advanced responses."
    try:
        import openai  # type: ignore
        openai.api_key = api_key
        # Minimal example (ChatCompletion or new responses depending on SDK version)
        completion = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": "You are Luna, a witty helpful assistant."},
                      {"role": "user", "content": prompt}]
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        return f"OpenAI call failed: {e}"

def chat_llm(user_text: str, history: Optional[List[Tuple[str,str]]] = None) -> str:
    """Chat via best available backend.
    Priority:
      1) Retrieval from CSV (fast, on-device) if available
      2) OpenAI Chat if OPENAI_API_KEY present
      3) Local small-talk fallback
    You can extend with transformers or llama.cpp later.
    """
    # Retrieval-based response from CSV
    try:
        if _resp_vectorizer is not None and _resp_matrix is not None and _resp_texts:
            vec = _resp_vectorizer.transform([user_text.lower()])
            sims = (vec @ _resp_matrix.T).toarray()[0]
            import numpy as np  # type: ignore
            idx = int(sims.argmax())
            conf = float(sims[idx])
            if conf >= 0.30:
                return _resp_responses[idx]
    except Exception as e:
        print(f"[WARN] Chat retrieval failed: {e}")
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        # Compose context into a prompt for better coherence
        ctx = []
        for u,a in (history or [])[-6:]:
            ctx.append({"role":"user","content":u})
            ctx.append({"role":"assistant","content":a})
        ctx.append({"role":"user","content":user_text})
        try:
            import openai  # type: ignore
            openai.api_key = api_key
            completion = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"system","content":"You are Luna, a witty, concise, and helpful desktop assistant."}] + ctx
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            return f"LLM chat failed: {e}"
    # Fallback
    return respond_smalltalk(user_text)


def tell_date_time(kind: str) -> str:
    now = datetime.now()
    if kind == 'time':
        return now.strftime("It's %H:%M:%S.")
    elif kind == 'date':
        return now.strftime("Today is %A, %d %B %Y.")
    else:
        return "Unknown date/time request."

# =============================
# Local Music Helpers
# =============================

def scan_music_files(limit: int = 5000) -> List[Path]:
    files: List[Path] = []
    try:
        if MUSIC_DIR.exists():
            for root, _, filenames in os.walk(MUSIC_DIR):
                for name in filenames:
                    p = Path(root) / name
                    if p.suffix.lower() in MUSIC_EXTS:
                        files.append(p)
                        if len(files) >= limit:
                            return files
    except Exception as e:
        print(f"[WARN] Music scan failed: {e}")
    return files

_music_cache: Optional[List[Path]] = None

def find_local_tracks(query: str, refresh: bool = False) -> List[Path]:
    global _music_cache
    if _music_cache is None or refresh:
        _music_cache = scan_music_files()
    q = query.lower()
    matches = [p for p in (_music_cache or []) if q in p.name.lower()]
    if matches:
        return matches
    names = [p.stem for p in (_music_cache or [])]
    close = get_close_matches(query, names, n=3, cutoff=0.6)
    results: List[Path] = []
    for c in close:
        for p in (_music_cache or []):
            if p.stem == c:
                results.append(p)
                break
    return results


def parse_intent(command: str) -> Dict[str, Any]:
    """Return a dict with keys: intent, args, raw"""
    c = command.lower().strip()
    # Greeting / wake word removal
    for w in WAKE_WORDS:
        if c.startswith(w):
            c = c[len(w):].strip()
            break
    if not c:
        return {"intent": "empty", "args": {}}
    # Single-word site shortcuts (e.g., "spotify", "youtube", "gmail")
    if c in SITE_URLS:
        return {"intent": "open_app", "args": {"app": c}}
    # Command to retrain model
    if c in ["retrain model", "train model", "rebuild model"]:
        ok, msg = train_intent_model()
        # Use a dedicated intent so we speak the status directly
        return {"intent": "ml_trained", "args": {"message": msg}}
    # Voice & TTS controls (handled BEFORE ML to avoid misclassification)
    if c in ["list voices", "voices", "voice list"]:
        return {"intent": "list_voices", "args": {}}
    m_voice = re.search(r"(use|set) voice (\d+)", c)
    if m_voice:
        return {"intent": "set_voice", "args": {"index": int(m_voice.group(2))}}
    m_vname = re.search(r"(use|set) voice named (.+)$", c)
    if m_vname:
        return {"intent": "set_voice_name", "args": {"name": m_vname.group(2).strip()}}
    m_vol = re.search(r"(set )?volume (\d+)%?", c)
    if m_vol:
        try:
            v = max(0, min(100, int(m_vol.group(2)))) / 100.0
            return {"intent": "set_tts_volume", "args": {"volume": v}}
        except Exception:
            pass
    if c in ["mute", "mute voice", "mute tts"]:
        return {"intent": "toggle_tts", "args": {"enabled": False}}
    if c in ["unmute", "enable voice", "enable tts", "unmute voice"]:
        return {"intent": "toggle_tts", "args": {"enabled": True}}
    if c in ["restart tts", "reload tts", "tts restart"]:
        return {"intent": "restart_tts", "args": {}}
    if c in ["voice test", "test voice"]:
        return {"intent": "voice_test", "args": {}}
    if c in ["use sapi", "sapi mode", "use sapi voice"]:
        return {"intent": "set_tts_backend", "args": {"backend": "sapi"}}
    if c in ["use pyttsx3", "pyttsx3 mode"]:
        return {"intent": "set_tts_backend", "args": {"backend": "pyttsx3"}}
    if c in ["use powershell tts", "use powershell", "powershell tts"]:
        return {"intent": "set_tts_backend", "args": {"backend": "ps"}}
    # Speaking rate quick controls
    if c in ["faster", "speak faster", "speed up", "increase speed"]:
        return {"intent": "adjust_tts_rate", "args": {"delta": 0.15}}
    if c in ["slower", "speak slower", "slow down", "decrease speed"]:
        return {"intent": "adjust_tts_rate", "args": {"delta": -0.15}}
    # Model status diagnostics
    if c in ["model status", "ml status", "model diagnostics", "diagnose model"]:
        return {"intent": "ml_status", "args": {}}
    if c in ["relaxed mode on", "relax mode on", "relaxed on"]:
        return {"intent": "relaxed_mode", "args": {"enabled": True}}
    if c in ["relaxed mode off", "relax mode off", "relaxed off"]:
        return {"intent": "relaxed_mode", "args": {"enabled": False}}
    if c in ["ethereal voice on", "luna voice on", "soft voice on"]:
        return {"intent": "ethereal_voice", "args": {"enabled": True}}
    if c in ["ethereal voice off", "luna voice off", "soft voice off"]:
        return {"intent": "ethereal_voice", "args": {"enabled": False}}
    if c in ["suggestions off", "silence suggestions", "mute suggestions"]:
        return {"intent": "toggle_suggestions", "args": {"speak": False}}
    if c in ["suggestions on", "speak suggestions"]:
        return {"intent": "toggle_suggestions", "args": {"speak": True}}
    # ML classifier next, if available
    ml = parse_intent_ml(c)
    if ml:
        return ml
    # Try NLP-powered parsing first if available
    smart = parse_intent_smart(c)
    if smart:
        return smart
    # Date/time
    if any(word in c for word in ["time", "what time", "current time"]):
        return {"intent": "time", "args": {}}
    if any(word in c for word in ["date", "what's the date", "today's date"]):
        return {"intent": "date", "args": {}}
    # Weather
    if c.startswith("weather"):
        # possible: "weather" or "weather in <city>"
        m = re.search(r"weather in ([a-zA-Z ]+)", c)
        city = m.group(1).strip() if m else preferences.get(DEFAULT_CITY_KEY, "London")
        return {"intent": "weather", "args": {"city": city}}
    # Open app
    if c.startswith("open "):
        app = c[5:].strip()
        return {"intent": "open_app", "args": {"app": app}}
    # Search
    if c.startswith("search "):
        query = c[7:].strip()
        return {"intent": "search", "args": {"query": query}}
    if c.startswith("youtube search "):
        query = c[len("youtube search "):].strip()
        return {"intent": "search", "args": {"query": "youtube " + query}}
    # Notes
    if c.startswith("note ") or c.startswith("take note "):
        text = c.split("note", 1)[1].strip()
        return {"intent": "note", "args": {"text": text}}
    # Reminders
    if "remind me" in c or "set reminder" in c:
        parsed = parse_reminder(c)
        if parsed:
            return {"intent": "reminder", "args": parsed}
        return {"intent": "error", "args": {"message": "Couldn't parse reminder format."}}
    # Play song
    if c.startswith("play "):
        song = c[5:].strip()
        return {"intent": "play_song", "args": {"query": song}}
    # Microphone management
    if c in ["list microphones", "list mics", "microphones"]:
        return {"intent": "list_mics", "args": {}}
    # accept 'mic' or 'mike'
    m = re.search(r"(use|set) (?:mic|mike) (\d+)", c)
    if m:
        return {"intent": "set_mic", "args": {"index": int(m.group(2))}}
    # Voice (TTS) management
    if c in ["list voices", "voices", "voice list"]:
        return {"intent": "list_voices", "args": {}}
    m_voice = re.search(r"(use|set) voice (\d+)", c)
    if m_voice:
        return {"intent": "set_voice", "args": {"index": int(m_voice.group(2))}}
    m_vol = re.search(r"(set )?volume (\d+)%?", c)
    if m_vol:
        try:
            v = max(0, min(100, int(m_vol.group(2)))) / 100.0
            return {"intent": "set_tts_volume", "args": {"volume": v}}
        except Exception:
            pass
    if c in ["mute", "mute voice", "mute tts"]:
        return {"intent": "toggle_tts", "args": {"enabled": False}}
    if c in ["unmute", "enable voice", "enable tts", "unmute voice"]:
        return {"intent": "toggle_tts", "args": {"enabled": True}}
    if c in ["restart tts", "reload tts", "tts restart"]:
        return {"intent": "restart_tts", "args": {}}
    if c in ["voice test", "test voice"]:
        return {"intent": "voice_test", "args": {}}
    if c in ["use sapi", "sapi mode", "use sapi voice"]:
        return {"intent": "set_tts_backend", "args": {"backend": "sapi"}}
    if c in ["use pyttsx3", "pyttsx3 mode"]:
        return {"intent": "set_tts_backend", "args": {"backend": "pyttsx3"}}
    if c in ["use powershell tts", "use powershell", "powershell tts"]:
        return {"intent": "set_tts_backend", "args": {"backend": "ps"}}
    # Theme change
    m_theme = re.search(r"(change|set) theme (fate-light|fate|default)", c)
    if m_theme:
        return {"intent": "set_theme", "args": {"theme": m_theme.group(2)}}
    # auto theme toggle
    if c in ["enable auto theme", "auto theme on", "theme auto on"]:
        return {"intent": "toggle_theme_auto", "args": {"enabled": True}}
    if c in ["disable auto theme", "auto theme off", "theme auto off"]:
        return {"intent": "toggle_theme_auto", "args": {"enabled": False}}
    # OpenAI advanced chat
    if c.startswith("ask ai ") or c.startswith("chat "):
        prompt = c.split(" ", 2)[2] if c.startswith("ask ai ") else c[5:]
        return {"intent": "openai", "args": {"prompt": prompt}}
    # Exit
    if c in ["quit", "exit", "goodbye", "stop"]:
        return {"intent": "exit", "args": {}}
    return {"intent": "unknown", "args": {"raw": c}}

def parse_intent_ml(c: str) -> Optional[Dict[str, Any]]:
    if _ml_pipeline is None:
        return None
    try:
        proba = None
        # predict probabilities if available
        if hasattr(_ml_pipeline.named_steps.get('clf'), 'predict_proba'):
            import numpy as np  # type: ignore
            proba = _ml_pipeline.predict_proba([c])[0]
            labels = _ml_pipeline.named_steps['clf'].classes_.tolist()
            idx = int(proba.argmax())
            label = labels[idx]
            conf = float(proba[idx])
        else:
            label = _ml_pipeline.predict([c])[0]
            conf = 0.6  # assume moderate confidence
        # Conservative confidence thresholding to avoid false positives like accidental 'exit'
        words = c.split()
        threshold = 0.7 if len(words) <= 2 else 0.6
        if conf < threshold:
            return None
        # Map label to intent + extract args
        mapped = map_label_to_intent(c, str(label))
        # Extra guard: don't allow ML to trigger 'exit' unless keywords are explicitly present
        if mapped.get('intent') == 'exit':
            if not re.search(r"\b(quit|exit|goodbye|bye|stop)\b", c):
                return {"intent": "chat", "args": {"message": c}}
        return mapped
    except Exception:
        return None

def map_label_to_intent(c: str, label: str) -> Dict[str, Any]:
    # Extended mapping including external CSV labels
    if label in {'greeting','fun_conversation','general_conversation'}:
        return {"intent": "chat", "args": {"message": c}}
    if label == 'weather_query':
        m = re.search(r"weather in ([a-zA-Z ]+)", c)
        city = m.group(1).strip() if m else preferences.get(DEFAULT_CITY_KEY, "London")
        return {"intent": "weather", "args": {"city": city}}
    if label == 'time_reminder':
        if any(w in c for w in ['remind me','set reminder','alarm','timer']):
            parsed = parse_reminder(c)
            return {"intent": "reminder", "args": parsed} if parsed else {"intent": "error", "args": {"message": "Couldn't parse reminder format."}}
        if any(w in c for w in ["time", "what time"]):
            return {"intent": "time", "args": {}}
        if any(w in c for w in ["date", "today's date"]):
            return {"intent": "date", "args": {}}
        return {"intent": "chat", "args": {"message": c}}
    if label == 'music_control':
        if c.startswith('play '):
            q = c[5:].strip()
            return {"intent": "play_song", "args": {"query": q}}
        return {"intent": "chat", "args": {"message": c}}
    if label == 'system_control':
        return {"intent": "chat", "args": {"message": c}}
    if label == 'time':
        return {"intent": "time", "args": {}}
    if label == 'date':
        return {"intent": "date", "args": {}}
    if label == 'weather':
        m = re.search(r"weather in ([a-zA-Z ]+)", c)
        city = m.group(1).strip() if m else preferences.get(DEFAULT_CITY_KEY, "London")
        return {"intent": "weather", "args": {"city": city}}
    if label == 'open_app':
        m = re.search(r"(open|launch|start|run)\s+(.*)$", c)
        app = m.group(2).strip() if m else c.replace('open', '').strip()
        return {"intent": "open_app", "args": {"app": app}}
    if label == 'search':
        m = re.search(r"(search|google|look up)\s+(.*)$", c)
        q = m.group(2).strip() if m else c.replace('search', '').strip()
        return {"intent": "search", "args": {"query": q}}
    if label == 'note':
        text = c.split('note', 1)[1].strip() if 'note' in c else c
        return {"intent": "note", "args": {"text": text}}
    if label == 'reminder':
        parsed = parse_reminder(c)
        return {"intent": "reminder", "args": parsed} if parsed else {"intent": "error", "args": {"message": "Couldn't parse reminder format."}}
    if label == 'play_song':
        m = re.search(r"play\s+(.*)$", c)
        q = m.group(1).strip() if m else c
        return {"intent": "play_song", "args": {"query": q}}
    if label == 'list_mics':
        return {"intent": "list_mics", "args": {}}
    if label == 'set_mic':
        m = re.search(r"(use|set) mic (\d+)", c)
        if m:
            return {"intent": "set_mic", "args": {"index": int(m.group(2))}}
        return {"intent": "list_mics", "args": {}}
    if label == 'set_theme':
        m = re.search(r"(fate-light|fate|default)", c)
        name = m.group(1) if m else get_theme_name()
        return {"intent": "set_theme", "args": {"theme": name}}
    if label == 'toggle_theme_auto':
        en = 'enable' in c or 'on' in c
        return {"intent": "toggle_theme_auto", "args": {"enabled": en}}
    if label == 'exit':
        return {"intent": "exit", "args": {}}
    # default to chat
    return {"intent": "chat", "args": {"message": c}}


def load_nlp_model():
    global _NLP
    if spacy and _NLP is None:
        try:
            _NLP = spacy.load("en_core_web_sm")
        except Exception:
            _NLP = None


def parse_intent_smart(c: str) -> Optional[Dict[str, Any]]:
    """Use spaCy lemmatization/NER for flexible phrasing. Returns None if unavailable."""
    load_nlp_model()
    if _NLP is None:
        return None
    doc = _NLP(c)
    lemmas = [t.lemma_.lower() for t in doc]

    def has(words: List[str]) -> bool:
        return any(w in lemmas for w in words)

    # exit
    if has(["quit", "exit", "stop", "goodbye"]):
        return {"intent": "exit", "args": {}}
    # time/date
    if has(["time"]):
        return {"intent": "time", "args": {}}
    if has(["date", "today"]):
        return {"intent": "date", "args": {}}
    # weather
    if has(["weather"]):
        city = None
        for ent in doc.ents:
            if ent.label_ in ("GPE", "LOC"):
                city = ent.text
                break
        city = city or preferences.get(DEFAULT_CITY_KEY, "London")
        return {"intent": "weather", "args": {"city": city}}
    # open app
    if has(["open", "launch", "start", "run"]):
        m = re.search(r"(open|launch|start|run)\s+(.*)$", c)
        if m:
            return {"intent": "open_app", "args": {"app": m.group(2).strip()}}
    # search
    if has(["search", "google", "lookup", "look"]):
        m = re.search(r"(search|google|look up)\s+(.*)$", c)
        if m:
            return {"intent": "search", "args": {"query": m.group(2).strip()}}
    # note
    if has(["note", "remember", "jot"]):
        m = re.search(r"(note|remember|jot)\s+(.*)$", c)
        if m:
            return {"intent": "note", "args": {"text": m.group(2).strip()}}
    # reminder
    if has(["remind", "reminder"]):
        parsed = parse_reminder(c)
        if parsed:
            return {"intent": "reminder", "args": parsed}
    # play music
    if has(["play"]):
        m = re.search(r"play\s+(.*)$", c)
        if m:
            return {"intent": "play_song", "args": {"query": m.group(1).strip()}}
    return None

# =============================
# Simple chat fallback
# =============================

def respond_smalltalk(message: str) -> str:
    m = message.lower().strip()
    greetings = ["hi", "hello", "hey", "yo", "sup", "what's up"]
    thanks = ["thanks", "thank you", "ty", "appreciate it"]
    if any(g in m.split() for g in greetings):
        return "Hello."
    if any(t in m for t in thanks):
        return "You're welcome! Got anything else for me?"
    if "who are you" in m or "what are you" in m:
        return "I'm Luna—your slightly sassy desktop assistant. I open apps, search, set reminders, play music, and more."
    if "tell me about yourself" in m or "about yourself" in m or "about you" in m:
        return (
            "I'm Luna—your Windows sidekick. I can open apps, search the web, check weather, take notes, set reminders, play music, and chat a bit."
        )
    if "how are you" in m:
        return "At peak performance, as always. How about you?"
    if m in {"how", "how?"} or m.startswith("how "):
        return "Doing well and listening. Want time, weather, a search, or to open an app?"
    if (
        "am i audible" in m
        or "can you hear me" in m
        or "are you able to listen" in m
        or "are you listening" in m
    ):
        return "Yep, loud and clear. To change mics, say 'list microphones' or 'use mic 0'."
    # Preference-based flavor
    fav_apps = get_top_items(preferences.get('favorite_apps', {}), 2)
    if fav_apps:
        return "I'm listening."
    return "I'm here."


# =============================
# Suggestions & Tray Icon helpers
# =============================

def get_suggestions(n: int = 3) -> List[str]:
    fav_apps = get_top_items(preferences.get('favorite_apps', {}), n)
    fav_topics = get_top_items(preferences.get('favorite_topics', {}), n)
    suggestions: List[str] = []
    for a in fav_apps:
        suggestions.append(f"open {a}")
    for t in fav_topics:
        suggestions.append(f"search {t}")
    suggestions += ["what's the weather", "set reminder in 10 minutes to stretch"]
    return suggestions[:n]


def maybe_suggest(idle_seconds: int = 25, cooldown_seconds: int = 60):
    global _last_suggest
    now = datetime.now()
    if (now - _last_activity).total_seconds() < idle_seconds:
        return
    if _last_suggest and (now - _last_suggest).total_seconds() < cooldown_seconds:
        return
    sugg = get_suggestions()
    if sugg:
        # Only show if explicitly enabled; do not speak by default to avoid long TTS
        if preferences.get('suggest_speak', False):
            speak("Suggestions: " + "; ".join(sugg))
        elif preferences.get('suggest_show', False):
            print("[SUGGEST] " + "; ".join(sugg))
        _last_suggest = datetime.now()


def _create_tray_image(size: int = 64):
    if Image is None:
        return None
    # Prefer excalibur.png as tray icon, else saber.jpg cropped circle, else generated L logo
    try:
        if ASSET_EXCALIBUR.exists():
            img = Image.open(ASSET_EXCALIBUR).convert('RGBA')
            img = img.resize((size, size), Image.LANCZOS)
            return img
        if ASSET_SABER.exists():
            img = Image.open(ASSET_SABER).convert('RGBA').resize((size, size), Image.LANCZOS)
            # circular crop
            mask = Image.new('L', (size, size), 0)
            d = ImageDraw.Draw(mask)
            d.ellipse((0, 0, size, size), fill=255)
            circ = Image.new('RGBA', (size, size))
            circ.paste(img, (0, 0), mask)
            return circ
    except Exception:
        pass
    # Fallback generated icon with "L"
    img = Image.new('RGBA', (size, size), (40, 40, 48, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size-4, size-4), fill=(120, 86, 255, 255))
    d.rectangle((size*0.35, size*0.25, size*0.48, size*0.78), fill=(255, 255, 255, 230))
    d.rectangle((size*0.35, size*0.65, size*0.7, size*0.78), fill=(255, 255, 255, 230))
    return img


def start_tray_icon():
    if pystray is None:
        return
    icon_image = _create_tray_image()

    def on_toggle(icon, item):
        global listening_enabled
        listening_enabled = not listening_enabled
        state = "on" if listening_enabled else "paused"
        speak(f"Listening {state}.")

    def on_suggest(icon, item):
        sugg = get_suggestions()
        if sugg:
            speak("Try saying: " + "; ".join(sugg))
        else:
            speak("Ask me anything. I'm all ears—virtually.")

    def on_quit(icon, item):
        global stop_flag
        stop_flag = True
        try:
            icon.stop()
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: "Pause listening" if listening_enabled else "Resume listening", on_toggle),
        pystray.MenuItem("Suggestions", on_suggest),
        pystray.MenuItem("Quit Luna", on_quit)
    )

    icon = pystray.Icon("Luna", icon_image, "Luna Assistant", menu)
    try:
        icon.run()
    except Exception as e:
        print(f"[WARN] Tray stopped: {e}")

# =============================
# Main Loop
# =============================

def main():
    global _speech_engine
    global _overlay_ui
    _speech_engine = SpeechEngine()
    overlay = OverlayUI()  # initialize optional overlay
    _overlay_ui = overlay
    overlay.set_status("Luna ready.")
    speak(time_based_greeting())
    # Personalization suggestions
    fav_apps = get_top_items(preferences.get('favorite_apps', {}))
    fav_topics = get_top_items(preferences.get('favorite_topics', {}))
    if fav_apps:
        speak(f"I notice you like: {', '.join(fav_apps)}.")
    if fav_topics:
        speak(f"Recent topics: {', '.join(fav_topics)}.")

    # Start reminder background thread
    t = threading.Thread(target=reminder_worker, daemon=True)
    t.start()

    speak("I'm ready. Talk to me. (Say 'exit' to quit.)")
    # Train model on first run if missing
    if _ML_AVAILABLE and _ml_pipeline is None:
        ok, msg = train_intent_model()
        print(f"[ML] {msg}")

    # Start tray icon thread if available
    if pystray is not None and Image is not None:
        try:
            threading.Thread(target=start_tray_icon, daemon=True).start()
        except Exception as e:
            print(f"[WARN] Tray icon failed: {e}")

    while True:
        if stop_flag:
            speak("Exiting. See you next time.")
            break
        maybe_suggest()
        if not listening_enabled:
            time.sleep(0.3)
            continue
        # If TTS is currently speaking, avoid listening to prevent echo loops
        if _is_speaking:
            overlay.set_status("Speaking…", listening=False)
            time.sleep(0.15)
            continue
        # Indicate listening state
        overlay.set_status("Listening…", listening=True)
        command = _speech_engine.listen()
        overlay.set_status("Processing…", listening=False)
        if not command:
            overlay.set_status("Ready.", listening=False)
            continue
        # Echo suppression: ignore captured text that matches our last spoken line
        try:
            if _last_spoken_text:
                cmd = command.lower().strip()
                # simple overlap heuristic
                toks_c = set(cmd.split())
                toks_s = set(_last_spoken_text.split())
                overlap = len(toks_c & toks_s)
                if len(toks_c) > 0 and (overlap / max(1, len(toks_c))) >= 0.6:
                    # likely our own voice was captured; skip
                    continue
        except Exception:
            pass
        # If user only said wake word, show overlay and prompt for next command
        if command.strip() in WAKE_WORDS or command.strip() == "luna":
            overlay.set_status("Hi, I'm Luna. What's next?", listening=True)
            follow = _speech_engine.listen("What's your command?")
            if follow:
                command = follow
            else:
                overlay.set_status("Ready.")
                continue
        else:
            # Display heard command
            overlay.set_status(f"Heard: {command[:60]}", listening=False)
        intent_data = parse_intent(command)
        intent = intent_data['intent']
        args = intent_data['args']
        if intent == 'unknown':
            # escalate to chat fallback
            intent = 'chat'
            args = {'message': intent_data.get('args', {}).get('raw', command)}

        if intent == 'empty':
            continue
        if intent == 'exit':
            speak("Goodbye. Powering down my sass.")
            break
        elif intent == 'time':
            msg = tell_date_time('time')
            speak(msg)
            track_usage(intent)
        elif intent == 'date':
            msg = tell_date_time('date')
            speak(msg)
            track_usage(intent)
        elif intent == 'weather':
            city = args['city']
            msg = get_weather(city)
            speak(msg)
            track_usage(intent, city)
        elif intent == 'open_app':
            app = args['app']
            msg = open_app(app)
            speak(msg)
            track_usage(intent, app)
        elif intent == 'search':
            query = args['query']
            # Perform the search, speak the summary/result, and track usage with the query.
            msg = perform_search(query)
            try:
                speak(msg)
            except Exception:
                pass
            track_usage(intent, query)
        elif intent == 'chat':
            user_msg = args.get('message', '')
            reply = chat_llm(user_msg, _chat_history)
            _chat_history.append((user_msg, reply))
            if len(_chat_history) > 12:
                _chat_history.pop(0)
            # Only speak the assistant's reply (avoid echoing the user's own message).
            speak(reply)
            # Track chat usage without referencing undefined 'query'.
            track_usage(intent)
        elif intent == 'note':
            text = args['text']
            append_note(text)
            speak("Noted. Future historians will appreciate that.")
            track_usage(intent)
        elif intent == 'reminder':
            when: datetime = args['when']
            message = args['message']
            add_reminder(message, when)
            track_usage(intent)
        elif intent == 'play_song':
            q = args['query']
            msg = play_song(q)
            speak(msg)
            track_usage(intent, q)
        elif intent == 'openai':
            prompt = args['prompt']
            answer = integrate_openai(prompt)
            speak(answer)
            track_usage(intent)
        elif intent == 'ml_trained':
            status = args.get('message', 'Model retrained.')
            # Announce status directly (do not send to chat)
            speak(status)
        elif intent == 'ml_status':
            try:
                if _ml_pipeline is None:
                    speak("Model not loaded. Say 'retrain model'.")
                else:
                    cls = _ml_pipeline.named_steps.get('clf')
                    classes = getattr(cls, 'classes_', []) if cls else []
                    speak(f"Model ready: {len(classes)} intents; retrieval {len(_resp_texts)} samples.")
            except Exception:
                speak("Model status check failed.")
        elif intent == 'adjust_tts_rate':
            delta = float(args.get('delta', 0.0))
            cur = float(preferences.get('tts_rate', 1.0))
            # Clamp between 0.6 and 1.6 for safety
            new_rate = max(0.6, min(1.6, cur + delta))
            preferences['tts_rate'] = new_rate
            save_json(PREFERENCES_FILE, preferences)
            if _speech_engine:
                _speech_engine.request_tts_restart()
            speak(f"Speed set.")
        elif intent == 'list_mics':
            items = list_microphones()
            # Print full list, speak summary
            print("\nAvailable microphones:")
            for line in items:
                print("  ", line)
            if items and len(items) > 1:
                speak("I listed microphones in the console. Say 'use mic 0' or another number to select.")
            else:
                speak(items[0] if items else "No microphones detected.")
        elif intent == 'set_mic':
            idx = args['index']
            set_configured_mic_index(idx)
            _speech_engine.refresh_mic()
            if _speech_engine.microphone_available:
                speak(f"Microphone set to device {idx} and ready.")
            else:
                speak(f"I saved mic {idx}, but it still looks unavailable.")
        elif intent == 'list_voices':
            try:
                # use a temporary engine to enumerate voices safely
                import pyttsx3
                tmp = pyttsx3.init(driverName='sapi5')
                voices = tmp.getProperty('voices') or []
                print("\nAvailable voices:")
                for i,v in enumerate(voices):
                    print(f"  [{i}] {getattr(v,'name','?')} | id={getattr(v,'id','?')}")
                speak(f"Found {len(voices)} voices. Say 'set voice 0' or another number to pick.")
            except Exception as e:
                speak(f"Couldn't list voices: {e}")
        elif intent == 'set_voice':
            idx = args['index']
            try:
                import pyttsx3
                tmp = pyttsx3.init(driverName='sapi5')
                voices = tmp.getProperty('voices') or []
                if 0 <= idx < len(voices):
                    vid = getattr(voices[idx], 'id', None)
                    if vid:
                        preferences['tts_voice_id'] = vid
                        save_json(PREFERENCES_FILE, preferences)
                        # ask worker to restart so new voice sticks immediately
                        if _speech_engine:
                            _speech_engine.request_tts_restart()
                        speak(f"Voice set to index {idx}.")
                    else:
                        speak("Couldn't access that voice id.")
                else:
                    speak("That voice index is out of range.")
            except Exception as e:
                speak(f"Failed to set voice: {e}")
        elif intent == 'set_voice_name':
            name = args.get('name','').lower()
            if not name:
                speak("Please say the voice name, for example 'set voice named zira'.")
            else:
                try:
                    import pyttsx3
                    tmp = pyttsx3.init(driverName='sapi5')
                    voices = tmp.getProperty('voices') or []
                    match_id = None
                    for v in voices:
                        nm = (getattr(v,'name','') + ' ' + getattr(v,'id','')).lower()
                        if name in nm:
                            match_id = getattr(v,'id', None)
                            break
                    # friendly fallback to common female voices
                    if match_id is None and 'luna' in name:
                        for v in voices:
                            nm = (getattr(v,'name','') + ' ' + getattr(v,'id','')).lower()
                            if any(t in nm for t in ['zira','aria','jenny','eva','susan']):
                                match_id = getattr(v,'id', None)
                                if match_id:
                                    break
                    if match_id:
                        preferences['tts_voice_id'] = match_id
                        save_json(PREFERENCES_FILE, preferences)
                        if _speech_engine:
                            _speech_engine.request_tts_restart()
                        speak("Voice set by name.")
                    else:
                        speak("I couldn't find a matching voice on this system.")
                except Exception as e:
                    speak(f"Failed to set voice by name: {e}")
        elif intent == 'set_tts_volume':
            vol = args.get('volume', 1.0)
            preferences['tts_volume'] = vol
            save_json(PREFERENCES_FILE, preferences)
            try:
                speak(f"Volume set to {int(vol*100)} percent.")
            except Exception as e:
                speak(f"Failed to set volume: {e}")
        elif intent == 'toggle_tts':
            en = args.get('enabled', True)
            preferences['tts_enabled'] = en
            save_json(PREFERENCES_FILE, preferences)
            speak("Voice output enabled." if en else "Voice output muted.")
        elif intent == 'restart_tts':
            try:
                if _speech_engine:
                    _speech_engine.request_tts_restart()
                speak("TTS engine restarted.")
            except Exception as e:
                speak(f"Failed to restart TTS: {e}")
        elif intent == 'voice_test':
            speak("This is a voice test. If you heard me, TTS is working.")
        elif intent == 'relaxed_mode':
            en = bool(args.get('enabled', False))
            preferences['tts_rate'] = 0.85 if en else 0.95
            save_json(PREFERENCES_FILE, preferences)
            if _speech_engine:
                _speech_engine.request_tts_restart()
            speak("Relaxed voice enabled." if en else "Relaxed voice disabled.")
        elif intent == 'ethereal_voice':
            en = bool(args.get('enabled', False))
            # Switch style plus soft_mode coupling
            preferences['tts_style'] = 'ethereal' if en else 'default'
            if en:
                preferences['soft_mode'] = True
                # Slightly slow down further for airy effect
                preferences['tts_rate'] = 0.80
            else:
                # Revert soft settings to defaults only if we previously enabled them via ethereal
                preferences['soft_mode'] = False
                preferences['tts_rate'] = 0.95
            save_json(PREFERENCES_FILE, preferences)
            if _speech_engine:
                _speech_engine.request_tts_restart()
            speak("Ethereal voice enabled." if en else "Ethereal voice disabled.")
        elif intent == 'toggle_suggestions':
            speak_on = bool(args.get('speak', False))
            preferences['suggest_speak'] = speak_on
            save_json(PREFERENCES_FILE, preferences)
            speak("I'll speak suggestions." if speak_on else "I'll keep suggestions quiet.")
        elif intent == 'set_tts_backend':
            backend = args.get('backend', 'pyttsx3')
            if backend not in ['pyttsx3', 'sapi', 'ps']:
                speak("Unsupported backend. Choose 'pyttsx3', 'sapi', or 'ps' (PowerShell).")
            else:
                preferences['tts_backend'] = backend
                save_json(PREFERENCES_FILE, preferences)
                if _speech_engine:
                    _speech_engine.request_tts_restart()
                human = 'PowerShell' if backend == 'ps' else backend
                speak(f"TTS backend set to {human}.")
        elif intent == 'set_theme':
            name = args.get('theme', 'default')
            if name not in THEMES:
                speak("I only know 'default' and 'fate' themes right now.")
            else:
                preferences['theme_auto'] = False
                save_json(PREFERENCES_FILE, preferences)
                set_theme_name(name)
                try:
                    overlay.apply_theme_by_name(name)
                except Exception:
                    pass
                speak(f"Applied the {name} theme.")
        elif intent == 'toggle_theme_auto':
            enabled = bool(args.get('enabled', False))
            preferences['theme_auto'] = enabled
            save_json(PREFERENCES_FILE, preferences)
            if enabled:
                try:
                    overlay._apply_auto_theme(force=True)
                except Exception:
                    pass
                speak("Auto theme enabled.")
            else:
                speak("Auto theme disabled.")
        elif intent == 'error':
            speak(args['message'])
        else:
            speak("I'm not sure how to do that yet. Try saying 'search cats doing parkour'.")
        # Keep overlay persistent; return to ready state
        overlay.set_status("Ready.")
    # End while


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        speak("Interrupted. See you later!")
    except Exception as e:
        print(f"[FATAL] {e}")
        raise

import logging
logger = logging.getLogger(__name__)
__version__ = '1.0.0-alpha'

class ConfigManager:
    def __init__(self): self.config = {}

from logging.handlers import RotatingFileHandler
def setup_logging(): pass

class ResponseCache:
    def __init__(self): self.cache = {}

class DatabaseManager:
    def __init__(self): self.db_path = ''

class EnhancedSpeechEngine:
    def __init__(self): self.recognizer = sr.Recognizer()

class VoiceProfileManager:
    def __init__(self): self.profiles = {}

class AudioMetrics:
    def __init__(self): self.noise_level = 0

class CommandValidator:
    def __init__(self): self.valid_commands = set()

from sklearn.ensemble import GradientBoostingClassifier
class IntentClassifierV2:
    def __init__(self): self.model = GradientBoostingClassifier()

class EntityExtractor:
    def __init__(self): self.model_name = 'en_core_web_sm'

class SentimentAnalyzer:
    def __init__(self): self.positive_words = set()

class ConversationState:
    def __init__(self): self.history = []

class DataAugmenter:
    def __init__(self): self.synonyms = {}

import hashlib
class ModelVersionManager:
    def __init__(self): self.versions = {}

class HPOFramework:
    def __init__(self): self.best_params = {}

from sklearn.model_selection import cross_val_score
class CrossValidationHelper:
    def __init__(self): self.cv_folds = 5

class PredictionFilter:
    def __init__(self): self.min_confidence = 0.75

scikit-learn>=1.3.0

class RequestBatcher:
    def __init__(self): self.queue = []

class MemoryMonitor:
    def __init__(self): self.snapshots = []

from functools import wraps

class SearchEngine:
    def __init__(self): self.index = {}

class NotificationManager:
    def __init__(self): self.notifications = []

class PluginManager:
    def __init__(self): self.plugins = {}

class EventBus:
    def __init__(self): self.subscribers = {}

class RateLimiter:
    def __init__(self): self.requests = {}

class InputValidator:
    def __init__(self): self.blacklist = set()

class APIKeyManager:
    def __init__(self): self.keys = {}

class SecureStorage:
    def __init__(self): self.master_key = ''

class AuditLogger:
    def __init__(self): self.log_file = Path('audit.log')

class AnimatedOverlay:
    def __init__(self): self.frames = []

class ThemeEngine:
    def __init__(self): self.themes = {}

# Updated features

class OAuth2Client:
    def __init__(self): self.access_token = None

class WeatherService:
    def __init__(self): self.api_key = ''

class CalendarService:
    def __init__(self): self.events = []

class MusicServiceAdapter:
    def __init__(self): self.service = ''

class SkillTrainer:
    def __init__(self): self.skills = {}

class PreferenceLearner:
    def __init__(self): self.preferences = {}

class ContextMemory:
    def __init__(self): self.memory = []

class MacroSystem:
    def __init__(self): self.macros = {}

class CommandHistory:
    def __init__(self): self.history = []

class NLPParser:
    def __init__(self): self.patterns = {}

class BatchCommandExecutor:
    def __init__(self): self.queue = []

class AnalyticsDashboard:
    def __init__(self): self.metrics = {}

class TaskScheduler:
    def __init__(self): self.tasks = {}

class ActivityDashboard:
    def __init__(self): self.activities = []

class ShortcutManager:
    def __init__(self): self.shortcuts = {}

class I18nManager:
    def __init__(self): self.translations = {}

class ResponseTemplate:
    def __init__(self): self.templates = {}

class ErrorRecovery:
    def __init__(self): self.strategies = {}

class PerformanceProfiler:
    def __init__(self): self.profiles = {}

def safe_get_nested(dictionary, keys): pass

psutil>=5.9.0

class TextToSpeechQueue:
    def __init__(self): self.queue = Queue()

class SpeechQualityAnalyzer:
    def __init__(self): self.threshold = 0.7

CONFIG_DEFAULTS = {'voice': 'female'}

## Architecture

class AliasSystem:
    def __init__(self): self.aliases = {}

class FallbackManager:
    def __init__(self): pass

class StatisticsCollector:
    def __init__(self): self.stats = {}

class SessionManager:
    def __init__(self): self.sessions = {}

class ConfidenceReporter:
    def __init__(self): self.thresholds = {}

class RetryManager:
    def __init__(self): self.max_retries = 3

class SuggestionEngine:
    def __init__(self): self.commands = set()

class ResponseFormatter:
    def format_list(self): pass

class BackgroundTaskRunner:
    def __init__(self): self.tasks = {}

def handle_errors(default_return=None): pass

class VoiceCommandLogger:
    def __init__(self): self.log_file = Path('')

class ContextAwareResponder:
    def __init__(self): self.context = {}

__all__ = ['ConfigManager']

class TelemetrySystem:
    def __init__(self): self.events = []

class HealthMonitor:
    def check_health(self): pass

class ResourceManager:
    def __init__(self): self.resources = []

class GracefulShutdownHandler:
    def __init__(self): pass

class SensitiveDataFilter: pass

class AdvancedCache:
    def __init__(self): self.cache = {}

class MultiUserSupport:
    def __init__(self): self.contexts = {}

class DataPersistence:
    def __init__(self): self.data_dir = Path('')

class RealtimeNotifications:
    def __init__(self): self.subs = {}
