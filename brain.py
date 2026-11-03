"""The local model behind 'ask about the screen' and the agent: Ollama's chat API, nothing leaves
the PC.  Calls block, so they run on worker threads; streaming text arrives through a callback."""
import json
import threading
import time

import requests


class BrainError(Exception):
    pass


class Brain:
    def __init__(self, cfg):
        self.url = cfg.get('url', 'http://127.0.0.1:11434').rstrip('/')
        self.model = cfg.get('model', 'qwen3.8:27b')
        self.keep_alive = cfg.get('keep_alive', '15m')
        self.think = bool(cfg.get('think', False))
        self.last_used = 0.0
        self._caps = None
        self._warming = threading.Lock()

    # ---------------------------------------------------------------- status
    def capabilities(self):
        """e.g. {'completion', 'vision', 'tools', 'thinking'}; empty if Ollama can't be reached."""
        if self._caps is None:
            try:
                r = requests.post(self.url + '/api/show', json={'model': self.model}, timeout=5)
                r.raise_for_status()
                self._caps = set(r.json().get('capabilities') or [])
            except (requests.RequestException, ValueError):
                return set()
        return self._caps

    @property
    def vision(self):
        return 'vision' in self.capabilities()

    def loaded(self):
        """The model is in memory right now (answers start in about a second)."""
        try:
            r = requests.get(self.url + '/api/ps', timeout=2)
            return any(m.get('name') == self.model or m.get('model') == self.model for m in r.json().get('models', []))
        except (requests.RequestException, ValueError):
            return False

    def warm(self):
        """Start loading the model in the background (the first load takes ~30 s)."""
        if not self._warming.acquire(blocking=False):
            return

        def go():
            try:
                if not self.loaded():
                    requests.post(self.url + '/api/generate', json={'model': self.model, 'keep_alive': self.keep_alive},
                                  timeout=180)
                    self.last_used = time.time()
            except requests.RequestException:
                pass
            finally:
                self._warming.release()

        threading.Thread(target=go, daemon=True).start()

    def unload(self):
        """Free the GPU memory (when a full-screen game starts)."""
        try:
            requests.post(self.url + '/api/generate', json={'model': self.model, 'keep_alive': 0}, timeout=10)
        except requests.RequestException:
            pass
        self.last_used = 0.0

    # ---------------------------------------------------------------- chat
    def chat(self, messages, tools=None, on_text=None, stop=None, timeout=300, fmt=None, think=None):
        """Returns the assistant message {'role', 'content', 'tool_calls'?}.  on_text(text_so_far) is
        called as text streams in; setting the `stop` Event abandons the request.  fmt: a JSON
        schema the reply must follow."""
        body = {'model': self.model, 'messages': messages, 'stream': True, 'keep_alive': self.keep_alive,
                'think': self.think if think is None else think,
                'options': {'temperature': 0 if fmt else 0.2}}
        if tools:
            body['tools'] = tools
        if fmt:
            body['format'] = fmt
        self.last_used = time.time()
        try:
            r = requests.post(self.url + '/api/chat', json=body, stream=True, timeout=(5, timeout))
        except requests.ConnectionError:
            raise BrainError("Ollama isn't running. Start it (or open jot once) and try again.")
        except requests.RequestException as e:
            raise BrainError(f'Could not reach the model: {e}')
        if r.status_code != 200:
            try:
                msg = r.json().get('error', r.text)
            except ValueError:
                msg = r.text
            raise BrainError(f'The model returned an error: {msg[:200]}')
        text, calls = '', []
        try:
            for line in r.iter_lines():
                if stop is not None and stop.is_set():
                    break
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get('error'):
                    raise BrainError(chunk['error'])
                m = chunk.get('message') or {}
                if m.get('content'):
                    text += m['content']
                    if on_text:
                        on_text(text)
                calls += m.get('tool_calls') or []
                if chunk.get('done'):
                    break
        except requests.RequestException as e:
            raise BrainError(f'The model stopped answering: {e}')
        finally:
            r.close()
            self.last_used = time.time()
        out = {'role': 'assistant', 'content': text}
        if calls:
            out['tool_calls'] = calls
        return out
