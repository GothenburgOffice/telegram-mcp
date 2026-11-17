# telegram-mcp

A remote MCP server (streamable HTTP) that lets Claude, or any MCP client, send messages, photos
and files to **one Telegram chat** through **your own bot**, and read the replies. It works in
every Claude surface through a custom connector.

You run it yourself: a ready-built image is published on GHCR (see [Install](#install)). Only
you can use your server, because every request needs an MCP token you set in its `.env`.

## What it stores: nothing

- **No bot tokens, no chat IDs.** Each connection sends its own bot token and chat ID in headers
  on every request. They live only in your connector settings.
- **No messages, no files.** Text and files go straight to Telegram; replies are read from
  Telegram and passed back. Nothing is written to disk (the container runs read-only with no
  volume), and there is no access log.
- **One chat per connection.** The tools have no `chat_id` argument: they write to and read from
  only the chat in the `x-application-id` header. Replies from other chats are never returned.

Sending Telegram messages costs nothing, so the server is not an open relay: every request needs
one of the MCP tokens from the server's `.env`.

## Headers

| Header | Value |
|---|---|
| `x-client-secret` | One of the server's `MCP_TOKENS` (or `Authorization: Bearer <token>`) |
| `x-application-key` | Your bot token from [@BotFather](https://t.me/BotFather), like `123456789:AA...` |
| `x-application-id` | The chat ID: your user ID for a private chat, a negative ID for a group, or `@channelname` |

## Set up a bot

1. In Telegram, talk to [@BotFather](https://t.me/BotFather): `/newbot`, pick a name, and copy the token.
2. Open your new bot and send it `/start` (a bot can only write to chats that have talked to it;
   for a group, add the bot to the group).
3. Find the chat ID: connect once **without** `x-application-id` and run `connection_status`. It
   lists the chats that recently wrote to the bot, with their IDs. Or open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser and read `message.chat.id`.
4. Use a bot for this alone: `get_replies` marks the bot's updates as read, and it cannot read
   them while the bot has a webhook (sending still works).

## Connect a client

**Claude (claude.ai, Desktop, mobile):** Settings → Connectors → Add custom connector. URL
`https://<your host>/mcp`, and the three headers above. Connectors reach your server from the
internet, so it needs a public HTTPS address.

**Several bots or chats in one client:** `/mcp/<anything>` is the same endpoint as `/mcp`, for
example `https://<your host>/mcp/alerts` and `https://<your host>/mcp/inbox-monitor`. Add one
connector per URL, each with its own bot token and chat ID, where a client accepts only one
connector per URL. The suffix is one or more path segments of letters, digits and `- _ . ~`, up to
128 characters in all. It is only a name, answered in place (no redirect), and every alias needs an
MCP token like `/mcp`. A `#` fragment cannot do this: clients never send it to the server.

**Claude Code:**

```sh
claude mcp add --transport http telegram https://<your host>/mcp \
  --header "x-client-secret: <MCP token>" \
  --header "x-application-key: <bot token>" \
  --header "x-application-id: <chat ID>"
```

## Tools

| Tool | What it does |
|---|---|
| `send_message` | Text, `format` plain / html / markdown, `silent`, `reply_to_message_id`, `link_preview`. Text over Telegram's 4096-character limit (counted in UTF-16, so an emoji is 2) is sent as a file (`.txt`, `.html` or `.md`) with its first line as the caption; `long_text: "split"` sends several messages instead. Markup Telegram can't parse is sent as plain text, and the reply says so |
| `send_photo` | A photo from an https URL (Telegram fetches it, up to 5 MB), a data: URL or base64 (up to 15 MB), with an optional caption |
| `send_document` | Any file, kept as is: https URL (up to 20 MB), data: URL or base64 (up to 15 MB), with a file name and caption |
| `get_replies` | New messages in the chat (text, captions, and the type of other messages; file contents are not fetched). `wait_seconds` (up to 50) waits for a reply; `mark_read=false` peeks |
| `connection_status` | The bot, the chat, and whether `get_replies` can work; without a chat ID, the chats that recently wrote to the bot |

To ask and wait: `send_message`, then `get_replies` with `wait_seconds: 50`, repeated as needed.

## Install

The CI publishes the image after the tests and an image health check pass on `main`:
`ghcr.io/gothenburgoffice/telegram-mcp:sha-<commit>` for each commit, and `:latest`. Pinning a
`sha-` tag means an update happens only when you choose it. The commit is the full 40-character
sha of a `main` commit whose **Verify** run passed (Actions tab; the run summary names the image).

You need Docker with Compose on a machine with a public HTTPS address, or only on your own machine
if you use it from Claude Code there (see [Local only](#local-only)).

```sh
mkdir telegram-mcp && cd telegram-mcp
sha=<40-character commit>    # a main commit whose Verify run passed
base=https://raw.githubusercontent.com/gothenburgoffice/telegram-mcp/$sha

# The compose file and the settings template, from that same commit
curl -fsSL -o compose.images.yaml "$base/compose.images.yaml"
curl -fsSL -o .env "$base/.env.example"
chmod 600 .env

# Fill in .env: your host name, one MCP token, and the pinned image
sed -i   -e "s|^PUBLIC_HOST=.*|PUBLIC_HOST=telegram.example.com|"   -e "s|^MCP_TOKENS=.*|MCP_TOKENS=$(openssl rand -hex 32)|"   -e "s|^TELEGRAM_MCP_IMAGE=.*|TELEGRAM_MCP_IMAGE=ghcr.io/gothenburgoffice/telegram-mcp:sha-$sha|"   .env
grep ^MCP_TOKENS= .env       # copy this token into your password manager and your connector

docker compose -f compose.images.yaml up -d
curl -fsS http://127.0.0.1:3440/health                                       # {"status":"ok",...,"configured":true}
curl -s -o /dev/null -w '%{http_code}
' -X POST http://127.0.0.1:3440/mcp   # 401: no token, as it should be
```

The container listens only on `127.0.0.1:3440` (`APP_PORT`). Put an HTTPS reverse proxy in front
of it. With [Caddy](https://caddyserver.com), which gets the certificate itself:

```
telegram.example.com {
	request_body {
		max_size 25MB
	}
	reverse_proxy 127.0.0.1:3440
}
```

Leave access logging off in the proxy: requests carry your bot token in a header, and access logs
record headers.

**Update:** set `TELEGRAM_MCP_IMAGE` in `.env` to the new `sha-` tag, download that commit's
`compose.images.yaml` again, and run `docker compose -f compose.images.yaml up -d`.

**Tokens:** `MCP_TOKENS` takes several comma-separated tokens, so each person or device can have
its own. To revoke one, remove it from `.env` and run `docker compose -f compose.images.yaml up -d`.

### Local only

To use it only from Claude Code on the same machine, skip the proxy. Set `PUBLIC_HOST` to
`localhost` and connect to `http://127.0.0.1:3440/mcp`. Claude's custom connectors (claude.ai,
Desktop, mobile) can't reach a local address.

### Without Compose

```sh
docker run -d --name telegram-mcp --restart unless-stopped --read-only --tmpfs /tmp   --security-opt no-new-privileges:true -p 127.0.0.1:3440:8000   -e PUBLIC_HOST=telegram.example.com -e MCP_TOKENS=<token>   ghcr.io/gothenburgoffice/telegram-mcp:sha-<commit>
```

### Settings

| Variable | Default | |
|---|---|---|
| `PUBLIC_HOST` | (required) | The public host name, for the DNS-rebinding check |
| `MCP_TOKENS` | (required) | Comma-separated tokens; `/mcp` answers 401 without one, 503 if none is set |
| `APP_PORT` | 3440 | Host port on `127.0.0.1` (Compose only) |
| `RATE_PER_MINUTE` / `RATE_PER_DAY` | 30 / 1000 | Sends per MCP token (each message part or file counts) |
| `MAX_UPLOAD_MB` | 15 | Largest data: URL or base64 file the server forwards |
| `TELEGRAM_MCP_IMAGE` | `:latest` | The image Compose runs; pin a `sha-` tag |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | For tests or a local Bot API server |

## Develop

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests     # offline: a stub Bot API, no real Telegram calls
```

## License

MIT
