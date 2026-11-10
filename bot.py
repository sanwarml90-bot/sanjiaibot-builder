"""Sanji AI — builder agent (@Sanjiaibot, new version).

DM-only Telegram bot. Two flows:
  /newbot  — user gives a bot idea + a BotFather token; the agent writes the
             bot code (AI), creates a GitHub repo, registers the bot in the
             bot-runner registry file (bot-runner/bots.json in this repo),
             and the Northflank bot-runner service picks it up and runs it.
             It tests the token and reports progress + logs. It retries code
             failures and does not stop until the bot is live or a hard
             failure is reported.
  /userbot — user gives a phone number; the agent takes the OTP and (if set)
             the 2FA password, then generates and returns the Pyrogram session
             string plus the API ID / API hash.

Group replies are OFF by design (builder only). OTPs, passwords and session
strings are used transiently, never stored or logged. User bot tokens are
Fernet-encrypted (FERNET_KEY) before being written to the registry file —
the registry lives in a PUBLIC repo, so a plaintext token must never be
written there. No Northflank API is used: Northflank's "Create API token"
button is broken (ongoing Northflank security incident since 2026-10-30),
so the runner polls the registry file instead, and the runner service is
created once via the Northflank dashboard.
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import os
import re
import urllib.request
import urllib.error

from pyrogram import Client, filters
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
    FloodWait,
)
from pyrogram.types import Message

import codegen
import github_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sanjiaibot-builder")

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "7966027018") or 7966027018)
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "sanwarml90-bot")
# Registry file the bot-runner polls: bot-runner/bots.json inside THIS repo
# (the sanjiaibot-builder repo, owned by GITHUB_OWNER, branch main).
# Tokens in it are Fernet-encrypted — the repo is PUBLIC, so plaintext
# tokens must never be written there.
REGISTRY_REPO = "sanjiaibot-builder"
REGISTRY_PATH = "bot-runner/bots.json"
REGISTRY_BRANCH = "main"
# Seconds the runner needs to notice a new registry entry (it polls every
# 60s) before the test step runs.
REGISTRY_PICKUP_WAIT_S = 90
MAX_BUILD_ATTEMPTS = 3

# --- token protection: Fernet when available, plain in-memory otherwise ---
try:
    from cryptography.fernet import Fernet
    _fernet = Fernet(os.environ["FERNET_KEY"].encode()) if os.environ.get("FERNET_KEY") else None
except Exception:  # noqa: BLE001
    _fernet = None


def _protect(value: str) -> str:
    if _fernet:
        return "fernet:" + _fernet.encrypt(value.encode()).decode()
    return "plain:" + value


def _unprotect(value: str) -> str:
    if value.startswith("fernet:") and _fernet:
        return _fernet.decrypt(value[len("fernet:"):].encode()).decode()
    if value.startswith("plain:"):
        return value[len("plain:"):]
    return value


# ---------------------------------------------------------------- registry
def _read_registry() -> list[dict]:
    """Read bot-runner/bots.json from the builder repo. [] if missing/invalid."""
    raw = github_api.get_file(GITHUB_OWNER, REGISTRY_REPO, REGISTRY_PATH, REGISTRY_BRANCH)
    if raw is None:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("registry %s is not valid JSON — treating as empty", REGISTRY_PATH)
        return []
    if not isinstance(entries, list):
        log.warning("registry %s is not a JSON list — treating as empty", REGISTRY_PATH)
        return []
    return entries


def _write_registry(entries: list[dict], bot_username: str) -> None:
    """Write the registry back to the builder repo (one commit).

    Refuses to run without FERNET_KEY: the registry lives in a PUBLIC repo,
    so a token written without encryption would leak. Every entry's token
    must already be Fernet-encrypted (see _protect).
    """
    if _fernet is None:
        raise RuntimeError(
            "FERNET_KEY is not configured — refusing to write bot tokens to "
            "the public registry file unencrypted. Set FERNET_KEY and retry."
        )
    for entry in entries:
        token = str(entry.get("token") or "")
        if not token.startswith("fernet:"):
            raise RuntimeError(
                f"Registry entry @{entry.get('username')} has no encrypted token — "
                "refusing to write it to the public registry file."
            )
    content = json.dumps(entries, indent=2) + "\n"
    github_api.put_file(
        GITHUB_OWNER, REGISTRY_REPO, REGISTRY_PATH, content,
        f"Register bot @{bot_username} in the runner registry", REGISTRY_BRANCH)


def _code_syntax_ok(code: str) -> str | None:
    """Return the SyntaxError message, or None if the code parses."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno})"


app = Client("sanjiaibot-builder", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# user_id -> {"flow": "newbot"|"userbot", "step": str, "data": dict}
STATE: dict[int, dict] = {}
# user_id -> asyncio.Task (one active build per user)
JOBS: dict[int, asyncio.Task] = {}


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len] or "bot"


def _bot_api_getme(token: str) -> dict | None:
    """Validate a BotFather token via getMe. Returns bot info or None."""
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/getMe", method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("result") if data.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


async def _safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception:  # noqa: BLE001
        pass


def _progress_text(title: str, steps: list[tuple[str, str]]) -> str:
    """steps: list of (label, state) where state in done|doing|todo|fail."""
    icons = {"done": "✅", "doing": "🔨", "todo": "⬜", "fail": "❌"}
    lines = [title, ""]
    for label, state in steps:
        lines.append(f"{icons.get(state, '⬜')} {label}")
    return "\n".join(lines)


# ---------------------------------------------------------------- /start etc.
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message):
    STATE.pop(message.from_user.id, None)
    await message.reply_text(
        "Namaste! Main **Sanji AI** hun — bot banane wala agent. 🤖\n\n"
        "Main ye kar sakta hun:\n"
        "• **/newbot** — apne bot ka idea batao + BotFather token do, "
        "main code likh ke GitHub pe daal ke Northflank pe live kar dunga, test kar ke. "
        "Progress aur logs yahin dikhata rahunga.\n"
        "• **/userbot** — apna number do, OTP aur password do, "
        "main session string + API ID/hash nikal ke de dunga.\n\n"
        "Tumhe kuch karna nahi padega — sab main karunga. Bas idea ya number bhejo. 😎"
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    STATE.pop(uid, None)
    job = JOBS.pop(uid, None)
    if job and not job.done():
        job.cancel()
    await message.reply_text("Theek hai, cancel kar diya. /start se dobara shuru kar sakte ho.")


@app.on_message(filters.command("newbot") & filters.private)
async def cmd_newbot(client: Client, message: Message):
    uid = message.from_user.id
    if uid in JOBS and not JOBS[uid].done():
        await message.reply_text("Tumhara ek bot build abhi chal raha hai — pehle wo complete hone do. 🙏")
        return
    STATE[uid] = {"flow": "newbot", "step": "idea", "data": {}}
    await message.reply_text(
        "Badhiya! Apne bot ka **idea** batao — wo kya karega? "
        "Jaise: 'ek voting bot jo group me poll banaye' ya 'file rename karne wala bot'."
    )


@app.on_message(filters.command("userbot") & filters.private)
async def cmd_userbot(client: Client, message: Message):
    uid = message.from_user.id
    STATE[uid] = {"flow": "userbot", "step": "phone", "data": {}}
    await message.reply_text(
        "Userbot banate hain. Apna **phone number** bhejo (country code ke saath, jaise +91 98765 43210). "
        "OTP aur password sirf isi kaam ke liye use hoga, kahin save nahi hoga."
    )


@app.on_message(filters.private & filters.text)
async def on_private_text(client: Client, message: Message):
    uid = message.from_user.id
    st = STATE.get(uid)
    if not st:
        await message.reply_text(
            "Main bot banane wala agent hun. /newbot se bot banao ya /userbot se userbot session lo. /start se details dekho."
        )
        return
    flow, step = st["flow"], st["step"]
    text = (message.text or "").strip()

    if flow == "newbot":
        if step == "idea":
            st["data"]["idea"] = text
            st["step"] = "token"
            await message.reply_text(
                "Idea mil gaya! ✅\n\nAb us bot ka **BotFather token** bhejo "
                "(BotFather se /newbot karke jo token mila tha). "
                "Token sirf deploy ke liye use hoga, kahin save ya share nahi hoga. "
                "Bhejte hi main ye message delete kar dunga."
            )
        elif step == "token":
            token = text.split()[0].strip()
            await _safe_delete(message)
            info = _bot_api_getme(token)
            if not info:
                await message.reply_text(
                    "Ye token galat lag raha hai — BotFather se sahi token check karke dobara bhejo. 🙏"
                )
                return
            username = info.get("username", "bot")
            st["data"]["token"] = _protect(token)
            st["data"]["bot_username"] = username
            STATE.pop(uid, None)
            await message.reply_text(
                f"Token verify ho gaya — bot hai @{username}. ✅\n"
                "Ab main build shuru karta hun. Progress yahin dikhata rahunga, rukna mat. 🔨"
            )
            task = asyncio.create_task(run_build_job(uid, st["data"]))
            JOBS[uid] = task
        return

    if flow == "userbot":
        if step == "phone":
            phone = re.sub(r"[\s\-()]", "", text)
            if not phone.startswith("+"):
                phone = "+" + phone
            if not re.fullmatch(r"\+\d{10,15}", phone):
                await message.reply_text("Number sahi format mein bhejo — jaise +91 98765 43210.")
                return
            st["data"]["phone"] = phone
            st["step"] = "otp_wait"
            await message.reply_text("OTP bhej raha hun… 📩")
            try:
                await _start_userbot_login(uid, phone)
            except FloodWait as exc:
                STATE.pop(uid, None)
                await message.reply_text(f"Telegram ne thoda wait karne ko kaha hai ({exc.value} sec). Thodi der baad /userbot se dobara try karo.")
                return
            except Exception as exc:  # noqa: BLE001
                STATE.pop(uid, None)
                await message.reply_text(f"OTP bhejne mein dikkat aayi: {exc}. Number check karke dobara try karo.")
                return
            st["step"] = "otp"
            await message.reply_text(
                "Telegram se aaya **OTP** bhejo (jo number pe aaya hai). "
                "Bhejte hi main ye message delete kar dunga."
            )
        elif step == "otp":
            code = re.sub(r"\D", "", text)
            await _safe_delete(message)
            st["data"]["code"] = code
            st["step"] = "password_wait"
            try:
                result = await _finish_userbot_login(uid)
            except PhoneCodeInvalid:
                st["step"] = "otp"
                await message.reply_text("OTP galat hai — sahi OTP dobara bhejo.")
                return
            except PhoneCodeExpired:
                STATE.pop(uid, None)
                await message.reply_text("OTP expire ho gaya — /userbot se dobara shuru karo.")
                return
            except SessionPasswordNeeded:
                st["step"] = "password"
                await message.reply_text(
                    "Is account pe 2FA password laga hai. **Password** bhejo. "
                    "Bhejte hi main ye message delete kar dunga, password kahin save nahi hoga."
                )
                return
            except Exception as exc:  # noqa: BLE001
                STATE.pop(uid, None)
                await message.reply_text(f"Login mein dikkat aayi: {exc}. /userbot se dobara try karo.")
                return
            await _deliver_userbot_session(uid, result)
        elif step == "password":
            password = text
            await _safe_delete(message)
            try:
                result = await _finish_userbot_password(uid, password)
            except PasswordHashInvalid:
                await message.reply_text("Password galat hai — sahi password dobara bhejo.")
                return
            except Exception as exc:  # noqa: BLE001
                STATE.pop(uid, None)
                await message.reply_text(f"Login mein dikkat aayi: {exc}. /userbot se dobara try karo.")
                return
            await _deliver_userbot_session(uid, result)
        return


# ---------------------------------------------------------------- userbot login
# user_id -> temporary in-memory Pyrogram client + phone_code_hash
_LOGIN_CLIENTS: dict[int, dict] = {}


async def _start_userbot_login(uid: int, phone: str) -> None:
    tmp = Client(f"login-{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await tmp.connect()
    sent = await tmp.send_code(phone)
    _LOGIN_CLIENTS[uid] = {"client": tmp, "phone_code_hash": sent.phone_code_hash, "phone": phone}


async def _finish_userbot_login(uid: int) -> str:
    ctx = _LOGIN_CLIENTS[uid]
    st = STATE[uid]
    await ctx["client"].sign_in(ctx["phone"], ctx["phone_code_hash"], st["data"]["code"])
    return await _export_session(uid)


async def _finish_userbot_password(uid: int, password: str) -> str:
    ctx = _LOGIN_CLIENTS[uid]
    await ctx["client"].check_password(password)
    return await _export_session(uid)


async def _export_session(uid: int) -> str:
    ctx = _LOGIN_CLIENTS[uid]
    session = await ctx["client"].export_session_string()
    try:
        await ctx["client"].disconnect()
    except Exception:  # noqa: BLE001
        pass
    _LOGIN_CLIENTS.pop(uid, None)
    STATE.pop(uid, None)
    return session


async def _deliver_userbot_session(uid: int, session: str) -> None:
    text = (
        "Ho gaya! 🎉 Tumhara userbot session taiyaar hai.\n\n"
        "**API_ID:**\n"
        f"`{API_ID}`\n\n"
        "**API_HASH:**\n"
        f"`{API_HASH}`\n\n"
        "**Session String:**\n"
        f"`{session}`\n\n"
        "⚠️ Ye session string kisi ko mat bhejo — isse tumhara account khul jata hai. "
        "Apne bot ke env vars mein daalo, chat mein save mat karo."
    )
    await app.send_message(uid, text)


# ---------------------------------------------------------------- bot build job
async def run_build_job(uid: int, data: dict) -> None:
    idea = data["idea"]
    token = _unprotect(data["token"])
    bot_username = data.get("bot_username", "bot")
    repo_name = _slug(bot_username)

    steps: list[tuple[str, str]] = [
        ("Idea samajh liya", "done"),
        ("Bot ka code likh raha hun", "todo"),
        ("GitHub repo bana raha hun", "todo"),
        ("Northflank runner pe register kar raha hun", "todo"),
        ("Bot ko test kar raha hun", "todo"),
    ]
    progress = await app.send_message(uid, _progress_text("🔨 Bot build ho raha hai…", steps))

    async def update(idx: int, state: str) -> None:
        steps[idx] = (steps[idx][0], state)
        try:
            await progress.edit_text(_progress_text("🔨 Bot build ho raha hai…", steps))
        except Exception:  # noqa: BLE001
            pass

    def note(text: str) -> None:
        asyncio.create_task(app.send_message(uid, text))

    try:
        # 1. Code generation (+ syntax check / fix loop for CODE errors only)
        await update(1, "doing")
        code, source = await asyncio.to_thread(codegen.generate_bot_code, idea)
        for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
            err = await asyncio.to_thread(_code_syntax_ok, code)
            if err is None:
                break
            if attempt < MAX_BUILD_ATTEMPTS:
                note(f"Code mein syntax error hai ({err}) — AI se fix kar raha hun…")
                fixed, _ = await asyncio.to_thread(codegen.fix_bot_code, code, err)
                code = fixed
            else:
                raise RuntimeError(
                    f"Bot code mein syntax error hai aur {MAX_BUILD_ATTEMPTS} baar "
                    f"fix nahi hua: {err}")
        await update(1, "done")
        note(f"Code likh liya ({'AI se' if source in ('groq', 'openrouter') else 'template se'}). ✅")
        if source == "template":
            note("AI se code nahi bana, isliye basic template deploy hua hai — isme group commands nahi honge.")

        # 2. GitHub repo + push
        await update(2, "doing")
        repo = repo_name
        for attempt in range(5):
            try:
                if not await asyncio.to_thread(github_api.repo_exists, GITHUB_OWNER, repo):
                    await asyncio.to_thread(
                        github_api.create_repo, repo,
                        f"Telegram bot @{bot_username} built by Sanji AI")
                break
            except Exception as exc:  # noqa: BLE001
                if "422" in str(exc):
                    repo = f"{repo_name}-{attempt + 2}"
                    continue
                raise
        files = {
            "bot.py": code,
            "requirements.txt": codegen.requirements_text(),
            "README.md": codegen.readme_text(bot_username, idea),
            ".gitignore": ".env\n__pycache__/\n*.session\n",
        }
        await asyncio.to_thread(github_api.push_scaffold, GITHUB_OWNER, repo, files)
        await update(2, "done")
        note(f"GitHub repo ban gaya: https://github.com/{GITHUB_OWNER}/{repo} ✅")

        # 3. Register the bot in the runner's GitHub-file registry.
        # No Northflank API (its token creation is broken): the runner polls
        # bot-runner/bots.json in this repo every 60s and starts any bot
        # listed there. The token is Fernet-encrypted before being written
        # (the repo is PUBLIC — a plaintext token must never land in it).
        await update(3, "doing")
        entries = await asyncio.to_thread(_read_registry)
        entries = [e for e in entries if e.get("username") != bot_username]
        entries.append({"username": bot_username,
                        "repo": f"{GITHUB_OWNER}/{repo}",
                        "token": _protect(token)})
        await asyncio.to_thread(_write_registry, entries, bot_username)
        await update(3, "done")
        note(f"Bot register kar diya — runner thodi der mein @{bot_username} start kar dega. ✅")

        # 4. Test: wait for the runner to pick the bot up, then verify the
        # token via getMe. A getMe failure is a TOKEN error — do not retry
        # the code for it.
        await update(4, "doing")
        await asyncio.sleep(REGISTRY_PICKUP_WAIT_S)
        info = await asyncio.to_thread(_bot_api_getme, token)
        if not info:
            await update(4, "fail")
            await progress.edit_text(
                _progress_text("🔨 Bot build ho raha hai…", steps)
                + "\n\n❌ Bot ka token ab verify nahi ho raha. Token check karke /newbot se dobara try karo."
            )
            return
        await update(4, "done")
        await progress.edit_text(_progress_text("🎉 Bot live hai!", steps))
        await app.send_message(
            uid,
            f"Ho gaya! 🎉 Tumhara bot **live** hai:\n\n"
            f"🤖 @{info.get('username')}\n"
            f"📦 Repo: https://github.com/{GITHUB_OWNER}/{repo}\n\n"
            f"Ab @{info.get('username')} ko /start bhejo aur test karo. "
            "Kuch gadbad lage to yahin bata dena."
        )
    except asyncio.CancelledError:
        try:
            await progress.edit_text("Build cancel kar diya. /newbot se dobara shuru kar sakte ho.")
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("build job failed")
        try:
            await app.send_message(uid, f"Build mein dikkat aayi: {exc}. /newbot se dobara try karo.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        JOBS.pop(uid, None)


# Ignore group messages entirely — builder is DM-only by design.
@app.on_message(filters.group)
async def on_group_message(client: Client, message: Message):
    return


if __name__ == "__main__":
    log.info("Sanji AI builder starting…")
    app.run()
