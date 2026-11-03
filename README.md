# Caps

Caps turns Caps Lock into a command key on Windows. Tap it and a small companion cursor appears at your pointer; start typing and a prompt opens beside it. Hold it and move the mouse to arrange windows. The model features run on your own PC through [Ollama](https://ollama.com).

## What you can type

- Part of a window's name to switch to it (`disc`, `ovl`), or an app's name to open it (`spot`). The window you were in keeps keyboard focus, and Esc, a second tap or any shortcut closes the prompt.
- A question about the screen, such as `what does this error mean?`. Caps sends the model a screenshot of the window under your pointer along with its buttons and text from Windows UI Automation, and the companion points at the button the answer mentions.
- A task, such as `put discord on the left and code on the right` or `search youtube for linear algebra`. The model writes a short plan and carries it out one step at a time. Caps's own code decides which steps wait for your OK, for example sending a message, deleting, buying or running a command that changes something. The tray menu makes it stricter or looser. Tap Caps Lock to stop a task. Each task is logged in `data/agent-log.md`.
- `build my schedule, no classes before 10`, with your degree plan and your registration site's course sections on screen (a Google Sheet and Workday side by side, for example). The model reads the plan and the sections; code finds every combination without time conflicts and ranks them by your preferences. Enter copies the list of sections and saves a calendar file.
- `jot bench 135 3x5` sends a note to [jot](https://github.com/AksNai/jot).
- `focus 50 cs` starts a 50-minute session. Distracting apps are minimized and sites like YouTube and Reddit are covered until it ends. `allow 5` gives you a 5-minute break and `focus off` ends it.

Shift+Caps Lock is still the normal Caps Lock, and Caps leaves the key alone in full-screen games.

## Gestures

Hold Caps Lock, then:

| do this | to get |
|---|---|
| flick left or right | that half of the screen |
| flick diagonally | that quarter |
| flick up or down | maximize or minimize |
| draw a circle | a focus session |
| scribble | undo the last window move |
| hold still | a ring menu with the same window moves |
| drag a window | move it; drop it on a screen edge to snap it |
| right-drag a window | resize it |
| scroll | flip through your open windows |

Gestures act on the window under the pointer. Esc cancels.

If my math autocorrect project is installed next to Caps (in `../math-autocorrect`), it runs in the same keyboard hook and turns `lamda` into λ as you type. Caps works without it.

## Setup

```powershell
pip install -r requirements.txt
ollama pull qwen3.8:27b     # for questions and tasks: any Ollama model with vision and tool use
python main.py              # or start_caps.bat, which runs without a console window
```

Settings live in `data/config.json`, which is created on first run. Quit from the tray icon or by typing `quit caps`.

The first question after the model has been unloaded takes about 30 seconds while it loads. After that, answers take about 2 seconds.

## How it works

- One low-level keyboard hook handles Caps Lock (`keys.py`). After a tap, keys go to the prompt instead of the active window. If the interface stops responding, the hook stops capturing after 3 seconds, so typing is never lost.
- `gestures.py` classifies the pointer's path: a straight stroke is a flick, more than 290 degrees of turning is a circle, and three or more reversals is a scribble.
- The mouse hook only exists while Caps Lock is held, so in games mouse input never passes through Python.
- To point at things, the model names a numbered element from the UI Automation list. Pixel coordinates from the vision model were unreliable. Caps checks the element against the words in the answer before pointing at it.
- `agent.py` has the agent loop and `risk()`, which decides what needs your approval.
- `schedule.py` parses meeting times and solves schedules exactly. The model only reads the screen.

## Privacy

Everything runs on your PC, and Caps doesn't record what you type. Questions and screenshots stay in memory for 3 minutes so you can ask follow-ups. The `data` folder holds the agent log, finished focus sessions, a list of your Start menu apps and an error log.

## Tests

```powershell
python -m pytest tests
```
