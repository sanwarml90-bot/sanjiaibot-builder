"""Minimal GitHub REST client for the builder agent.

Creates repos and pushes files via the Contents API. Auth: classic PAT with
`repo` scope (env GITHUB_PAT). Never logs or stores secrets.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

API_BASE = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubError(Exception):
    pass


def _headers() -> dict:
    token = os.environ.get("GITHUB_PAT", "").strip()
    if not token:
        raise GitHubError("GITHUB_PAT is not configured")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise GitHubError(f"GitHub {method} {path} failed ({exc.code}): {body}") from exc


def repo_exists(owner: str, name: str) -> bool:
    try:
        _request("GET", f"/repos/{owner}/{name}")
        return True
    except GitHubError as exc:
        if "404" in str(exc):
            return False
        raise


def create_repo(name: str, description: str = "") -> dict:
    """Create a public repo under the authenticated user. Returns repo object."""
    return _request("POST", "/user/repos", {
        "name": name,
        "description": description or "Telegram bot built by Sanji AI",
        "private": False,
        "auto_init": True,
    })


def _file_sha(owner: str, repo: str, path: str, branch: str) -> str | None:
    try:
        info = _request("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
        return info.get("sha")
    except GitHubError as exc:
        if "404" in str(exc):
            return None
        raise


def put_file(owner: str, repo: str, path: str, content: str, message: str,
             branch: str = "main") -> dict:
    """Create or update one file (base64 contents API). One commit per file."""
    sha = _file_sha(owner, repo, path, branch)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    return _request("PUT", f"/repos/{owner}/{repo}/contents/{path}", payload)


def push_scaffold(owner: str, repo: str, files: dict[str, str],
                  branch: str = "main") -> list[str]:
    """Push a dict of {path: content} to the repo. Returns pushed paths."""
    pushed = []
    for path, content in files.items():
        put_file(owner, repo, path, content, f"Add {path}", branch)
        pushed.append(path)
    return pushed
