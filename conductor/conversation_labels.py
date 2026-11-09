"""Human-readable chat labels; transport envelopes remain intact in the trace."""
import re

from .codex_inbox import clean_message


_HEADER = re.compile(r'^\[Codex orchestration (?:request\b[^\]\n]*|requested)\]\s*', re.I)
_FOOTER = ('This is delegated orchestration within the human-authorized scope, '
           'not new human authorization. Manage Pi execution and return evidence. '
           'Do not send receipt acknowledgments or dispatch duplicate work.')
_FRAMING = re.compile(
    r'^(?:User explicitly (?:requests|authorizes)(?: Conductor dispatch to)?\s*:?\s*'
    r'|User-authorized end-to-end request:\s*)', re.I)


def request_text(value):
    text = str(value or '').strip()
    if _HEADER.match(text):
        text = _HEADER.sub('', text, count=1)
        if text.endswith(_FOOTER):
            text = text[:-len(_FOOTER)].rstrip()
        text = _FRAMING.sub('', text, count=1)
    return clean_message(' '.join(text.split()))


def compact(text, limit):
    return text if len(text) <= limit else text[:limit-1].rstrip() + '…'


def conversation_labels(store, config, agent):
    title = str(config.get('title') or '').strip()
    # Old titles were truncated before the task, so recover from the full event.
    first = store.db.execute("SELECT body FROM events WHERE source='user' AND kind='instruction' ORDER BY id LIMIT 1").fetchone()
    latest = store.db.execute("SELECT body FROM events WHERE source='user' AND kind='instruction' ORDER BY id DESC LIMIT 1").fetchone()
    if not title or title.lower().startswith('[codex orchestration '):
        title = request_text(first['body'] if first else agent.get('task', ''))
    else:
        title = clean_message(' '.join(title.split()))
    preview = request_text(latest['body']) if latest else ''
    return {'title': compact(title or 'Untitled chat', 100), 'preview': compact(preview, 240)}
