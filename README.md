# Luna 1.0 – Personal AI Voice Assistant (Windows, Python)

Luna is a friendly, witty voice assistant you can run on your Windows laptop using plain Python. She listens to your voice, talks back in a female voice, opens apps, searches the web, tells time/date/weather, takes notes, sets reminders, and learns your preferences over time.

## Features
- Time-based greeting with personality
- Voice recognition via `speech_recognition` (fallback to text input if mic fails)
- Text-to-speech via `pyttsx3` (female voice selection)
- Open apps (Chrome, VS Code, Notepad, Calculator)
- Web search (Google + YouTube)
- Date and time reporting
- Weather via OpenWeatherMap (API key required)
- Notes and reminders (background worker)
- Music playback
	- Local library search (scans your `Music` folder; configurable via `LUNA_MUSIC_DIR` env var)
	- YouTube fallback
- Learns preferences (tracks common apps/topics)
- Idle suggestions (periodic hints based on your usage)
- Optional system tray icon (pause listening, suggestions, quit)
- Optional top overlay bar (like Siri): pops up when you say "Luna", shows what was heard, then hides
- Optional: OpenAI integration stub
  
### Optional Advanced NLP
If you install `spacy` and the English model `en_core_web_sm`, Luna will parse more flexible phrasing like:
> "Could you please launch Chrome for me" or "I'd like the weather in Paris".

Without spaCy it falls back to the regex rules.

## Requirements
- Windows 10/11
- Python 3.9+
- Microphone (optional; text fallback works)

## Install dependencies
Use PowerShell:

```powershell
# From the project folder
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -U pip
pip install -r requirements.txt

# (Optional) advanced features
# pip install spacy pystray pillow
# python -m spacy download en_core_web_sm
```

If `pyaudio` fails to build, install the prebuilt wheel from https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio or use `pip install pipwin; pipwin install pyaudio`.

## Configure API keys (optional)
- Weather: create a free key at https://openweathermap.org/api and set it as an environment variable:

```powershell
$env:OPENWEATHER_API_KEY = "your_key_here"
```

- OpenAI (optional):
```powershell
$env:OPENAI_API_KEY = "your_openai_key_here"
```

To make env vars persistent, set them via System Properties or `$PROFILE` script.

## Run Luna
```powershell
python luna_assistant.py
```

Speak commands like:
- "Luna, open Chrome"
- "Search anime fanfics about Soul Land"
- "What's the weather in Tokyo"
- "Remind me in 10 minutes to stretch"
- "Set reminder at 14:30 drink water"
- "Take note prepare slides for demo"
- "Play Nujabes"
- "What time is it"

Say `exit` to quit.

System tray icon appears if `pystray` and `Pillow` are installed; right-click for pause/resume, suggestions, quit.

Top overlay bar uses Python's built-in `tkinter` (no extra install). If `tkinter` is unavailable in your Python build, the overlay is skipped automatically.

## Notes & Data Files
- `preferences.json` – usage stats and defaults (auto-created)
- `reminders.json` – reminders list (auto-created)
- `notes.txt` – notes log (auto-appended)

## Troubleshooting
- Mic not available: Luna falls back to typing input.
- TTS voice not female: Windows voice names vary; Luna tries common female voices like Zira/Aria.
- Can't open app: paths differ per PC. Update `APP_MAP` in `luna_assistant.py` with your paths.
- Weather errors: ensure `OPENWEATHER_API_KEY` is set and network allows `api.openweathermap.org`.

## Extend Luna
- Add more apps to `APP_MAP`.
- Improve intent parsing (tune spaCy heuristics or integrate transformers).
- Wire in OpenAI for smarter, contextual responses.
- Add persistent conversation memory or a journaling mode.
- Implement local media player controls (pause/next) with a library like `pygame` or OS hooks.

### Chrome profile for Spotify/web actions
Set your Chrome profile directory name for web actions that use Chrome (e.g., saying just `Spotify`):

```powershell
$env:LUNA_CHROME_PROFILE = "Profile 1"   # or "Default", "Profile 2", etc.
# Optional: override Chrome path if installed elsewhere
$env:LUNA_CHROME_PATH = "C:\Program Files\Google\Chrome\Application\chrome.exe"
```
Note: The profile value must be the directory name Chrome uses (e.g., `Default`, `Profile 1`), not the visible profile label.

You can also specify the visible profile name instead and let Luna auto-map it:

```powershell
$env:LUNA_CHROME_PROFILE_NAME = "Abhay Kumar"   # The display name shown in Chrome profile picker
```
Luna will read Chrome's Local State file and map the display name to the underlying directory (e.g., `Profile 3`). If both `LUNA_CHROME_PROFILE` and `LUNA_CHROME_PROFILE_NAME` are set, the explicit directory (`LUNA_CHROME_PROFILE`) wins.

Have fun! Luna has jokes, plays your tunes, and nudges you when you're idle. ☕️💻🎧
## New Features
- ML-based intent recognition

### Components
- Speech Engine
- Intent Classifier
