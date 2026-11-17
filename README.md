# telegram-mcp

A remote MCP server (streamable HTTP) that lets Claude, or any MCP client, send messages, photos
and files to **one Telegram chat** through **your own bot**, and read the replies. It works in
every Claude surface through a custom connector.

Hosted at `https://telegrammcp.gothenburgoffice.se/mcp`; access needs an MCP token from the
operator. You can also run it yourself (below).

## What it stores: nothing

- **No bot tokens, no chat IDs.** Each connection sends its own bot token and chat ID in headers
  on every request. They live only in your connector settings.
- **No messages, no files.** Text and files go straight to Telegram; replies are read from
  Telegram and passed back. Nothing is written to disk (the container runs read-only with no
  volume), and there is no access log.
- **One chat per connection.** The tools have no `chat_id` argument: they write to and read from
  only the chat in the `x-application-id` header. Replies from other chats are never returned.

Sending Telegram messages costs nothing, so the server is not an open relay: every request needs
one of the operator's MCP tokens.

## Headers

| Header | Value |
|---|---|
| `x-client-secret` | An MCP token of this server (or `Authorization: Bearer <token>`) |
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
`https://telegrammcp.gothenburgoffice.se/mcp`, and the three headers above.

**Claude Code:**

```sh
claude mcp add --transport http telegram https://telegrammcp.gothenburgoffice.se/mcp \
  --header "x-client-secret: <MCP token>" \
  --header "x-application-key: <bot token>" \
  --header "x-application-id: <chat ID>"
```

## Tools

| Tool | What it does |
|---|---|
| `send_message` | Text, `format` plain / html / markdown, `silent`, `reply_to_message_id`, `link_preview`. Long text is split at line breaks. Markup Telegram can't parse is sent as plain text, and the reply says so |
| `send_photo` | A photo from an https URL (Telegram fetches it, up to 5 MB), a data: URL or base64 (up to 15 MB), with an optional caption |
| `send_document` | Any file, kept as is: https URL (up to 20 MB), data: URL or base64 (up to 15 MB), with a file name and caption |
| `get_replies` | New messages in the chat (text, captions, and the type of other messages; file contents are not fetched). `wait_seconds` (up to 50) waits for a reply; `mark_read=false` peeks |
| `connection_status` | The bot, the chat, and whether `get_replies` can work; without a chat ID, the chats that recently wrote to the bot |

To ask and wait: `send_message`, then `get_replies` with `wait_seconds: 50`, repeated as needed.

## Run it yourself

```sh
cp .env.example .env    # set PUBLIC_HOST and MCP_TOKENS (openssl rand -hex 32)
docker compose -f compose.images.yaml up -d
curl -fsS http://127.0.0.1:3440/health
```

Put it behind an HTTPS reverse proxy. `MCP_TOKENS` takes several comma-separated tokens, so each
person or device can have its own and you can revoke one by removing it.

| Variable | Default | |
|---|---|---|
| `PUBLIC_HOST` | (required) | The public host name, for the DNS-rebinding check |
| `MCP_TOKENS` | (required) | Comma-separated tokens; `/mcp` answers 401 without one, 503 if none is set |
| `RATE_PER_MINUTE` / `RATE_PER_DAY` | 30 / 1000 | Sends per MCP token (each message part or file counts) |
| `MAX_UPLOAD_MB` | 15 | Largest data: URL or base64 file the server forwards |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | For tests or a local Bot API server |

The CI publishes `ghcr.io/gothenburgoffice/telegram-mcp:sha-<commit>` and `:latest` after the
tests and an image health check pass on `main`.

## Develop

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q tests     # offline: a stub Bot API, no real Telegram calls
```

## License

MIT
