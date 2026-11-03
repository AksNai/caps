# Caps

I built this to make my computer easier to use. 
Tap Caps Lock and type, or hold it and move the mouse.

- focus: blocks distracting apps and sites for a set time
- queries: ask about anything on screen (local model)
- organization: window gestures, snapping, switching
- jot integration: `jot <note>` files a note in [jot](https://github.com/AksNai/jot)
- agent: carries out tasks, asks before anything risky

```powershell
pip install -r requirements.txt
python main.py
```

The model features need [Ollama](https://ollama.com) with `qwen3.8:27b` or another model with vision and tool use.
