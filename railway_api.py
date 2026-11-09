"""Minimal Railway GraphQL client for the builder agent.

Creates services, sets variables, triggers deploys and polls status/logs.
Auth: account token (env RAILWAY_TOKEN). Endpoint: backboard.railway.com.
Never logs or stores secrets.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


class RailwayError(Exception):
    pass


def _post(query: str, variables: dict | None = None) -> dict:
    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    if not token:
        raise RailwayError("RAILWAY_TOKEN is not configured")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RailwayError(f"Railway HTTP {exc.code}") from exc
    if data.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in data["errors"])
        raise RailwayError(f"Railway GraphQL error: {msgs}")
    return data.get("data", {})


def get_production_env_id(project_id: str) -> str:
    data = _post(
        "query($id: String!) { project(id: $id) { environments { edges { node { id name } } } } }",
        {"id": project_id},
    )
    edges = data["project"]["environments"]["edges"]
    for edge in edges:
        if edge["node"]["name"] == "production":
            return edge["node"]["id"]
    if edges:
        return edges[0]["node"]["id"]
    raise RailwayError("No environment found in project")


def create_service(project_id: str, env_id: str, repo: str, branch: str = "main") -> str:
    """Create a service connected to a GitHub repo. Returns service id."""
    data = _post(
        "mutation($input: ServiceCreateInput!) { serviceCreate(input: $input) { id } }",
        {"input": {
            "projectId": project_id,
            "environmentId": env_id,
            "source": {"repo": repo},
            "branch": branch,
        }},
    )
    return data["serviceCreate"]["id"]


def update_service(service_id: str, env_id: str, start_command: str) -> None:
    _post(
        "mutation($serviceId: String!, $environmentId: String!, $input: ServiceInstanceUpdateInput!) "
        "{ serviceInstanceUpdate(serviceId: $serviceId, environmentId: $environmentId, input: $input) }",
        {"serviceId": service_id, "environmentId": env_id,
         "input": {"startCommand": start_command}},
    )


def set_variables(project_id: str, env_id: str, service_id: str,
                  variables: dict[str, str], skip_deploys: bool = True) -> None:
    _post(
        "mutation($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
        {"input": {
            "projectId": project_id,
            "environmentId": env_id,
            "serviceId": service_id,
            "variables": variables,
            "skipDeploys": skip_deploys,
        }},
    )


def deploy_service(service_id: str, env_id: str, commit_sha: str | None = None) -> str:
    variables: dict = {"serviceId": service_id, "environmentId": env_id}
    if commit_sha:
        variables["commitSha"] = commit_sha
    data = _post(
        "mutation($serviceId: String!, $environmentId: String!, $commitSha: String) "
        "{ serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId, commitSha: $commitSha) }",
        variables,
    )
    return data.get("serviceInstanceDeployV2", "")


def latest_deployment(service_id: str) -> dict | None:
    data = _post(
        "query($input: DeploymentsInput!) { deployments(input: $input) { edges { node { id status createdAt } } } }",
        {"input": {"serviceId": service_id}},
    )
    edges = data.get("deployments", {}).get("edges", [])
    return edges[0]["node"] if edges else None


def deployment_status(deployment_id: str) -> dict:
    data = _post(
        "query($id: String!) { deployment(id: $id) { id status } }",
        {"id": deployment_id},
    )
    return data.get("deployment", {})


def deployment_logs(deployment_id: str, limit: int = 100) -> list[dict]:
    data = _post(
        "query($deploymentId: String!, $limit: Int) { deploymentLogs(deploymentId: $deploymentId, limit: $limit) { message timestamp severity } }",
        {"deploymentId": deployment_id, "limit": limit},
    )
    return data.get("deploymentLogs", [])


def build_logs(deployment_id: str, limit: int = 100) -> list[dict]:
    data = _post(
        "query($deploymentId: String!, $limit: Int) { buildLogs(deploymentId: $deploymentId, limit: $limit) { message timestamp } }",
        {"deploymentId": deployment_id, "limit": limit},
    )
    return data.get("buildLogs", [])


def log_tail(deployment_id: str, limit: int = 40) -> str:
    """Combined tail of build + runtime logs, newest last, secrets stripped."""
    lines: list[str] = []
    for entry in build_logs(deployment_id, limit):
        lines.append(entry.get("message", ""))
    for entry in deployment_logs(deployment_id, limit):
        lines.append(entry.get("message", ""))
    text = "\n".join(lines[-limit:])
    return _scrub(text)


def _scrub(text: str) -> str:
    """Remove anything that looks like a token/secret from log text."""
    import re
    text = re.sub(r"\d{6,12}:[A-Za-z0-9_-]{30,}", "[TOKEN]", text)
    text = re.sub(r"(?i)(api[_-]?hash\s*=\s*)['\"]?[A-Za-z0-9]{16,}['\"]?", r"\1[SECRET]", text)
    return text


def wait_for_deployment(service_id: str, timeout_s: int = 600,
                        poll_s: int = 15) -> tuple[str, str]:
    """Poll the latest deployment until SUCCESS/FAILED/CRASHED or timeout.

    Returns (status, deployment_id)."""
    deadline = time.time() + timeout_s
    dep_id = ""
    status = "UNKNOWN"
    while time.time() < deadline:
        dep = latest_deployment(service_id)
        if dep:
            dep_id = dep["id"]
            status = dep.get("status", "UNKNOWN")
            if status in ("SUCCESS", "FAILED", "CRASHED", "REMOVED"):
                return status, dep_id
        time.sleep(poll_s)
    return status, dep_id
