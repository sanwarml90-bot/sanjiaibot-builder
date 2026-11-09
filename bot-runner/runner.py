"""bot-runner — multi-bot dispatcher for Sanji Bot Factory.

One Northflank service runs MANY user Telegram bots (Northflank's free
Sandbox allows only 2 services, so one service per bot is impossible).

How it works (GitHub-file registry — NO Northflank API needed):
- The builder bot writes the list of bots into
  bot-runner/bots.json inside the sanjiaibot-builder GitHub repo
  (https://raw.githubusercontent.com/<owner>/sanjiaibot-builder/main/
  bot-runner/bots.json). This runner polls that file every 60s.
- Each entry is {"username": ..., "repo": "owner/repo",
  "token": "<Fernet-encrypted bot token>"}. The repo is PUBLIC, so tokens
  are Fernet-encrypted (FERNET_KEY, same key as the builder's) — a
  "plain:" token is refused outright, never used.
- For each bot it downloads the repo as a tarball from the GitHub API
  (GITHUB_PAT, kept in memory only, never logged), extracts it to
  bots/<username>/, installs its requirements.txt (best effort) and spawns
  `python bot.py` as a subprocess with BOT_TOKEN / API_ID / API_HASH.
- It supervises the subprocesses: a crashed bot is restarted with
  backoff. A bot that fails to start 5 times in a row (e.g. a bad token)
  is parked with a clear log line — the other bots keep running.
- When a bot is REMOVED from bots.json, its subprocess is stopped. When a
  new bot appears, it is started without restarting the others. If a bot's
  repo changes, it is restarted with the new repo. A code fix pushed to a
  bot repo is picked up the next time that bot's process (re)starts —
  the runner re-downloads the repo on every (re)start.
- Bots must be stateless (no local files they need to keep).

Log markers:
  [bot-runner] @<username> started (pid <n>)
  [bot-runner] @<username> FAILED: <reason>
  [bot-runner] @<username> exited (code <n>), restarting in <s>s

Tokens (plaintext or encrypted) are never written to disk or logs.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot-runner")

# --- registry (GitHub file the runner polls) ---------------------------
REGISTRY_OWNER = os.environ.get("GITHUB_OWNER", "sanwarml90-bot")
REGISTRY_REPO = os.environ.get("REGISTRY_REPO", "sanjiaibot-builder")
REGISTRY_PATH = os.environ.get("REGISTRY_PATH", "bot-runner/bots.json")
REGISTRY_BRANCH = os.environ.get("REGISTRY_BRANCH", "main")
REGISTRY_URL = (
    f"https://raw.githubusercontent.com/{REGISTRY_OWNER}/{REGISTRY_REPO}"
    f"/{REGISTRY_BRANCH}/{REGISTRY_PATH}"
)
API_ID = os.environ.get("API_ID", "")
API_HASH = os.environ.get("API_HASH", "")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
BOTS_DIR = os.environ.get("BOTS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60") or 60)
MAX_FAST_FAILURES = 5          # give up on a bot after this many quick crashes
FAST_FAIL_WINDOW_S = 10        # a start that dies within this is a "fast failure"
MAX_BACKOFF_S = 300

# A bot token looks like 123456789:AAE... — never log anything matching it.
_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def _scrub(text: str) -> str:
    return _TOKEN_RE.sub("[TOKEN]", text)


# --- token decryption (same Fernet key as the builder) -----------------
try:
    from cryptography.fernet import Fernet
    _fernet = Fernet(os.environ["FERNET_KEY"].encode()) if os.environ.get("FERNET_KEY") else None
except Exception:  # noqa: BLE001
    _fernet = None


def _decrypt_token(value: str) -> str | None:
    """Decrypt a registry token. Returns the plaintext token or None.

    Only "fernet:" values are accepted. A "plain:" value (or anything else)
    is refused: the registry lives in a PUBLIC repo, so an unencrypted
    token there would be a leak — never run a bot with one.
    """
    if not value.startswith("fernet:"):
        return None
    if _fernet is None:
        return None
    try:
        return _fernet.decrypt(value[len("fernet:"):].encode()).decode()
    except Exception:  # noqa: BLE001
        return None


class Bot:
    def __init__(self, username: str, repo: str, token: str):
        self.username = username
        self.repo = repo
        self.token = token
        self.dir = os.path.join(BOTS_DIR, _safe_name(username))
        self.proc: subprocess.Popen | None = None
        self.log_path = os.path.join(self.dir, "bot.log")
        self.log_handle = None
        self.fast_failures = 0
        self.gave_up = False
        self.last_start = 0.0
        self.next_retry_at = 0.0


def _safe_name(username: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in username) or "bot"


def _fetch_registry() -> list[dict]:
    """Fetch + parse bots.json from the raw GitHub URL. [] on any failure."""
    try:
        req = urllib.request.Request(REGISTRY_URL, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.info("[bot-runner] registry not found yet (404) — no bots")
            return []
        log.warning("[bot-runner] registry fetch failed (HTTP %s)", exc.code)
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("[bot-runner] registry fetch failed: %s", _scrub(str(exc)))
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[bot-runner] registry is not valid JSON — ignoring this poll")
        return []
    if not isinstance(entries, list):
        log.warning("[bot-runner] registry is not a JSON list — ignoring this poll")
        return []
    return entries


def _entries_to_bots(entries: list[dict]) -> dict[str, Bot]:
    """Build {username: Bot} from registry entries, decrypting tokens.

    Entries with a missing field or an undecryptable token are skipped with
    a clear log line — they never stop the other bots.
    """
    bots: dict[str, Bot] = {}
    for entry in entries:
        username = str(entry.get("username") or "").strip()
        repo = str(entry.get("repo") or "").strip()
        enc = str(entry.get("token") or "").strip()
        if not username or not repo or not enc:
            log.warning("[bot-runner] skipping a registry entry with a missing username/repo/token")
            continue
        token = _decrypt_token(enc)
        if token is None:
            log.error(
                "[bot-runner] @%s FAILED: could not decrypt its token "
                "(FERNET_KEY wrong/missing, or the token is not Fernet-encrypted) — skipping",
                username,
            )
            continue
        bots[username] = Bot(username, repo, token)
    return bots


def _fetch_repo_tarball(repo: str) -> bytes:
    """Download a repo tarball from the GitHub API (PAT in memory only)."""
    if not GITHUB_PAT:
        raise RuntimeError("GITHUB_PAT is not configured")
    owner, name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{name}/tarball/main"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_PAT}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub tarball download for {repo} failed (HTTP {exc.code})") from exc


def _extract_bot_code(bot: Bot) -> None:
    """Fresh-extract the bot repo into the bot dir (code only, no git)."""
    data = _fetch_repo_tarball(bot.repo)
    if os.path.isdir(bot.dir):
        shutil.rmtree(bot.dir)
    os.makedirs(bot.dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise RuntimeError(f"empty tarball for {bot.repo}")
        top = members[0].name.split("/")[0]
        for member in members:
            if not member.name.startswith(top + "/"):
                continue
            member.name = member.name[len(top) + 1:]
            if not member.name:
                continue
            tar.extract(member, path=bot.dir)
    if not os.path.isfile(os.path.join(bot.dir, "bot.py")):
        raise RuntimeError(f"{bot.repo} has no bot.py at its root")


def _install_requirements(bot: Bot) -> None:
    """Best-effort pip install of the bot's requirements.txt (120s cap)."""
    req = os.path.join(bot.dir, "requirements.txt")
    if not os.path.isfile(req):
        return
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req],
            cwd=bot.dir, timeout=120,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if proc.returncode != 0:
            log.warning("[bot-runner] @%s pip install failed (code %s): %s",
                        bot.username, proc.returncode, _scrub(proc.stdout or "")[-500:])
    except subprocess.TimeoutExpired:
        log.warning("[bot-runner] @%s pip install timed out, continuing anyway", bot.username)
    except Exception as exc:  # noqa: BLE001
        log.warning("[bot-runner] @%s pip install error: %s", bot.username, _scrub(str(exc)))


def _start_bot(bot: Bot) -> bool:
    """Fetch code, install deps, spawn the bot. True if it started."""
    try:
        _extract_bot_code(bot)
    except Exception as exc:  # noqa: BLE001
        log.error("[bot-runner] @%s FAILED: could not fetch code: %s", bot.username, _scrub(str(exc)))
        return False
    _install_requirements(bot)
    env = dict(os.environ)
    env["BOT_TOKEN"] = bot.token
    env["API_ID"] = API_ID
    env["API_HASH"] = API_HASH
    try:
        # Log to a file, not a pipe: a chatty bot would otherwise block
        # once the pipe buffer fills (the runner never drains a live bot).
        bot.log_handle = open(bot.log_path, "w", encoding="utf-8", errors="replace")
        bot.proc = subprocess.Popen(
            [sys.executable, "bot.py"],
            cwd=bot.dir, env=env,
            stdout=bot.log_handle, stderr=subprocess.STDOUT,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[bot-runner] @%s FAILED: could not start: %s", bot.username, _scrub(str(exc)))
        return False
    bot.last_start = time.time()
    log.info("[bot-runner] @%s started (pid %s)", bot.username, bot.proc.pid)
    return True


def _stop_bot(bot: Bot) -> None:
    """Terminate a bot's subprocess and close its log handle."""
    if bot.proc is not None:
        try:
            bot.proc.terminate()
            bot.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                bot.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        bot.proc = None
    if bot.log_handle is not None:
        try:
            bot.log_handle.close()
        except Exception:  # noqa: BLE001
            pass
        bot.log_handle = None


def _drain_output(bot: Bot) -> None:
    """Log the tail of a dead bot's log file (scrubbed), then close it."""
    try:
        if bot.log_handle is not None:
            bot.log_handle.close()
            bot.log_handle = None
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(bot.log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:  # noqa: BLE001
        return
    for line in lines[-20:]:
        log.info("[bot-runner] @%s | %s", bot.username, _scrub(line))


def _reconcile(running: dict[str, Bot]) -> None:
    """Sync running bots with the registry: start new, stop removed.

    A bot whose repo changed is restarted with the new repo. Bots already
    running are left alone (no restart for unchanged entries).
    """
    desired = _entries_to_bots(_fetch_registry())
    # Stop bots removed from the registry.
    for username in list(running):
        if username not in desired:
            log.info("[bot-runner] @%s removed from registry — stopping", username)
            _stop_bot(running[username])
            del running[username]
    # Start new bots; restart ones whose repo changed.
    for username, bot in desired.items():
        if username in running:
            if running[username].repo != bot.repo:
                log.info("[bot-runner] @%s repo changed — restarting", username)
                _stop_bot(running[username])
                running[username] = bot
                _start_bot(bot)
            continue
        running[username] = bot
        _start_bot(bot)


def _supervise(stop: list[bool]) -> None:
    """Main loop: poll the registry, supervise bots, park repeat failures."""
    running: dict[str, Bot] = {}
    try:
        _reconcile(running)
        while not stop[0]:
            time.sleep(POLL_INTERVAL)
            if stop[0]:
                break
            _reconcile(running)
            now = time.time()
            for bot in list(running.values()):
                if bot.gave_up or bot.proc is None:
                    continue
                rc = bot.proc.poll()
                if rc is None:
                    continue
                # Bot process died.
                _drain_output(bot)
                uptime = now - bot.last_start
                if uptime < FAST_FAIL_WINDOW_S:
                    bot.fast_failures += 1
                else:
                    bot.fast_failures = 0
                if bot.fast_failures >= MAX_FAST_FAILURES:
                    bot.gave_up = True
                    bot.proc = None
                    log.error(
                        "[bot-runner] @%s FAILED: crashed %s times in a row right after "
                        "starting (last exit code %s) — parking it, check its token/code. "
                        "Other bots keep running.",
                        bot.username, bot.fast_failures, rc,
                    )
                    continue
                backoff = min(2 ** bot.fast_failures * 5, MAX_BACKOFF_S)
                bot.next_retry_at = now + backoff
                log.warning(
                    "[bot-runner] @%s exited (code %s), restarting in %ss",
                    bot.username, rc, backoff,
                )
                bot.proc = None
            # (Re)start bots whose backoff has elapsed.
            for bot in running.values():
                if bot.gave_up or bot.proc is not None:
                    continue
                if bot.next_retry_at and time.time() < bot.next_retry_at:
                    continue
                if bot.next_retry_at:
                    bot.next_retry_at = 0.0
                _start_bot(bot)
    finally:
        for bot in running.values():
            _stop_bot(bot)


def main() -> int:
    log.info("[bot-runner] starting — polling registry %s every %ss",
             REGISTRY_URL, POLL_INTERVAL)
    if _fernet is None:
        log.warning("[bot-runner] FERNET_KEY is not set — no registry token can "
                    "be decrypted, so no bot will start until it is set")
    stop: list[bool] = [False]

    def _on_signal(signum, _frame):
        log.info("[bot-runner] signal %s, shutting down bots…", signum)
        stop[0] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    _supervise(stop)
    log.info("[bot-runner] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
