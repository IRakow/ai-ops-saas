"""
Git workspace management — clone, pull, push, create PRs.
Each tenant gets an isolated workspace directory.
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.crypto import decrypt_credential
from app.supabase_client import get_supabase_client
import config

logger = logging.getLogger("ai_ops.git")


def _build_auth_url(repo_url: str, credential: str, provider: str) -> str:
    """Inject credentials into a git URL for HTTPS cloning."""
    if not credential:
        return repo_url

    # For HTTPS URLs: https://TOKEN@github.com/owner/repo.git
    if repo_url.startswith("https://"):
        if provider == "github":
            return repo_url.replace("https://", f"https://x-access-token:{credential}@")
        elif provider == "gitlab":
            return repo_url.replace("https://", f"https://oauth2:{credential}@")
        else:
            return repo_url.replace("https://", f"https://{credential}@")

    return repo_url


def _run_git(args: list[str], cwd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a git command in the given directory."""
    cmd = ["git"] + args
    logger.debug(f"git {' '.join(args)} in {cwd}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def clone_workspace(tenant) -> str:
    """
    Clone a tenant's repo into their workspace directory.
    Returns the workspace path.
    """
    workspace = Path(tenant.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    # Decrypt credential
    credential = ""
    if tenant.git_credentials_encrypted:
        credential = decrypt_credential(tenant.git_credentials_encrypted, config.SECRET_KEY)

    auth_url = _build_auth_url(tenant.git_repo_url, credential, tenant.git_provider)

    # Clone with shallow depth for speed
    result = _run_git(
        ["clone", "--depth", "50", auth_url, str(workspace)],
        cwd=str(workspace.parent),
        timeout=300,
    )

    if result.returncode != 0:
        # Clean up failed clone
        if workspace.exists():
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
        raise RuntimeError(f"Git clone failed: {result.stderr}")

    # Update last sync timestamp
    sb = get_supabase_client()
    sb.table("tenants").update({
        "last_git_sync": datetime.now(timezone.utc).isoformat(),
    }).eq("id", tenant.id).execute()

    logger.info(f"Cloned {tenant.git_repo_url} to {workspace}")
    return str(workspace)


def sync_workspace(tenant) -> None:
    """Pull latest changes from remote before an agent run."""
    workspace = Path(tenant.workspace_path)
    if not workspace.exists():
        clone_workspace(tenant)
        return

    branch = tenant.git_default_branch or "main"

    # Fetch + hard reset to ensure clean state
    _run_git(["fetch", "origin", branch], cwd=str(workspace))
    _run_git(["reset", "--hard", f"origin/{branch}"], cwd=str(workspace))
    _run_git(["clean", "-fd"], cwd=str(workspace))

    # Update sync timestamp
    sb = get_supabase_client()
    sb.table("tenants").update({
        "last_git_sync": datetime.now(timezone.utc).isoformat(),
    }).eq("id", tenant.id).execute()

    logger.info(f"Synced workspace for tenant {tenant.slug}")


def commit_and_push(tenant, session_id: str, description: str) -> str | None:
    """
    Commit agent changes and push/PR based on tenant's deploy method.
    Returns PR URL if applicable, or None.
    """
    workspace = Path(tenant.workspace_path)

    # Check for changes
    status = _run_git(["status", "--porcelain"], cwd=str(workspace))
    if not status.stdout.strip():
        logger.info("No changes to commit")
        return None

    deploy_branch = tenant.git_deploy_branch or "main"

    if tenant.deploy_method == "github_pr":
        return _push_as_pr(tenant, workspace, session_id, description, deploy_branch)
    elif tenant.deploy_method == "git_push":
        return _push_direct(tenant, workspace, session_id, description, deploy_branch)
    else:
        # Just commit locally — webhook or manual delivery
        _run_git(["add", "-A"], cwd=str(workspace))
        _run_git(["commit", "-m", f"fix: {description} [AI Ops #{session_id[:8]}]"], cwd=str(workspace))
        return None


def _push_as_pr(tenant, workspace: Path, session_id: str, description: str, base_branch: str) -> str | None:
    """Create a PR on GitHub/GitLab."""
    branch_name = f"ai-ops/fix-{session_id[:8]}"

    _run_git(["checkout", "-b", branch_name], cwd=str(workspace))
    _run_git(["add", "-A"], cwd=str(workspace))
    _run_git(["commit", "-m", f"fix: {description} [AI Ops #{session_id[:8]}]"], cwd=str(workspace))

    # Set up auth for push
    credential = ""
    if tenant.git_credentials_encrypted:
        credential = decrypt_credential(tenant.git_credentials_encrypted, config.SECRET_KEY)

    auth_url = _build_auth_url(tenant.git_repo_url, credential, tenant.git_provider)
    _run_git(["remote", "set-url", "origin", auth_url], cwd=str(workspace))

    push_result = _run_git(["push", "-u", "origin", branch_name], cwd=str(workspace), timeout=120)
    if push_result.returncode != 0:
        logger.error(f"Push failed: {push_result.stderr}")
        return None

    # Create PR via GitHub API
    if tenant.git_provider == "github":
        pr_url = _create_github_pr(tenant, credential, branch_name, base_branch, description, session_id)
        return pr_url

    # Switch back to default branch
    _run_git(["checkout", base_branch], cwd=str(workspace))

    return None


def _push_direct(tenant, workspace: Path, session_id: str, description: str, branch: str) -> None:
    """Push directly to the deploy branch."""
    _run_git(["add", "-A"], cwd=str(workspace))
    _run_git(["commit", "-m", f"fix: {description} [AI Ops #{session_id[:8]}]"], cwd=str(workspace))

    credential = ""
    if tenant.git_credentials_encrypted:
        credential = decrypt_credential(tenant.git_credentials_encrypted, config.SECRET_KEY)

    auth_url = _build_auth_url(tenant.git_repo_url, credential, tenant.git_provider)
    _run_git(["remote", "set-url", "origin", auth_url], cwd=str(workspace))
    _run_git(["push", "origin", branch], cwd=str(workspace), timeout=120)
    return None


def _create_github_pr(tenant, token: str, head: str, base: str, title: str, session_id: str) -> str | None:
    """Create a GitHub pull request via API."""
    import requests

    # Extract owner/repo from URL
    # https://github.com/owner/repo.git → owner/repo
    repo_url = tenant.git_repo_url.rstrip("/").removesuffix(".git")
    parts = repo_url.split("/")
    if len(parts) < 2:
        return None
    owner_repo = f"{parts[-2]}/{parts[-1]}"

    response = requests.post(
        f"https://api.github.com/repos/{owner_repo}/pulls",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={
            "title": f"fix: {title[:80]}",
            "body": f"Automated fix by AI Ops (session #{session_id[:8]}).\n\nReview the changes and merge when ready.",
            "head": head,
            "base": base,
        },
    )

    if response.status_code == 201:
        pr_url = response.json().get("html_url")
        logger.info(f"Created PR: {pr_url}")
        return pr_url
    else:
        logger.error(f"PR creation failed: {response.status_code} {response.text}")
        return None


def delete_workspace(tenant) -> None:
    """Remove a tenant's workspace directory."""
    import shutil
    workspace = Path(tenant.workspace_path)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info(f"Deleted workspace for tenant {tenant.slug}")
