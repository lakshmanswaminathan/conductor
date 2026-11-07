"""OpenRouter adapter and an owner-only local broker for terminal workers."""
import json
import base64
import os
import re
from pathlib import Path
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from .models import DEFAULT_MODEL


def api_error(status, diagnostic=None):
    messages = {
        401: 'OpenRouter rejected the API key (401). Open Settings and paste a valid OpenRouter key, without quotes or a Bearer prefix, then save and retry.',
        402: 'OpenRouter reports insufficient credits (402). Check the account balance and key spending limit before retrying.',
        403: 'OpenRouter denied this request (403). Check the key permissions and account restrictions.',
        429: 'OpenRouter rate limit reached (429). Wait briefly before retrying.',
        400: 'OpenRouter rejected the request (400). Check model and tool-schema compatibility.',
    }
    if status == 400 and diagnostic:
        return 'OpenRouter rejected the tool schema (400): ' + diagnostic
    return messages.get(status, f'OpenRouter HTTP {status}; response body omitted')


def schema_diagnostic(body):
    """Only expose a known schema path/reason, never echoed prompts or credentials."""
    try:
        error = json.loads(body).get('error', {})
        raw = error.get('metadata', {}).get('raw')
        if isinstance(raw, str):
            error = json.loads(raw).get('error', {})
        message = error.get('message', '')
        if not isinstance(message, str):
            return None
        match = re.search(r"At path '([A-Za-z0-9_.\[\]-]{1,160})': type is not defined", message)
        if match:
            return f"{match.group(1)} is missing its type."
        if 'json schema' in message.lower():
            return 'The model provider could not validate a tool parameter schema.'
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def validate_key(key):
    """Validate through the read-only key endpoint; never return account metadata."""
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 4096:
        raise ValueError('Enter your OpenRouter API key')
    key = key.strip()
    if (not key.isascii() or any(c.isspace() for c in key) or
            any(c in key for c in '\"\'`') or '=' in key):
        raise ValueError('Paste only the OpenRouter key, without quotes, a Bearer prefix, or an export command.')
    request = urllib.request.Request('https://openrouter.ai/api/v1/key',
        headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(65537))
        if not isinstance(result, dict) or not isinstance(result.get('data'), dict) or 'error' in result:
            raise RuntimeError('OpenRouter could not verify the key. Try saving again.')
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise ValueError(api_error(status)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('Could not reach OpenRouter to verify the key. Check your connection and save again.') from None
    except (ValueError, UnicodeError):
        raise RuntimeError('OpenRouter returned an invalid key-verification response. Try saving again.') from None
    return key


class OpenRouter:
    def __init__(self, model, key=None, max_calls=200, state_path=None):
        self.model = model
        self.key = key or os.environ.get('OPENROUTER_API_KEY')
        if not self.key:
            raise ValueError('Set OPENROUTER_API_KEY in the coordinator environment; do not pass it as a command argument.')
        self.lock = threading.Lock()
        self.calls = 0
        self.max_calls = max_calls
        self.usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'cost': 0.0}
        self.state_path = state_path
        if state_path:
            from .store import Store
            store = Store(state_path)
            saved = store.config()
            self.calls = saved.get('api_calls', 0)
            self.usage.update(saved.get('usage', {}))
            store.close()

    def persist(self):
        if self.state_path:
            from .store import Store
            store = Store(self.state_path)
            try:
                store.configure(api_calls=self.calls, usage=self.usage)
            finally:
                store.close()

    def complete(self, messages, tools, max_tokens=None):
        if max_tokens is None:
            max_tokens = 16384 if self.model == DEFAULT_MODEL else 4096
        wire_messages = []
        latest_image = max((i for i, m in enumerate(messages) if m.get('_image_path')), default=-1)
        for index, message in enumerate(messages):
            clean = {k: v for k, v in message.items() if k != '_image_path'}
            if index == latest_image:
                path = Path(message['_image_path']).resolve()
                if not self.state_path or not path.is_relative_to((Path(self.state_path) / 'artifacts').resolve()):
                    raise ValueError('Screenshot must be a generated run artifact')
                if path.stat().st_size > 10_000_000:
                    raise ValueError('Screenshot exceeds image size limit')
                clean['content'] = [{'type': 'text', 'text': message.get('content', '')},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()}}]
            wire_messages.append(clean)
        body = {'model': self.model, 'messages': wire_messages, 'max_tokens': max_tokens,
                'reasoning': {'enabled': True}, 'provider': {'require_parameters': True}}
        if self.model == DEFAULT_MODEL:
            # K3.1 defaults to max effort, which can exhaust a small output cap
            # before emitting a response. Low is one of its supported efforts.
            body['reasoning'] = {'effort': 'low'}
        if tools:
            body['tools'] = tools
        for attempt in range(3):
            with self.lock:
                if self.calls >= self.max_calls:
                    raise RuntimeError('Run API request ceiling reached')
                self.calls += 1
                self.persist()  # Reserve before sending; a crash cannot reset the ceiling.
            request = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + self.key,
                'Content-Type': 'application/json', 'X-Title': 'Conductor open-source harness'})
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.load(response)
                if 'error' in result:
                    raise RuntimeError('OpenRouter returned an API error; response body omitted to protect request data')
                usage = result.get('usage', {})
                with self.lock:
                    for key in self.usage:
                        self.usage[key] += usage.get(key, 0) or 0
                    self.persist()
                choice = result['choices'][0]
                if choice.get('finish_reason') == 'length':
                    raise RuntimeError(f'Model output limit reached ({max_tokens:,} tokens); incomplete output was not executed')
                message = choice['message']
                if not message.get('content') and not message.get('tool_calls'):
                    raise RuntimeError('Model returned no usable content or tool calls')
                # Keep reasoning_details verbatim across tool exchanges.
                return {k: v for k, v in message.items() if k in {'role', 'content', 'tool_calls', 'reasoning', 'reasoning_details'}}
            except urllib.error.HTTPError as exc:
                status = exc.code
                diagnostic = schema_diagnostic(exc.read(16384)) if status == 400 else None
                exc.close()
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(api_error(status, diagnostic)) from None
                time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError):
                raise RuntimeError('OpenRouter connection failed; request outcome unknown, no automatic replay') from None


class Broker:
    def __init__(self, path, provider):
        self.path, self.provider = str(path), provider

    def __enter__(self):
        provider = self.provider
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(240)
                try:
                    raw = self.rfile.readline(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError('Request too large')
                    request = json.loads(raw)
                    response = {'message': provider.complete(request['messages'], request['tools'], request.get('max_tokens'))}
                except Exception as exc:
                    response = {'error': str(exc)}
                self.wfile.write(json.dumps(response).encode() + b'\n')
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        self.server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        os.unlink(self.path)


class RemoteProvider:
    def __init__(self, path):
        self.path = str(path)

    def complete(self, messages, tools, max_tokens=None):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(240)
            client.connect(self.path)
            client.sendall(json.dumps({'messages': messages, 'tools': tools, 'max_tokens': max_tokens}).encode() + b'\n')
            with client.makefile('rb') as stream:
                result = json.loads(stream.readline(2_000_001))
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result['message']
