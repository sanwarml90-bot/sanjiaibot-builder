# Sanji AI — builder agent (@Sanjiaibot, new version)

DM-only Telegram bot that builds Telegram bots and userbots for users.
Builder only — no group replies.

## Flows

- `/newbot` — user gives a bot idea + a BotFather token. The agent writes the
  bot code (Groq), creates a GitHub repo, deploys it on Railway, tests it and
  reports progress + logs. It retries failures and does not stop until the bot
  is live or a hard failure is reported.
- `/userbot` — user gives a phone number; the agent takes the OTP and (if set)
  the 2FA password, then returns the Pyrogram session string + API ID/hash.

## Security

- OTPs, 2FA passwords and session strings are used transiently, never stored
  or logged. User bot tokens are kept in memory only, never written to disk.
- User tokens are encrypted in memory with FERNET_KEY when set.
- Tokens go to Railway variables only — never into a repo file.
- The builder deletes the user's token/OTP/password messages from chat after
  reading them.

## Env vars (Railway service variables)

- `BOT_TOKEN` — @Sanjiaibot token (from BotFather)
- `API_ID`, `API_HASH` — shared Telegram API credentials
- `OWNER_ID` — Sanji (7966027018)
- `GITHUB_OWNER` — sanwarml90-bot
- `GITHUB_PAT` — classic PAT with `repo` scope (creates repos, pushes files)
- `RAILWAY_TOKEN` — Railway account token (creates services, sets vars, deploys)
- `RAILWAY_PROJECT_ID` — the Railway project bots deploy into
- `GROQ_API_KEY` — Groq free API key (code generation)
- `FERNET_KEY` — encrypts user tokens held in memory

## Run

```
pip install -r requirements.txt
python bot.py
```

Start command on Railway: `python bot.py`
