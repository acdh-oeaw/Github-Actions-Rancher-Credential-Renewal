#!/usr/bin/env python
import asyncio
import base64
import os
import ipaddress
from typing import List, Literal, Optional

import httpx2
import yaml
from nacl import encoding, public
from pydantic import BaseModel, Field, ValidationError
from urllib.parse import urlparse, urlunparse

# =========================
# Pydantic models / settings
# =========================

class RancherSettings(BaseModel):
    url: str = Field(..., description="Base URL of Rancher, e.g. https://rancher.example.com")
    token: str = Field(..., description="Rancher API Bearer token (v3 API token)")
    ttl_seconds: int = Field(
        default=172800,
        description="TTL in seconds for kubeconfig tokens (optional; if omitted, 2 days is used)",
    )

class GitHubRepoScope(BaseModel):
    type: Literal["repo"]
    owner: str
    repo: str


class GitHubOrgScope(BaseModel):
    type: Literal["org"]
    org: str
    visibility: Optional[Literal["all", "private", "selected"]] = "all"
    selected_repository_ids: Optional[List[int]] = None


GitHubScope = GitHubRepoScope | GitHubOrgScope


class TargetConfig(BaseModel):
    name: str = Field(..., description="Logical name for this target")
    rancher_cluster_id: str = Field(..., description="Rancher cluster ID (e.g. c-m-abc123)")
    github_scope: GitHubScope
    secret_name: str = Field(..., description="GitHub secret name to update")


class GitHubSettings(BaseModel):
    token: str = Field(..., description="GitHub PAT with actions:write/admin:org as needed")


class AppConfig(BaseModel):
    rancher: RancherSettings
    github: GitHubSettings
    targets: List[TargetConfig]


# =========================
# Config loading
# =========================

def load_config(path: Optional[str] = None) -> AppConfig:
    """
    Load configuration from YAML file and/or environment variables.

    - YAML for full config (Rancher, GitHub, targets).
    - ENV can override:
        RANCHER_URL, RANCHER_TOKEN, GITHUB_TOKEN
    """
    data: dict = {}

    if path:
        if not os.path.exists(path):
            raise SystemExit(f"Config file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
        data.update(yaml_data)

    # Env overrides for main credentials
    rancher_url = os.getenv("RANCHER_URL")
    rancher_token = os.getenv("RANCHER_TOKEN")
    github_token = os.getenv("GITHUB_TOKEN")

    if rancher_url or rancher_token:
        data.setdefault("rancher", {})
        if rancher_url:
            data["rancher"]["url"] = rancher_url
        if rancher_token:
            data["rancher"]["token"] = rancher_token

    if github_token:
        data.setdefault("github", {})
        data["github"]["token"] = github_token

    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise SystemExit(f"Configuration error:\n{e}") from e


# =========================
# Crypto helpers (GitHub)
# =========================

def encrypt_for_github(public_key_b64: str, secret_value: str) -> str:
    """
    Encrypt a string for GitHub Actions secrets using the repository/org public key
    and libsodium sealed boxes (PyNaCl).
    """
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


# =========================
# Rancher 2.14 API
# =========================

async def get_kubeconfig_for_cluster(
    client: httpx2.AsyncClient,
    rancher: RancherSettings,
    cluster_id: str,
) -> str:
    """
    Use Rancher 2.14's ext.cattle.io/v1 Kubeconfig CRD endpoint to get a kubeconfig
    for a single cluster. No merging is done.
    """
    url = f"{rancher.url.rstrip('/')}/apis/ext.cattle.io/v1/kubeconfigs"
    payload = {
        "apiVersion": "ext.cattle.io/v1",
        "kind": "Kubeconfig",
        "spec": {
            "clusters": [cluster_id],
            "currentContext": cluster_id,
            "ttl": rancher.ttl_seconds,
        },
    }

    headers = {
        "Authorization": f"Bearer {rancher.token}",
        "Content-Type": "application/json",
    }

    resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    kubeconfig = body.get("status", {}).get("value")
    if not kubeconfig:
        raise RuntimeError(
            f"Rancher did not return .status.value for cluster {cluster_id}; "
            f"response was: {body}"
        )
    return kubeconfig

def adjust_kubeconfig(
    kubeconfig_yaml: str,
    external_host: str,
) -> str:
    """
    Adjust kubeconfig if any cluster.server host is in 10.x.x.x range:

    - For each cluster:
      - If server host is in 10.0.0.0/8:
        - Replace host with external_host.
        - Remove certificate-authority / certificate-authority-data.
        - Set insecure-skip-tls-verify: true.
    Returns updated kubeconfig YAML as string.
    """
    data = yaml.safe_load(kubeconfig_yaml)

    clusters = data.get("clusters") or []
    for c in clusters:
        cluster = c.get("cluster") or {}
        server = cluster.get("server")
        if not server:
            continue

        parsed = urlparse(server)
        host = parsed.hostname
        if not host:
            continue

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Not an IP, skip
            continue

        # Check if it's in 10.0.0.0/8
        if ip.is_private and ip in ipaddress.ip_network("10.0.0.0/8"):
            # Replace host with external_host, keep scheme and port
            new_netloc = external_host
            if parsed.port:
                new_netloc = f"{external_host}:{parsed.port}"
            new_url = urlunparse(parsed._replace(netloc=new_netloc))
            cluster["server"] = new_url

            # Remove CA fields and set insecure-skip-tls-verify
            cluster.pop("certificate-authority-data", None)
            cluster.pop("certificate-authority", None)

    return yaml.safe_dump(data)

def get_external_host_from_rancher_url(rancher_url: str) -> str:
    parsed = urlparse(rancher_url)
    return parsed.hostname or rancher_url

# =========================
# GitHub API helpers
# =========================

GITHUB_API_VERSION = "2022-11-28"
GITHUB_API_BASE = "https://api.github.com"


async def get_github_public_key(
    client: httpx2.AsyncClient,
    gh: GitHubSettings,
    scope: GitHubScope,
) -> tuple[str, str]:
    """
    Fetch the GitHub public key (key, key_id) for a repo or org.
    """
    headers = {
        "Authorization": f"Bearer {gh.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    if isinstance(scope, GitHubRepoScope):
        url = f"{GITHUB_API_BASE}/repos/{scope.owner}/{scope.repo}/actions/secrets/public-key"
    else:
        url = f"{GITHUB_API_BASE}/orgs/{scope.org}/actions/secrets/public-key"

    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["key"], data["key_id"]

async def put_github_secret(
    client: httpx2.AsyncClient,
    gh: GitHubSettings,
    scope: GitHubScope,
    secret_name: str,
    encrypted_value: str,
    key_id: str,
) -> None:
    """
    PUT a GitHub secret (repo or org scope).
    For org scope, this can also set visibility/selected repositories.
    """
    headers = {
        "Authorization": f"Bearer {gh.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    if isinstance(scope, GitHubRepoScope):
        url = f"{GITHUB_API_BASE}/repos/{scope.owner}/{scope.repo}/actions/secrets/{secret_name}"
        payload = {
            "encrypted_value": encrypted_value,
            "key_id": key_id,
        }
    else:
        url = f"{GITHUB_API_BASE}/orgs/{scope.org}/actions/secrets/{secret_name}"
        payload = {
            "encrypted_value": encrypted_value,
            "key_id": key_id,
        }
        if scope.visibility:
            payload["visibility"] = scope.visibility
        if scope.visibility == "selected" and scope.selected_repository_ids:
            payload["selected_repository_ids"] = scope.selected_repository_ids

    resp = await client.put(url, headers=headers, json=payload)
    resp.raise_for_status()


# =========================
# Workflow per target
# =========================

async def process_target(
    client: httpx2.AsyncClient,
    cfg: AppConfig,
    target: TargetConfig,
) -> None:
    print(f"=== Processing target: {target.name} ===")

    # 1) Get kubeconfig for exactly one Rancher cluster
    kubeconfig = await get_kubeconfig_for_cluster(
        client, cfg.rancher, target.rancher_cluster_id
    )
    print(f"Fetched kubeconfig for cluster {target.rancher_cluster_id}")

    # 1a) Adjust kubeconfig if server host is 10.x.x.x
    external_host = urlparse(cfg.rancher.url).hostname or cfg.rancher.url
    kubeconfig_adjusted = adjust_kubeconfig(kubeconfig, external_host)
    print("Adjusted kubeconfig (server + TLS) where needed")

    # 1b) Base64-encode the kubeconfig (so the secret value itself is base64)
    kubeconfig_b64 = base64.b64encode(kubeconfig_adjusted.encode("utf-8")).decode("ascii")
    print("Base64-encoded kubeconfig")

    # 2) Get GitHub public key for this scope (repo or org)
    public_key_b64, key_id = await get_github_public_key(
        client, cfg.github, target.github_scope
    )
    print("Got GitHub public key")

    # 3) Encrypt kubeconfig
    encrypted_value = encrypt_for_github(public_key_b64, kubeconfig_b64)
    print("Encrypted kubeconfig")

    # 4) PUT secret
    await put_github_secret(
        client,
        cfg.github,
        target.github_scope,
        target.secret_name,
        encrypted_value,
        key_id,
    )
    print(f"Updated GitHub secret: {target.secret_name}\n")


# =========================
# Entry point
# =========================

async def main() -> None:
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    cfg = load_config(config_path)

    limits = httpx2.Limits(max_keepalive_connections=10, max_connections=20)
    timeout = httpx2.Timeout(30.0)

    async with httpx2.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [process_target(client, cfg, t) for t in cfg.targets]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())