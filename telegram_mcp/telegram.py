"""A small client for the Telegram Bot API (https://core.telegram.org/bots/api), standard library only.

The bot token is part of every request URL, so nothing here ever puts a URL, an exception text from
urllib or the token itself into an error message.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid

API = os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org').rstrip('/')
TOKEN_RE = re.compile(r'^\d{3,20}:[A-Za-z0-9_-]{30,64}$')
CHAT_RE = re.compile(r'^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$')
TEXT_LIMIT = 4096       # per message, in UTF-16 code units after markup is parsed (an emoji is 2)
CAPTION_LIMIT = 1024
MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', '15')) * 1024 * 1024


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _explain(code: int | None, desc: str, retry_after: int | None) -> str:
    d = desc.lower()
    if code == 401 or 'unauthorized' in d:
        return 'Telegram refused the bot token (401 Unauthorized). Check x-application-key: the token from @BotFather.'
    if code == 429:
        return f'Telegram rate limit: retry after {retry_after or "a few"} seconds.'
    if 'chat not found' in d:
        return ('Telegram: chat not found. The bot can only write to a chat that has talked to it: open the bot '
                'in Telegram and send /start (or add it to the group), and check x-application-id.')
    if code == 403:
        return f'Telegram refused (403): {desc}. The user may have blocked the bot, or it is not a member of the chat.'
    if code == 409:
        return ('Telegram: this bot has a webhook set, or another program is reading its updates (409). '
                f'get_replies needs to be the only reader of the bot\'s updates. ({desc})')
    return f'Telegram error {code}: {desc}'


def call(token: str, method: str, params: dict | None = None, files: dict | None = None, timeout: float = 30):
    """POST a Bot API method; return its "result" or raise TelegramError."""
    url = f'{API}/bot{token}/{method}'
    if files:
        body, ctype = _multipart(params or {}, files)
    else:
        body, ctype = json.dumps({k: v for k, v in (params or {}).items() if v is not None}).encode(), 'application/json'
    req = urllib.request.Request(url, data=body, headers={'Content-Type': ctype}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise TelegramError(f'Telegram answered HTTP {e.code}.', e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TelegramError('Could not reach Telegram (network error or timeout).') from None
    except ValueError:
        raise TelegramError('Telegram sent a reply that is not JSON.') from None
    if not isinstance(data, dict) or not data.get('ok'):
        data = data if isinstance(data, dict) else {}
        retry = (data.get('parameters') or {}).get('retry_after')
        code = data.get('error_code')
        raise TelegramError(_explain(code, str(data.get('description', 'unknown error')), retry), code, retry)
    return data.get('result')


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(text.encode() + b'\r\n')
    for name, (filename, data, ctype) in files.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                  f'Content-Type: {ctype}\r\n\r\n'.encode())
        out.write(data + b'\r\n')
    out.write(f'--{boundary}--\r\n'.encode())
    return out.getvalue(), f'multipart/form-data; boundary={boundary}'


class FileError(ValueError):
    pass


def safe_filename(name: str | None, fallback: str) -> str:
    name = re.sub(r'[^\w.\- ]', '_', (name or '').strip())[:100].strip(' .')
    return name or fallback


def sniff(data: bytes) -> tuple[str, str]:
    """(mime type, extension) for the common image types; otherwise a generic binary type."""
    if data.startswith(b'\x89PNG'):
        return 'image/png', 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', 'jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', 'gif'
    if data.startswith(b'%PDF'):
        return 'application/pdf', 'pdf'
    return 'application/octet-stream', 'bin'


def load_file(spec: str) -> str | tuple[bytes, str | None]:
    """An http(s) URL is returned as is (Telegram fetches it). A data: URL or bare base64 is decoded and
    returned as (bytes, mime type or None)."""
    s = (spec or '').strip()
    if not s:
        raise FileError('No file given.')
    if re.match(r'^https?://', s, re.I):
        return s
    mime = None
    if s.startswith('data:'):
        head, _, s = s.partition(',')
        if ';base64' not in head:
            raise FileError('Only base64 data: URLs are supported.')
        mime = head[5:].split(';')[0] or None
    elif re.match(r'^([A-Za-z]:[\\/]|/|~|\.{1,2}[\\/]|file:)', s):
        raise FileError('The server cannot read local paths. Pass an https URL, a data: URL or base64.')
    s = re.sub(r'\s+', '', s)
    if len(s) * 3 // 4 > MAX_UPLOAD_BYTES + 3:
        raise FileError(f'File too large: at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB through this server. '
                        'Pass an https URL instead (Telegram fetches photos up to 5 MB and documents up to 20 MB).')
    try:
        data = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise FileError('Not an https URL, a data: URL or valid base64.') from None
    if not data:
        raise FileError('The file is empty.')
    return data, mime


def tg_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units (characters outside the BMP, like emoji, are 2)."""
    return len(text.encode('utf-16-le')) // 2


def fit(text: str, limit: int) -> int:
    """The number of leading characters of text whose Telegram length is at most limit."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return i
    return len(text)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split a long message at line breaks (else spaces) into parts Telegram accepts."""
    parts = []
    while tg_len(text) > limit:
        end = fit(text, limit)
        sep = 1  # drop only the one line break or space the split happens at; keep blank lines
        cut = text.rfind('\n', 0, end)
        if cut < end // 2:
            cut = text.rfind(' ', 0, end)
        if cut < end // 2:
            cut, sep = end, 0
        parts.append(text[:cut])
        text = text[cut + sep:]
    parts.append(text)
    return parts
