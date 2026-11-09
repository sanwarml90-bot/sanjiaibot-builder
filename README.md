# Sanji AI — builder agent (@Sanjiaibot, new version)

DM-only Telegram bot that builds Telegram bots and userbots for users.
Builder only — no group replies.

## Flows

- `/newbot` — user gives a bot idea + a BotFather token. The agent writes the
  bot code (AI), creates a GitHub repo, registers the bot in the
  bot-runner registry file (which the Northflank dispatcher polls), tests
  the token and reports progress + logs. It retries code failures and
  does not stop until the bot is live or a hard failure is reported.
- `/userbot` — user gives a phone number; the agent takes the OTP and (if set)
  the 2FA password, then returns the Pyrogram session string + API ID/hash.

## Deploy model — Northflank dispatcher, GitHub-file registry (no API)

Railway's Limited Trial blocks API service creation (HTTP 403), and
Northflank's free Sandbox allows only **2 services**, so the builder does
NOT create one service per user bot. Instead, one Northflank combined
service — **bot-runner** (code in `bot-runner/`, its own GitHub repo,
created once via the Northflank dashboard) — runs many bots. The builder
registers each new bot in `bot-runner/bots.json` inside this repo; the
runner polls that file every 60s and starts/stops bots to match it.

No Northflank API token is used: Northflank's "Create API token" button
is broken (ongoing Northflank security incident since 2026-10-30), so the
builder never calls the Northflank API. The runner service is created once
via the dashboard; after that, adding a bot is just a commit to
`bot-runner/bots.json`.

**Tokens are Fernet-encrypted** (`FERNET_KEY`, same key on builder and
runner) before being written to `bots.json` — the file lives in this
PUBLIC repo, so a plaintext token must never land in it. The runner
refuses any entry whose token is not `fernet:`-encrypted. The runner
downloads each bot's repo, spawns `python bot.py` per bot, and supervises
them (restart on crash; a bot that fails 5 times in a row is parked, the
rest keep running). A code fix pushed to a bot repo is picked up the next
time that bot's process (re)starts.

## Security

- OTPs, 2FA passwords and session strings are used transiently, never stored
  or logged. User bot tokens are encrypted in memory with FERNET_KEY when set.
- User bot tokens are Fernet-encrypted before being written to
  `bot-runner/bots.json` (a PUBLIC repo file) — never plaintext. The
  builder refuses to write the registry without FERNET_KEY, and the runner
  refuses any registry entry whose token is not `fernet:`-encrypted.
- The builder deletes the user's token/OTP/password messages from chat after
  reading them.

## Env vars (Railway service variables — the builder runs on Railway)

- `BOT_TOKEN` — @Sanjiaibot token (from BotFather)
- `API_ID`, `API_HASH` — shared Telegram API credentials
- `OWNER_ID` — Sanji (7966027018)
- `GITHUB_OWNER` — sanwarml90-bot
- `GITHUB_PAT` — classic PAT with `repo` scope (creates repos, pushes files)
- `GROQ_API_KEY` — Groq free API key (code generation)
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` — OpenRouter code generation
- `FERNET_KEY` — **required**: encrypts user tokens before they are written
  to the public registry file `bot-runner/bots.json` (and held in memory)
- `NORTHFLANK_API_TOKEN` / `NORTHFLANK_PROJECT_ID` / `NORTHFLANK_TEAM_ID` /
  `NORTHFLANK_RUNNER_SERVICE` / `NORTHFLANK_DEPLOYMENT_PLAN` — no longer
  used (the builder never calls the Northflank API; the runner service is
  created once via the dashboard and polls the registry file)
- `RAILWAY_TOKEN` / `RAILWAY_PROJECT_ID` — no longer used for user-bot deploys

## Run

```
pip install -r requirements.txt
python bot.py
```

Start command on Railway: `python bot.py`
