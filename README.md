# Caps

I built this to make my computer easier to use. 
Tap Caps Lock and ask it for help, or hold it down for gesture controls.

- focus: blocks distracting apps and sites for a set time
- queries: ask about anything on screen (local model)
- teaching: shows me what to click, step by step, and draws diagrams or rough 3D models to explain things
- organization: window gestures, snapping, switching
- jot integration: `jot <note>` files a note in [jot](https://github.com/AksNai/jot)
- agent: carries out tasks, asks before anything risky

```powershell
pip install -r requirements.txt
python main.py
```

Needs [Ollama](https://ollama.com) with `qwen3.8:27b` or another model with vision and tool use.
