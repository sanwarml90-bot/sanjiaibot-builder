# bot-runner — multi-bot dispatcher for Sanji Bot Factory

One Northflank combined service runs **many** user Telegram bots.
Northflank's free Sandbox allows only 2 services, so one service per bot
is impossible — this dispatcher is the workaround.

## How it works (GitHub-file registry — no Northflank API)

- The builder bot writes the list of bots into
  `bot-runner/bots.json` inside the sanjiaibot-builder GitHub repo.
  This runner polls that file every 60s at
  `https://raw.githubusercontent.com/<owner>/sanjiaibot-builder/main/bot-runner/bots.json`.
- Each entry is `{"username", "repo": "owner/repo", "token"}` where the
  token is **Fernet-encrypted** (same `FERNET_KEY` as the builder). The
  registry file lives in a PUBLIC repo, so a plaintext token must never
  be written there — this runner refuses any entry whose token is not
  `fernet:`-encrypted, and logs a clear error for it instead of running it.
- For each bot it downloads the repo as a tarball from the GitHub API
  (auth: `GITHUB_PAT`, in memory only), extracts it to
  `bots/<username>/`, pip-installs its `requirements.txt` (best effort)
  and spawns `python bot.py` with `BOT_TOKEN` / `API_ID` / `API_HASH`.
- It supervises the bots: a crashed bot is restarted with backoff. A bot
  that fails to start 5 times in a row (e.g. a bad token) is parked with a
  clear log line — the other bots keep running.
- When a bot is removed from `bots.json`, its subprocess is stopped.
  When a new bot appears, it is started without restarting the others.
  If a bot's repo changes, it is restarted with the new repo.
- A code fix pushed to a bot repo is picked up the next time that bot's
  process (re)starts — the runner re-downloads the repo on every
  (re)start. Bots must be stateless (no local files they need to keep).

## Setup (one-time, Northflank dashboard — no API token needed)

Create one combined service in the Northflank project from this repo
(the `bot-runner` GitHub repo) and set its runtime variables (below).
No Northflank API token is required for the builder to add bots — the
builder only edits `bots.json` on GitHub, and this runner polls it.

## Env vars (Northflank runtime variables on this service)

- `FERNET_KEY` — **required**; same key as the builder's, decrypts tokens
- `API_ID`, `API_HASH` — the builder's shared Telegram API credentials
- `GITHUB_PAT` — classic PAT with `repo` scope (downloads bot repos)
- `GITHUB_OWNER` — owner of the registry repo (default `sanwarml90-bot`)
- `REGISTRY_REPO` — registry repo name (default `sanjiaibot-builder`)
- `REGISTRY_PATH` — registry file path (default `bot-runner/bots.json`)
- `REGISTRY_BRANCH` — registry branch (default `main`)
- `BOTS_DIR` — where bot code lives (default: `bots/` next to runner.py)
- `POLL_INTERVAL` — registry poll interval in seconds (default 60)

## Log markers

- `[bot-runner] @<username> started (pid <n>)`
- `[bot-runner] @<username> FAILED: <reason>`
- `[bot-runner] @<username> exited (code <n>), restarting in <s>s`

Tokens (plaintext or encrypted) are never written to disk or logs.
