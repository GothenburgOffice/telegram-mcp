# telegram-mcp

A remote MCP server that sends and reads Telegram bot messages for one chat per connection. Read
[README.md](README.md) first. It is self-hosted: the README documents installing the published
image, and no hosted instance is advertised here. Operations for our own server live in a
separate private repository, not here.

- Store nothing: no bot tokens, chat IDs, messages or files on disk or in logs. The bot token is in
  every Bot API URL, so never put a URL, a urllib exception text or the token into an error.
- Tools never take a chat ID argument: the chat comes only from the `x-application-id` header.
- Tests are offline (`python -m pytest -q tests`, a stub Bot API); keep them that way.

## Branches and pull requests

These rules come from `hkarn/repo-baseline`. Keep them when editing this file; change a rule only
when the owner asks for this repository to differ.

- `main` is protected: no direct pushes, force-pushes or deletion. Every change is a pull request from a branch made from the latest `main`.
- Assign every pull request you open to `hkarn`, so it shows up in the owner's GitHub views:
  `gh pr create --assignee hkarn`, or `gh pr edit <N> --add-assignee hkarn` for a pull request
  opened another way (a GitHub MCP tool, or a draft the environment opened).
- Open pull requests as drafts. The owner marks a pull request ready for review.
- Never merge, approve or enable auto-merge on a pull request. The owner merges; that is the approval.
- Claude Code signs everything it posts on GitHub (pull request descriptions, issues, comments and review
  replies) with a last line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
  This line is Claude Code's alone: Copilot and other reviewers or agents reading this file don't add it.
- Review comments, from Copilot or a person: check each claim against the code first. Fix what is
  real in a new commit and reply on the thread with what you found and what changed (or why nothing
  did). Never resolve a conversation; the owner does that.
- Line endings are LF everywhere (`.gitattributes`); don't commit CRLF files other than `.bat`/`.cmd`.
