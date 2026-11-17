"""telegram-mcp: send and read Telegram bot messages as a remote MCP server (streamable HTTP).

Every request carries three headers:
  x-client-secret     one of this server's MCP tokens (MCP_TOKENS); requests without one get 401
  x-application-key   the caller's bot token from @BotFather
  x-application-id    the one chat the tools write to and read from
The server stores nothing: no tokens, no messages, no files, and it keeps no access log. The
bot token and chat ID live only in the caller's connector settings.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import TextContent
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import telegram as tg

VERSION = '1.0.0'
SECRET_HEADER = 'x-client-secret'
TOKEN_HEADER = 'x-application-key'
CHAT_HEADER = 'x-application-id'
MCP_TOKENS = [t.strip() for t in os.environ.get('MCP_TOKENS', '').split(',') if t.strip()]
PUBLIC_HOST = os.environ.get('PUBLIC_HOST', '').strip()
RATE_PER_MINUTE = int(os.environ.get('RATE_PER_MINUTE', '30'))
RATE_PER_DAY = int(os.environ.get('RATE_PER_DAY', '1000'))
FORMATS = {'plain': None, 'html': 'HTML', 'markdown': 'Markdown'}

INSTRUCTIONS = """Send messages, photos and documents to one Telegram chat through the caller's own bot, and read
the replies. The chat is fixed by the connection (header x-application-id); the tools cannot write to any
other chat. The server stores nothing.

- send_message for text (long text is split), send_photo / send_document for files (https URL, data: URL or
  base64; local paths cannot be sent), get_replies to read what was written to the bot in that chat,
  connection_status to check the setup.
- To ask the user something and wait: send_message, then get_replies with wait_seconds (up to 50; call it
  again to keep waiting).
- Use format='html' (<b>, <i>, <code>, <pre>, <a href>) for formatting; plain is the default. If Telegram
  cannot parse the markup, the text is sent plain and the reply says so.
"""


def _text(obj) -> TextContent:
    return TextContent(type='text', text=json.dumps(obj, indent=1, ensure_ascii=False))


def _clean(v: str | None) -> str:
    v = (v or '').strip()
    if v.lower().startswith('bearer '):
        v = v[7:].strip()
    return '' if v.startswith('${') else v


def _secret_ok(secret: str) -> bool:
    ok = False
    for t in MCP_TOKENS:  # compare with every token, so timing says nothing about which one matched
        ok |= hmac.compare_digest(secret.encode(), t.encode())
    return ok


def _request_secret(headers) -> str:
    return _clean(headers.get(SECRET_HEADER)) or _clean(headers.get('authorization'))


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int):
        self.per_minute, self.per_day = per_minute, per_day
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, n: int = 1) -> str | None:
        now = time.time()
        q = self.hits[key]
        while q and q[0] < now - 86400:
            q.popleft()
        last_minute = sum(1 for t in q if t > now - 60)
        if last_minute + n > self.per_minute:
            return f'Rate limit: at most {self.per_minute} sends a minute on this server. Wait a minute.'
        if len(q) + n > self.per_day:
            return f'Rate limit: at most {self.per_day} sends a day on this server.'
        q.extend([now] * n)
        return None


limiter = RateLimiter(RATE_PER_MINUTE, RATE_PER_DAY)


def _headers(ctx: Context):
    return ctx.headers or {}


def _bot(ctx: Context) -> str:
    token = _clean(_headers(ctx).get(TOKEN_HEADER))
    if token.lower().startswith('bot') and tg.TOKEN_RE.match(token[3:]):
        token = token[3:]
    if not token:
        raise ToolError(f'No bot token in this connection. Send "{TOKEN_HEADER}: <token from @BotFather>".')
    if not tg.TOKEN_RE.match(token):
        raise ToolError(f'{TOKEN_HEADER} does not look like a bot token (digits, a colon, then the secret part).')
    return token


def _chat(ctx: Context, required: bool = True) -> str:
    chat = _clean(_headers(ctx).get(CHAT_HEADER))
    if not chat:
        if not required:
            return ''
        raise ToolError(f'No chat in this connection. Send "{CHAT_HEADER}: <chat ID>". connection_status without '
                        'that header lists the chats that recently wrote to the bot, with their IDs.')
    if not tg.CHAT_RE.match(chat):
        raise ToolError(f'{CHAT_HEADER} must be a numeric chat ID (groups are negative) or @channelusername.')
    return chat


def _rate(ctx: Context, n: int = 1):
    key = hashlib.sha256(_request_secret(_headers(ctx)).encode()).hexdigest()
    msg = limiter.check(key, n)
    if msg:
        raise ToolError(msg)


async def _call(token: str, method: str, params: dict | None = None, files: dict | None = None, timeout: float = 30):
    try:
        return await anyio.to_thread.run_sync(lambda: tg.call(token, method, params, files, timeout))
    except tg.TelegramError as e:
        raise ToolError(str(e)) from None


def _when(ts) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _sent(msg: dict) -> dict:
    return {'message_id': msg.get('message_id'), 'date': _when(msg.get('date'))}


async def _send_text(token: str, params: dict) -> tuple[dict, str | None]:
    """sendMessage, falling back to plain text when Telegram cannot parse the markup."""
    try:
        return await anyio.to_thread.run_sync(lambda: tg.call(token, 'sendMessage', params)), None
    except tg.TelegramError as e:
        if params.get('parse_mode') and e.code == 400 and 'parse' in str(e).lower():
            plain = {k: v for k, v in params.items() if k != 'parse_mode'}
            msg = await _call(token, 'sendMessage', plain)
            return msg, f'Telegram could not parse the {params["parse_mode"]} markup, so this part was sent as plain text.'
        raise ToolError(str(e)) from None


mcp = MCPServer(
    name='telegram',
    title='Telegram (your own bot, one chat)',
    version=VERSION,
    instructions=INSTRUCTIONS,
)

Format = Annotated[Literal['plain', 'html', 'markdown'],
                   Field(description="plain (default), html (<b> <i> <u> <s> <code> <pre> <a href> <blockquote>) "
                                     "or markdown (Telegram's legacy Markdown: *bold* _italic_ `code`)")]
Silent = Annotated[bool, Field(description='Deliver without a notification sound')]


LONG_EXT = {'plain': 'txt', 'html': 'html', 'markdown': 'md'}
LONG_MIME = {'txt': 'text/plain; charset=utf-8', 'html': 'text/html; charset=utf-8', 'md': 'text/markdown; charset=utf-8'}


async def _send_long_as_document(ctx: Context, token: str, chat: str, text: str, format: str, silent: bool,
                                 reply_to_message_id: int | None, filename: str | None) -> list[TextContent]:
    _rate(ctx)
    ext = LONG_EXT[format]
    name = tg.safe_filename(filename, f'message.{ext}')
    if '.' not in name:
        name += f'.{ext}'
    first = next((line.strip() for line in text.splitlines() if line.strip()), '')
    caption = (first if tg.tg_len(first) <= 200 else first[:tg.fit(first, 199)] + '…') + \
        f'\n\n(Full text in the file, {len(text)} characters.)'
    params = {'chat_id': chat, 'caption': caption, 'disable_notification': silent or None}
    if reply_to_message_id:
        params['reply_parameters'] = json.dumps({'message_id': reply_to_message_id, 'allow_sending_without_reply': True})
    msg = await _call(token, 'sendDocument', params,
                      files={'document': (name, text.encode('utf-8'), LONG_MIME[ext])}, timeout=120)
    return [_text({'sent': [_sent(msg)], 'sent_as': 'document', 'file_name': name,
                   'note': f'The text is longer than one Telegram message ({tg.TEXT_LIMIT} characters), so it was '
                           'sent as a file. Pass long_text="split" to send it as several messages instead.'})]


@mcp.tool(description="""Send a text message to the connection's Telegram chat. Telegram allows 4096 characters
per message (counted in UTF-16, so an emoji counts as 2). Longer text is sent as a file (.txt, or .html / .md
for those formats) with its first line as the caption; long_text="split" sends it as several messages split
at line breaks instead. Returns the message IDs.""")
async def send_message(
    ctx: Context,
    text: Annotated[str, Field(description='The message text', min_length=1, max_length=1_000_000)],
    format: Format = 'plain',
    silent: Silent = False,
    reply_to_message_id: Annotated[int | None, Field(description='Answer this message in the chat')] = None,
    link_preview: Annotated[bool, Field(description='Show a preview for the first link')] = True,
    long_text: Annotated[Literal['document', 'split'],
                         Field(description='Text over the limit: send it as a file (default) or split it '
                                           '(up to 40000 characters)')] = 'document',
    filename: Annotated[str | None, Field(description='File name when long text is sent as a file')] = None,
) -> list[TextContent]:
    token, chat = _bot(ctx), _chat(ctx)
    if tg.tg_len(text) > tg.TEXT_LIMIT:
        if long_text == 'document':
            return await _send_long_as_document(ctx, token, chat, text, format, silent, reply_to_message_id, filename)
        if len(text) > 40000:
            raise ToolError('Too long to split (over 40000 characters, more than 10 messages). '
                            'Use long_text="document".')
    parts = tg.split_text(text)
    _rate(ctx, len(parts))
    sent, notes = [], []
    for i, part in enumerate(parts):
        params = {'chat_id': chat, 'text': part, 'parse_mode': FORMATS[format], 'disable_notification': silent or None,
                  'link_preview_options': None if link_preview else {'is_disabled': True}}
        if i == 0 and reply_to_message_id:
            params['reply_parameters'] = {'message_id': reply_to_message_id, 'allow_sending_without_reply': True}
        try:
            msg, note = await _send_text(token, params)
        except ToolError as e:
            # Telegram measures after parsing markup; if it still says too long, fall back to a file.
            if len(parts) == 1 and long_text == 'document' and 'too long' in str(e).lower():
                return await _send_long_as_document(ctx, token, chat, text, format, silent, reply_to_message_id,
                                                    filename)
            raise
        sent.append(_sent(msg))
        if note:
            notes.append(f'Part {i + 1}: {note}' if len(parts) > 1 else note)
    out = {'sent': sent, 'parts': len(parts)}
    if notes:
        out['notes'] = notes
    return [_text(out)]


async def _send_file(ctx: Context, method: str, field: str, spec: str, caption: str | None, format: str,
                     silent: bool, filename: str | None) -> list[TextContent]:
    token, chat = _bot(ctx), _chat(ctx)
    if caption and len(caption) > tg.CAPTION_LIMIT:
        raise ToolError(f'Caption too long ({len(caption)} characters, Telegram allows {tg.CAPTION_LIMIT}). '
                        'Send a shorter caption and the rest with send_message.')
    try:
        src = tg.load_file(spec)
    except tg.FileError as e:
        raise ToolError(str(e)) from None
    _rate(ctx)
    params = {'chat_id': chat, 'caption': caption or None, 'parse_mode': FORMATS[format] if caption else None,
              'disable_notification': silent or None}
    if isinstance(src, str):
        msg = await _call(token, method, {**params, field: src}, timeout=90)
    else:
        data, mime = src
        sniffed, ext = tg.sniff(data)
        mime = mime or sniffed
        name = tg.safe_filename(filename, f'{field}.{ext}')
        msg = await _call(token, method, params, files={field: (name, data, mime)}, timeout=120)
    return [_text(_sent(msg))]


@mcp.tool(description="""Send a photo to the connection's Telegram chat. photo is an https URL (Telegram
fetches it, up to 5 MB), a data: URL or base64 (up to 15 MB through this server). Telegram recompresses
photos; use send_document to deliver the original file.""")
async def send_photo(
    ctx: Context,
    photo: Annotated[str, Field(description='https URL, data: URL or base64 of a JPEG, PNG or WebP')],
    caption: Annotated[str | None, Field(description='Up to 1024 characters')] = None,
    format: Format = 'plain',
    silent: Silent = False,
) -> list[TextContent]:
    return await _send_file(ctx, 'sendPhoto', 'photo', photo, caption, format, silent, None)


@mcp.tool(description="""Send a file as a document (kept as is, not recompressed) to the connection's
Telegram chat. document is an https URL (Telegram fetches it, up to 20 MB), a data: URL or base64 (up to
15 MB through this server).""")
async def send_document(
    ctx: Context,
    document: Annotated[str, Field(description='https URL, data: URL or base64')],
    filename: Annotated[str | None, Field(description='File name shown in Telegram, for data: URLs and base64')] = None,
    caption: Annotated[str | None, Field(description='Up to 1024 characters')] = None,
    format: Format = 'plain',
    silent: Silent = False,
) -> list[TextContent]:
    return await _send_file(ctx, 'sendDocument', 'document', document, caption, format, silent, filename)


def _message(m: dict, edited: bool) -> dict:
    kind = next((k for k in ('text', 'photo', 'document', 'voice', 'audio', 'video', 'sticker', 'location',
                             'contact', 'poll') if k in m), 'other')
    sender = m.get('from') or m.get('sender_chat') or {}
    out = {'message_id': m.get('message_id'), 'date': _when(m.get('edit_date') if edited else m.get('date')),
           'from': sender.get('username') or ' '.join(filter(None, [sender.get('first_name'), sender.get('last_name')]))
           or sender.get('title'), 'type': kind}
    if m.get('text') or m.get('caption'):
        out['text'] = m.get('text') or m.get('caption')
    if kind == 'document':
        out['file_name'] = (m.get('document') or {}).get('file_name')
    if (m.get('reply_to_message') or {}).get('message_id'):
        out['reply_to_message_id'] = m['reply_to_message']['message_id']
    if edited:
        out['edited'] = True
    return out


def _update_message(u: dict) -> tuple[dict | None, bool]:
    for key, edited in (('message', False), ('edited_message', True), ('channel_post', False),
                        ('edited_channel_post', True)):
        if key in u:
            return u[key], edited
    return None, False


def _same_chat(m: dict, chat: str) -> bool:
    c = m.get('chat') or {}
    if chat.startswith('@'):
        return (c.get('username') or '').lower() == chat[1:].lower()
    return str(c.get('id')) == chat


@mcp.tool(description="""Read new messages written to the bot in the connection's chat (text, captions, and
the type of photos, files, voice and so on; file contents are not fetched). wait_seconds > 0 waits up to
that long for a first reply (long poll, max 50; call again to keep waiting).

By default the messages are marked as read at Telegram, so the next call only returns newer ones. This
marks all of the bot's pending updates as read, including any from other chats (which are never returned),
so use a bot that nothing else reads updates from. Telegram keeps unread updates for 24 hours.
mark_read=false peeks without marking (then waiting only works when nothing is pending).""")
async def get_replies(
    ctx: Context,
    wait_seconds: Annotated[int, Field(ge=0, le=50, description='Wait up to this long for a first message')] = 0,
    mark_read: Annotated[bool, Field(description='Mark the returned updates as read at Telegram')] = True,
) -> list[TextContent]:
    token, chat = _bot(ctx), _chat(ctx)
    deadline = time.monotonic() + wait_seconds
    found, offset, others = [], None, 0
    while True:
        left = max(0, int(deadline - time.monotonic()))
        updates = await _call(token, 'getUpdates', {'offset': offset, 'timeout': left, 'limit': 100}, timeout=left + 15)
        for u in updates or []:
            m, edited = _update_message(u)
            if m and _same_chat(m, chat):
                found.append(_message(m, edited))
            else:
                others += 1
        if updates and mark_read:
            offset = updates[-1]['update_id'] + 1
        if found or not mark_read or time.monotonic() >= deadline:
            break
        if not updates:
            await anyio.sleep(1)  # Telegram answered early with nothing: don't spin
    if mark_read and offset is not None:
        # Telegram marks updates as read when a later call passes an offset past them.
        await _call(token, 'getUpdates', {'offset': offset, 'timeout': 0, 'limit': 1})
    out = {'messages': found, 'marked_read': bool(mark_read and offset is not None)}
    if others:
        out['other_updates'] = f'{others} update(s) from other chats or of other kinds were skipped' + \
            (' and marked as read.' if mark_read else '.')
    if not found:
        out['note'] = 'No new messages in this chat.' + (' Use wait_seconds to wait for one.' if not wait_seconds else '')
    return [_text(out)]


@mcp.tool(description="""Check the connection: the bot's name, the chat it writes to, and whether
get_replies can read updates (it cannot while the bot has a webhook). Without the x-application-id header
it lists the chats that recently wrote to the bot, with their IDs, so you can find yours (nothing is
marked as read).""")
async def connection_status(ctx: Context) -> list[TextContent]:
    token, chat = _bot(ctx), _chat(ctx, required=False)
    me = await _call(token, 'getMe')
    hook = await _call(token, 'getWebhookInfo')
    out = {'bot': {'username': me.get('username'), 'name': me.get('first_name'), 'id': me.get('id')},
           'get_replies_works': not hook.get('url'),
           'pending_updates': hook.get('pending_update_count')}
    if hook.get('url'):
        out['webhook_note'] = 'This bot has a webhook, so get_replies cannot read its updates. Sending works.'
    if chat:
        c = await _call(token, 'getChat', {'chat_id': chat})
        out['chat'] = {'id': c.get('id'), 'type': c.get('type'),
                       'name': c.get('title') or ' '.join(filter(None, [c.get('first_name'), c.get('last_name')])),
                       'username': c.get('username')}
    else:
        chats = {}
        if not hook.get('url'):
            for u in await _call(token, 'getUpdates', {'timeout': 0, 'limit': 100}) or []:
                m, _ = _update_message(u)
                c = (m or {}).get('chat') or {}
                if c.get('id') is not None:
                    chats[c['id']] = {'id': c['id'], 'type': c.get('type'), 'username': c.get('username'),
                                      'name': c.get('title') or ' '.join(filter(None, [c.get('first_name'), c.get('last_name')]))}
        out['chat'] = None
        out['recent_chats'] = list(chats.values())
        out['note'] = (f'No {CHAT_HEADER} header. Send the bot a message in Telegram (for example /start), call this '
                       f'again, and put the chat ID in {CHAT_HEADER}.')
    return [_text(out)]


# ---------------------------------------------------------------- HTTP routes
@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> Response:
    return JSONResponse({'status': 'ok', 'version': VERSION, 'configured': bool(MCP_TOKENS)})


def build_app():
    hosts = ['127.0.0.1:*', 'localhost:*']
    origins = ['http://127.0.0.1:*', 'http://localhost:*']
    if PUBLIC_HOST:
        hosts.append(PUBLIC_HOST)
        origins.append(f'https://{PUBLIC_HOST}')
    inner = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host='0.0.0.0',
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=hosts, allowed_origins=origins),
    )

    async def guarded(scope, receive, send):
        # Everything except /health needs one of the server's MCP tokens.
        if scope['type'] == 'http' and scope['path'] != '/health':
            if not MCP_TOKENS:
                await JSONResponse({'error': 'server not configured: MCP_TOKENS is empty'}, status_code=503)(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope['headers']}
            if not _secret_ok(_request_secret(headers)):
                await JSONResponse({'error': f'unauthorized: send {SECRET_HEADER}: <token>'}, status_code=401)(
                    scope, receive, send)
                return
        await inner(scope, receive, send)

    return guarded


def main():
    import uvicorn
    uvicorn.run(build_app(), host=os.environ.get('HOST', '0.0.0.0'), port=int(os.environ.get('PORT', '8000')),
                proxy_headers=False, server_header=False, access_log=False, log_level='info')


if __name__ == '__main__':
    main()
