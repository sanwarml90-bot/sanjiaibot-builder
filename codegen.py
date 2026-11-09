"""Code generation for the Sanji AI builder agent.

Uses Groq (free tier) to write a complete Pyrogram Telegram bot from a
user's idea. Falls back to a generic template bot when Groq is unavailable
or no key is configured. Never logs or stores any secret.
"""
from __future__ import annotations

import ast
import json
import os
import re
import urllib.request

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")

SYSTEM_PROMPT = """You are an expert Python developer writing Telegram bots with Pyrogram.
Write ONE complete, working bot.py. Rules:
- Use Pyrogram only (pyrogram + tgcrypto). Read BOT_TOKEN, API_ID, API_HASH from environment variables with os.environ. Never hardcode secrets.
- Use Pyrogram's built-in app.run() runner at the end. NEVER use asyncio.run(main()) + start() + idle() — that pattern never polls updates.
- Include /start and /help handlers in Hinglish (simple Hindi + English mix).
- Keep the code self-contained in a single file. No extra files, no placeholders, no TODOs.
- Reply ONLY with the Python code, no markdown fences, no explanation.
"""

USER_PROMPT_TEMPLATE = """Write a complete Pyrogram Telegram bot for this idea (user's words, Hinglish/English):

\"\"\"{idea}\"\"\"

The bot must implement the idea fully: all commands, handlers and logic the idea describes.
If the idea is vague, build a sensible, working version of it with /start and /help.
Remember: BOT_TOKEN, API_ID, API_HASH come from os.environ. End with app.run(). Reply with ONLY the Python code.
"""


def _groq_chat(prompt: str, model: str, api_key: str, timeout: int = 120) -> str:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 8000,
    }).encode("utf-8")
    req = urllib.request.Request(
        GROQ_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\s*\n", "", code)
        code = re.sub(r"\n```\s*$", "", code)
    return code.strip()


def _syntax_ok(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


FALLBACK_TEMPLATE = '''"""Generic Telegram bot built by Sanji AI."""
import os
from pyrogram import Client, filters

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

app = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply_text(
        "Namaste! Main aapka bot hun. /help bhejo, main kya kar sakta hun ye dekhne ke liye."
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message):
    await message.reply_text(
        "/start - bot shuru karo\\n/help - ye madad"
    )


@app.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def echo(client, message):
    await message.reply_text(f"Aapne kaha: {message.text}")


app.run()
'''


def generate_bot_code(idea: str) -> tuple[str, str]:
    """Generate bot.py source for an idea.

    Returns (code, source) where source is 'groq' or 'template'.
    Raises RuntimeError if Groq is configured but fails and no code results.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if api_key:
        last_err = None
        for model in (GROQ_MODEL, GROQ_FALLBACK_MODEL):
            try:
                raw = _groq_chat(USER_PROMPT_TEMPLATE.format(idea=idea), model, api_key)
                code = _strip_fences(raw)
                if code and _syntax_ok(code):
                    return code, "groq"
                last_err = "Groq returned code with a syntax error"
            except Exception as exc:  # noqa: BLE001 - report as failure, fall through
                last_err = str(exc)
        # Groq configured but unusable: fall back to template rather than fail.
        _ = last_err
    return FALLBACK_TEMPLATE, "template"


def fix_bot_code(code: str, error_log: str) -> tuple[str, str]:
    """Ask Groq to fix bot code given a deploy/runtime error log.

    Returns (fixed_code, source). Falls back to the original code on failure.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return code, "unchanged"
    prompt = (
        "Fix this Pyrogram Telegram bot code so it runs without errors.\n\n"
        f"ERROR LOG (last lines):\n{error_log[-4000:]}\n\n"
        f"CURRENT CODE:\n{code}\n\n"
        "Reply with ONLY the complete fixed Python code, no markdown fences, no explanation. "
        "Keep BOT_TOKEN, API_ID, API_HASH from os.environ. End with app.run()."
    )
    try:
        raw = _groq_chat(prompt, GROQ_MODEL, api_key)
        fixed = _strip_fences(raw)
        if fixed and _syntax_ok(fixed):
            return fixed, "groq-fix"
    except Exception:  # noqa: BLE001 - keep original code
        pass
    return code, "unchanged"


def requirements_text() -> str:
    return "pyrogram\ntgcrypto\npython-dotenv\n"


def readme_text(bot_name: str, idea: str) -> str:
    return (
        f"# {bot_name}\n\n"
        "Telegram bot built automatically by Sanji AI (@Sanjiaibot).\n\n"
        f"Idea: {idea}\n\n"
        "## Run\n\n"
        "Set BOT_TOKEN, API_ID and API_HASH as environment variables, then:\n\n"
        "```\n"
        "pip install -r requirements.txt\n"
        "python bot.py\n"
        "```\n"
    )
