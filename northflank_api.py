"""Minimal Northflank REST client for the builder agent.

Manages the single multi-bot dispatcher ("bot-runner") combined service:
lists/finds services, creates a combined service from a GitHub repo,
reads and updates runtime environment variables (incl. the BOTS_JSON list
the runner uses), triggers builds/restarts, polls service status and
fetches build/runtime logs.

Auth: personal/team API token (env NORTHFLANK_API_TOKEN). Project id from
env NORTHFLANK_PROJECT_ID. If the token is organisation-scoped, set
NORTHFLANK_TEAM_ID and the client uses the /teams/{teamId}/... routes.
Base: https://api.northflank.com/v1/ (docs: https://northflank.com/docs/v1/api/use-the-api)
Never logs or stores secrets.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.northflank.com/v1"


class NorthflankError(Exception):
    pass


def _team_prefix() -> str:
    team_id = os.environ.get("NORTHFLANK_TEAM_ID", "").strip()
    return f"/teams/{team_id}" if team_id else ""


def _headers() -> dict:
    token = os.environ.get("NORTHFLANK_API_TOKEN", "").strip()
    if not token:
        raise NorthflankError("NORTHFLANK_API_TOKEN is not configured")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _scrub(text: str) -> str:
    """Remove anything that looks like a token/secret from log text."""
    text = re.sub(r"\d{6,12}:[A-Za-z0-9_-]{30,}", "[TOKEN]", text)
    text = re.sub(r"(?i)(api[_-]?hash\s*=\s*)['\"]?[A-Za-z0-9]{16,}['\"]?", r"\1[SECRET]", text)
    # Bearer tokens that may appear in API error bodies
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]{20,}", r"\1[TOKEN]", text)
    return text


def _request(method: str, path: str, payload: dict | None = None,
             params: dict | None = None) -> dict:
    url = API_BASE + _team_prefix() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        msg = body
        try:
            parsed_body = json.loads(body)
            if isinstance(parsed_body, dict):
                err = parsed_body.get("error") or parsed_body.get("message")
                if isinstance(err, dict):
                    msg = err.get("message", body)
                elif err:
                    msg = str(err)
        except Exception:  # noqa: BLE001 - keep raw body as message
            pass
        raise NorthflankError(
            f"Northflank {method} {path} failed (HTTP {exc.code}): {_scrub(msg)}"
        ) from exc
    if isinstance(parsed, dict) and parsed.get("error"):
        err = parsed["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise NorthflankError(f"Northflank API error: {_scrub(msg)}")
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed["data"]
    return parsed


# ---------------------------------------------------------------- services
def list_services(project_id: str) -> list[dict]:
    """List all services in a project. Returns a list of service dicts."""
    data = _request("GET", f"/projects/{project_id}/services")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("services", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def get_service(project_id: str, service_id: str) -> dict:
    """Get one service (id, name, runtimeEnvironment, status...)."""
    data = _request("GET", f"/projects/{project_id}/services/{service_id}")
    return data if isinstance(data, dict) else {}


def find_service_by_name(project_id: str, name: str) -> dict | None:
    """Find a service by name (or id). Returns the service dict or None."""
    for svc in list_services(project_id):
        if svc.get("name") == name or svc.get("id") == name:
            return svc
    return None


def get_runtime_env(project_id: str, service_id: str) -> dict:
    """Current runtimeEnvironment map of a service (may be empty)."""
    svc = get_service(project_id, service_id)
    env = svc.get("runtimeEnvironment") or {}
    return dict(env) if isinstance(env, dict) else {}


def create_combined_service(project_id: str, name: str, repo_url: str,
                            branch: str = "main",
                            runtime_env: dict | None = None,
                            deployment_plan: str = "nf-compute-50",
                            description: str = "") -> dict:
    """Create a combined service that builds + deploys a GitHub repo.

    Returns the created service dict (incl. its id). The service builds from
    a Dockerfile at the repo root. deployment_plan defaults to nf-compute-50
    (0.5 vCPU / 1 GB — the smallest Northflank compute plan, inside the
    Sandbox free allowance). buildPlan is left out so Northflank uses its
    default; creating the service does not start a build — call start_build
    (or rely on the repo push webhook) to build it.
    """
    payload: dict = {
        "name": name,
        "description": description or f"Combined service {name}",
        "billing": {"deploymentPlan": deployment_plan},
        "deployment": {
            "instances": 1,
            "docker": {"configType": "default"},
            "storage": {"ephemeralStorage": {"storageSize": 1024}},
        },
        "buildSource": "git",
        "vcsData": {
            "projectUrl": repo_url,
            "projectType": "github",
            "projectBranch": branch,
        },
        "buildSettings": {
            "dockerfile": {
                "buildEngine": "buildkit",
                "dockerFilePath": "/Dockerfile",
                "dockerWorkDir": "/",
            },
        },
        "runtimeEnvironment": runtime_env or {},
    }
    return _request("PUT", f"/projects/{project_id}/services/combined", payload)


def set_runtime_variable(project_id: str, service_id: str, key: str,
                         value: str) -> dict:
    """Set one runtime variable, keeping all existing ones.

    Northflank PATCH replaces the whole runtimeEnvironment map, so this
    reads the current map first, merges the change, and sends the complete
    set back. Setting a variable restarts the service workload.
    Returns the full runtimeEnvironment map that was sent.
    """
    env = get_runtime_env(project_id, service_id)
    env[key] = value
    _request("PATCH", f"/projects/{project_id}/services/combined/{service_id}",
             {"runtimeEnvironment": env})
    return env


def set_runtime_env(project_id: str, service_id: str, variables: dict) -> None:
    """Replace the service's whole runtimeEnvironment map with `variables`."""
    _request("PATCH", f"/projects/{project_id}/services/combined/{service_id}",
             {"runtimeEnvironment": dict(variables)})


# ---------------------------------------------------------------- BOTS_JSON
def get_bots_json(project_id: str, service_id: str) -> list[dict]:
    """Parse the runner service's BOTS_JSON runtime variable. [] if unset."""
    env = get_runtime_env(project_id, service_id)
    raw = (env.get("BOTS_JSON") or "").strip()
    if not raw:
        return []
    try:
        bots = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NorthflankError(f"BOTS_JSON on the runner service is not valid JSON: {exc}") from exc
    return bots if isinstance(bots, list) else []


def set_bots_json(project_id: str, service_id: str, bots: list[dict]) -> None:
    """Write the runner service's BOTS_JSON runtime variable (restarts it)."""
    set_runtime_variable(project_id, service_id, "BOTS_JSON", json.dumps(bots))


# ---------------------------------------------------------------- build / deploy
def start_build(project_id: str, service_id: str, sha: str | None = None) -> str:
    """Trigger a build of the service's linked branch. Returns the build id."""
    payload: dict = {}
    if sha:
        payload["sha"] = sha
    data = _request("POST", f"/projects/{project_id}/services/{service_id}/build", payload)
    if isinstance(data, dict):
        return str(data.get("id") or data.get("buildId") or "")
    return ""


def get_build(project_id: str, service_id: str, build_id: str) -> dict:
    data = _request("GET", f"/projects/{project_id}/services/{service_id}/build/{build_id}")
    return data if isinstance(data, dict) else {}


def restart_service(project_id: str, service_id: str) -> None:
    """Restart all running containers of a service (no rebuild)."""
    _request("POST", f"/projects/{project_id}/services/{service_id}/restart", {})


def service_status(project_id: str, service_id: str) -> dict:
    """Return {"build": <build status>, "deployment": <deployment status>}."""
    svc = get_service(project_id, service_id)
    status = svc.get("status") or {}
    build = status.get("build") or {}
    deployment = status.get("deployment") or {}
    return {
        "build": str(build.get("status", "")),
        "deployment": str(deployment.get("status", "")),
        "reason": str(deployment.get("reason", "")),
    }


def wait_for_deployment(project_id: str, service_id: str, timeout_s: int = 600,
                        poll_s: int = 15) -> str:
    """Poll a service until its deployment settles.

    Returns "COMPLETED" when the new deployment is live, "FAILED" when the
    build or deployment failed, or "TIMEOUT" if it never settles in time.
    """
    deadline = time.time() + timeout_s
    status = "UNKNOWN"
    while time.time() < deadline:
        try:
            st = service_status(project_id, service_id)
        except NorthflankError:
            time.sleep(poll_s)
            continue
        dep = (st.get("deployment") or "").upper()
        build = (st.get("build") or "").upper()
        if dep == "COMPLETED":
            return "COMPLETED"
        if dep in ("FAILED", "ERROR", "CRASHED") or build == "FAILED":
            return "FAILED"
        status = dep or build or "UNKNOWN"
        time.sleep(poll_s)
    return "TIMEOUT"


# ---------------------------------------------------------------- logs
def service_logs(project_id: str, service_id: str, log_source: str = "runtime",
                 duration_s: int = 600, limit: int = 100) -> str:
    """Fetch recent logs for a service. log_source: 'runtime' or 'build'.

    Returns the log lines (oldest first), secrets scrubbed.
    """
    data = _request("GET", "/logs", params={
        "projectId": project_id,
        "serviceId": service_id,
        "logSource": log_source,
        "duration": str(duration_s),
        "lineLimit": str(limit),
        "direction": "forward",
    })
    entries = data if isinstance(data, list) else []
    lines = []
    for entry in entries:
        if isinstance(entry, dict):
            lines.append(str(entry.get("log", "")))
        else:
            lines.append(str(entry))
    return _scrub("\n".join(lines))


def log_tail(project_id: str, service_id: str, limit: int = 40) -> str:
    """Combined tail of build + runtime logs, newest last, secrets stripped."""
    lines: list[str] = []
    for source in ("build", "runtime"):
        try:
            text = service_logs(project_id, service_id, source, 600, limit)
        except NorthflankError:
            continue
        if text:
            lines.append(text)
    return _scrub("\n".join(lines[-limit:]))
