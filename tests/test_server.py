"""Offline end-to-end tests: a stub Telegram Bot API, the real server app, a real MCP client.
Nothing here reaches Telegram."""
import asyncio
import base64
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


BOT = '123456789:AAFakeTokenForTestsOnly_abcdefghijklmn'
BAD_BOT = '123456789:AAThisTokenIsRefusedByTheStub_abcdefgh'
CHAT = '4242'
SECRET = 'test-secret-1'
CALLS = []          # (method, params or raw body) per Bot API call
UPDATES = []        # pending updates, as Telegram would hold them


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        _, token, method = self.path.split('/', 2)
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        params = json.loads(raw) if self.headers['Content-Type'] == 'application/json' else {'_multipart': raw}
        CALLS.append((method, params))
        if token != f'bot{BOT}':
            return self.reply({'ok': False, 'error_code': 401, 'description': 'Unauthorized'}, 401)
        if method in ('sendMessage', 'sendPhoto', 'sendDocument'):
            if params.get('chat_id') not in (None, CHAT):
                return self.reply({'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'}, 400)
            if params.get('parse_mode') == 'HTML' and '<b>' in params.get('text', '') and '</b>' not in params['text']:
                return self.reply({'ok': False, 'error_code': 400,
                                   'description': "Bad Request: can't parse entities: unclosed tag"}, 400)
            text = params.get('text', '')
            if method == 'sendMessage' and (len(text.encode('utf-16-le')) // 2 > 4096 or 'TOOLONG' in text):
                return self.reply({'ok': False, 'error_code': 400, 'description': 'Bad Request: message is too long'}, 400)
            return self.reply({'ok': True, 'result': {'message_id': len(CALLS), 'date': 1763370000}})
        if method == 'getUpdates':
            offset = params.get('offset')
            if offset is not None:
                UPDATES[:] = [u for u in UPDATES if u['update_id'] >= offset]
            return self.reply({'ok': True, 'result': UPDATES[:params.get('limit', 100)]})
        if method == 'getMe':
            return self.reply({'ok': True, 'result': {'id': 123456789, 'username': 'test_bot', 'first_name': 'Test'}})
        if method == 'getWebhookInfo':
            return self.reply({'ok': True, 'result': {'url': '', 'pending_update_count': len(UPDATES)}})
        if method == 'getChat':
            return self.reply({'ok': True, 'result': {'id': int(CHAT), 'type': 'private', 'first_name': 'Owner'}})
        self.reply({'ok': False, 'error_code': 404, 'description': 'Not Found'}, 404)


stub_port, app_port = free_port(), free_port()
threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', stub_port), Stub).serve_forever, daemon=True).start()
os.environ.update({'TELEGRAM_API_BASE': f'http://127.0.0.1:{stub_port}', 'MCP_TOKENS': f'{SECRET}, other-secret',
                   'RATE_PER_MINUTE': '1000'})

import uvicorn  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

from telegram_mcp import server, telegram  # noqa: E402

_srv = uvicorn.Server(uvicorn.Config(server.build_app(), host='127.0.0.1', port=app_port, log_level='warning'))
threading.Thread(target=_srv.run, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{app_port}/health', timeout=1)
        break
    except Exception:
        time.sleep(0.1)

URL = f'http://127.0.0.1:{app_port}/mcp'
GOOD = {'x-client-secret': SECRET, 'x-application-key': BOT, 'x-application-id': CHAT}


async def _call(tool, args, headers, url=None):
    async with create_mcp_http_client(headers=headers) as http, \
            streamable_http_client(url or URL, http_client=http) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        if tool is None:
            return await s.list_tools()
        return await s.call_tool(tool, args)


def run(tool, args=None, headers=None, url=None):
    CALLS.clear()
    return asyncio.run(_call(tool, args or {}, GOOD if headers is None else headers, url))


def body(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def error(result):
    assert result.is_error
    return result.content[0].text


def status_without(headers, url=None):
    req = urllib.request.Request(url or URL, data=b'{}', headers={'Content-Type': 'application/json', **headers},
                                 method='POST')
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_mcp_needs_one_of_the_tokens():
    assert status_without({}) == 401
    assert status_without({'x-client-secret': 'wrong'}) == 401
    assert status_without({'Authorization': 'Bearer nope'}) == 401
    names = {t.name for t in run(None).tools}
    assert names == {'send_message', 'send_photo', 'send_document', 'get_replies', 'connection_status'}
    # The second token and the Authorization header work too.
    assert run(None, headers={'Authorization': 'Bearer other-secret'}).tools


def test_mcp_suffix_aliases_are_the_same_endpoint():
    base = f'http://127.0.0.1:{app_port}'
    for suffix in ('/mcp/inbox', '/mcp/hkarn/monitor', '/mcp/a.b_c~d-1/'):
        names = {t.name for t in run(None, url=base + suffix).tools}
        assert 'send_message' in names, suffix
        assert status_without({}, base + suffix) == 401, suffix  # still needs a token
    out = body(run('send_message', {'text': 'via alias'}, url=base + '/mcp/inbox'))
    assert out['parts'] == 1
    (method, params), = CALLS
    assert method == 'sendMessage' and params['chat_id'] == CHAT


def test_other_paths_are_not_aliases():
    base = f'http://127.0.0.1:{app_port}'
    for path in ('/mcpx', '/mcp/a%20b', '/mcp//x', '/mcp/x/../y', '/mcp/' + 'a' * 130, '/other/mcp'):
        assert status_without({'x-client-secret': SECRET}, base + path) == 404, path


def test_health_is_open():
    assert json.loads(urllib.request.urlopen(f'http://127.0.0.1:{app_port}/health').read())['configured'] is True


def test_send_message_goes_to_the_header_chat_only():
    out = body(run('send_message', {'text': 'hello'}))
    assert out['parts'] == 1 and out['sent'][0]['message_id']
    (method, params), = CALLS
    assert method == 'sendMessage' and params['chat_id'] == CHAT and params['text'] == 'hello'
    assert 'parse_mode' not in params


def test_long_text_is_split_when_asked():
    text = ('line of text\n' * 700).strip()
    out = body(run('send_message', {'text': text, 'long_text': 'split'}))
    assert out['parts'] == 3
    assert all(telegram.tg_len(p['text']) <= telegram.TEXT_LIMIT for _, p in CALLS)
    assert '\n'.join(p['text'] for _, p in CALLS) == text


def test_split_keeps_blank_lines():
    text = 'a' * 3000 + '\n\n\n' + 'b' * 3000
    parts = telegram.split_text(text)
    assert parts == ['a' * 3000 + '\n\n', 'b' * 3000]   # only the break it splits at is dropped
    assert '\n'.join(parts) == text
    hard = 'x' * 5000
    assert ''.join(telegram.split_text(hard)) == hard


def test_oversized_body_is_refused_before_parsing():
    big = server.MAX_BODY_BYTES + 1
    req = urllib.request.Request(URL, data=b' ' * big, method='POST',
                                 headers={'Content-Type': 'application/json', 'x-client-secret': SECRET})
    try:
        urllib.request.urlopen(req, timeout=10)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except (ConnectionError, urllib.error.URLError):
        code = 413  # the server answered 413 and closed before reading the whole body
    assert code == 413


def test_long_text_is_sent_as_a_file_by_default():
    text = 'Weekly report\n' + 'detail line\n' * 400
    out = body(run('send_message', {'text': text}))
    assert out['sent_as'] == 'document' and out['file_name'] == 'message.txt'
    (method, params), = CALLS
    raw = params['_multipart']
    assert method == 'sendDocument' and text.encode() in raw and b'text/plain; charset=utf-8' in raw
    assert 'Weekly report\n\n(Full text in the file'.encode() in raw
    body(run('send_message', {'text': text, 'format': 'html', 'filename': 'report'}))
    assert b'filename="report.html"' in CALLS[0][1]['_multipart']


def test_emoji_count_double_toward_the_limit():
    text = '😀' * 2100            # 2100 characters but 4200 UTF-16 units: over the limit
    assert len(text) < telegram.TEXT_LIMIT < telegram.tg_len(text)
    assert body(run('send_message', {'text': text}))['sent_as'] == 'document'
    out = body(run('send_message', {'text': text, 'long_text': 'split'}))
    assert out['parts'] == 2 and all(telegram.tg_len(p['text']) <= telegram.TEXT_LIMIT for _, p in CALLS)


def test_too_long_after_markup_falls_back_to_a_file():
    out = body(run('send_message', {'text': 'TOOLONG short raw text', 'format': 'html'}))
    assert out['sent_as'] == 'document'
    assert [m for m, _ in CALLS] == ['sendMessage', 'sendDocument']


def test_bad_markup_falls_back_to_plain():
    out = body(run('send_message', {'text': '<b>unclosed', 'format': 'html'}))
    assert 'plain text' in out['notes'][0]
    assert [p.get('parse_mode') for _, p in CALLS] == ['HTML', None]


def test_refused_token_is_explained_and_never_echoed():
    msg = error(run('send_message', {'text': 'x'}, headers={**GOOD, 'x-application-key': BAD_BOT}))
    assert '401' in msg and BAD_BOT not in msg and BAD_BOT.split(':')[1] not in msg


def test_missing_or_malformed_headers():
    assert 'x-application-key' in error(run('send_message', {'text': 'x'}, headers={'x-client-secret': SECRET}))
    no_chat = {k: v for k, v in GOOD.items() if k != 'x-application-id'}
    assert 'x-application-id' in error(run('send_message', {'text': 'x'}, headers=no_chat))
    assert 'numeric chat ID' in error(run('send_message', {'text': 'x'}, headers={**GOOD, 'x-application-id': '../x'}))
    assert 'bot token' in error(run('send_message', {'text': 'x'}, headers={**GOOD, 'x-application-key': 'abc/../x'}))
    assert CALLS == []


def test_document_from_base64_is_uploaded_with_its_name():
    data = base64.b64encode(b'%PDF-1.4 test').decode()
    body(run('send_document', {'document': data, 'filename': 'räkning 1.pdf', 'caption': 'Här'}))
    (method, params), = CALLS
    raw = params['_multipart']
    assert method == 'sendDocument'
    assert 'filename="räkning 1.pdf"'.encode() in raw and b'application/pdf' in raw and b'%PDF-1.4 test' in raw
    assert b'name="chat_id"\r\n\r\n4242' in raw


def test_photo_url_is_passed_to_telegram():
    body(run('send_photo', {'photo': 'https://example.com/a.png'}))
    (method, params), = CALLS
    assert method == 'sendPhoto' and params['photo'] == 'https://example.com/a.png'


def test_local_paths_and_long_captions_are_refused():
    assert 'local paths' in error(run('send_photo', {'photo': 'C:\\pics\\a.png'}))
    assert 'Caption too long' in error(run('send_photo', {'photo': 'https://e.com/a.png', 'caption': 'x' * 1100}))


def test_get_replies_returns_only_this_chat_and_marks_read():
    UPDATES[:] = [
        {'update_id': 10, 'message': {'message_id': 5, 'date': 1763370000, 'chat': {'id': 4242},
                                      'from': {'first_name': 'Owner'}, 'text': 'yes please'}},
        {'update_id': 11, 'message': {'message_id': 1, 'date': 1763370001, 'chat': {'id': 999},
                                      'from': {'first_name': 'Stranger'}, 'text': 'secret of someone else'}},
    ]
    out = body(run('get_replies'))
    assert [m['text'] for m in out['messages']] == ['yes please']
    assert 'secret of someone else' not in json.dumps(out)
    assert out['marked_read'] and UPDATES == []
    assert body(run('get_replies'))['messages'] == []


def test_get_replies_peek_keeps_updates():
    UPDATES[:] = [{'update_id': 20, 'message': {'message_id': 6, 'date': 1, 'chat': {'id': 4242}, 'text': 'hi'}}]
    out = body(run('get_replies', {'mark_read': False}))
    assert out['messages'][0]['text'] == 'hi' and len(UPDATES) == 1
    UPDATES.clear()


def test_connection_status_without_chat_lists_recent_chats():
    UPDATES[:] = [{'update_id': 30, 'message': {'message_id': 1, 'date': 1, 'text': 'private words',
                                                'chat': {'id': 4242, 'type': 'private', 'first_name': 'Owner'}}}]
    no_chat = {k: v for k, v in GOOD.items() if k != 'x-application-id'}
    out = body(run('connection_status', headers=no_chat))
    assert out['bot']['username'] == 'test_bot' and out['recent_chats'][0]['id'] == 4242
    assert 'private words' not in json.dumps(out) and len(UPDATES) == 1
    UPDATES.clear()
    assert body(run('connection_status'))['chat']['id'] == 4242


def test_rate_limit_counts_message_parts():
    lim = server.RateLimiter(per_minute=2, per_day=100)
    assert lim.check('k', 2) is None
    assert 'minute' in lim.check('k')
