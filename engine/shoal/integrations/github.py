"""GitHub integration: repositories, pull requests, reviews and merges.

Auth: a personal access / fine-grained token, or a GitHub App installation
(short-lived installation tokens are minted and cached automatically).
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from pydantic import BaseModel, Field, SecretStr, model_validator

from shoal.integrations.base import (
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Page,
    action,
    raise_for_status,
    resource,
)

API = "https://api.github.com"


class AttributionPolicy(BaseModel):
    """How work is attributed to the person who drove it (architecture §11.1)."""

    mode: Literal["app", "user"] = Field(
        "app", description="'user' acts with the initiator's linked GitHub token; 'app' uses the connection credential."
    )
    fallback: Literal["fail", "app"] = Field("fail", description="user mode: what to do when the initiator has no linked token.")
    labels: bool = Field(True, description="Add provenance labels to PRs and issues.")
    label_prefix: str = "shoal:"
    requested_by_trailer: bool = Field(True, description="Append 'Requested-by: @login' to PR and issue bodies.")
    assign_initiator: bool = False
    request_initiator_review: bool = False
    elevation: Literal["deny", "qualified_approval", "allow"] = Field(
        "qualified_approval",
        description="When the initiator lacks write access: never merge (deny), merge only after approval from "
        "someone with write access (qualified_approval), or merge on the app's rights (allow).",
    )
    require_merge_check: bool = Field(
        False, description="Require a passing 'shoal/merge-check' commit status (agent-assisted review) before merging."
    )
    required_labels: list[str] = Field(default_factory=list, description="Labels that must be on a PR before merge.")


MERGE_CHECK_CONTEXT = "shoal/merge-check"


class GitHubConnection(BaseModel):
    base_url: str = Field(API, description="API base URL (GitHub Enterprise: https://ghe.example.com/api/v3).")
    token: SecretStr | None = Field(None, description="Personal access or fine-grained token.")
    app_id: str | None = Field(None, description="GitHub App id (instead of a token).")
    private_key: SecretStr | None = Field(None, description="GitHub App private key (PEM).")
    installation_id: str | None = None
    webhook_secret: SecretStr | None = Field(None, description="Secret used to sign webhook deliveries.")
    git_author_name: str = "Shoal"
    git_author_email: str = "shoal-bot@users.noreply.github.com"
    attribution: AttributionPolicy = Field(default_factory=AttributionPolicy)

    @model_validator(mode="after")
    def _auth(self) -> GitHubConnection:
        if not self.token and not (self.app_id and self.private_key and self.installation_id):
            raise ValueError("configure either 'token' or 'app_id' + 'private_key' + 'installation_id'")
        return self


# ----------------------------------------------------------------------------- models


class RepoRef(BaseModel):
    repo: str = Field(description="owner/name", pattern=r"^[\w.-]+/[\w.-]+$")


class PullRef(RepoRef):
    number: int


class CreatePullRequest(RepoRef):
    title: str
    head: str = Field(description="Branch with the changes.")
    base: str | None = Field(None, description="Target branch (default: repository default branch).")
    body: str = ""
    draft: bool = False
    labels: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    mode: str
    initiator: str | None = Field(None, description="GitHub login (or display name) of the person who drove the work.")
    initiator_permission: str = Field("unknown", description="admin, maintain, write, triage, read, none or unknown.")
    initiator_can_merge: bool = False
    labels: list[str] = Field(default_factory=list)


class PullRequest(BaseModel):
    number: int
    url: str
    state: str
    title: str
    head: str
    base: str
    draft: bool = False
    created: bool = True
    provenance: Provenance | None = None


class PullRequestStatus(BaseModel):
    number: int
    url: str
    state: str
    merged: bool
    draft: bool
    mergeable: bool | None
    mergeable_state: str | None
    review_decision: Literal["approved", "changes_requested", "review_required"]
    approvals: int
    changes_requested: int
    checks: Literal["success", "failure", "pending", "none"]
    head_sha: str
    updated_at: str | None = None


class MergePullRequest(PullRef):
    method: Literal["merge", "squash", "rebase"] = "squash"
    commit_title: str | None = None
    require_approval: bool = True


class MergeResult(BaseModel):
    merged: bool
    sha: str | None = None
    reason: str | None = None
    governance: dict[str, Any] | None = None


class CanMerge(BaseModel):
    can_merge: bool
    reasons: list[str]
    permission: str
    governance: dict[str, Any] = Field(default_factory=dict)


class MergeCheck(PullRef):
    verdict: Literal["pass", "fail"]
    summary: str = Field(max_length=140, description="Short reason shown on the commit status.")
    details_url: str | None = None


class MergeCheckResult(BaseModel):
    state: str
    sha: str


class AddComment(PullRef):
    body: str


class Comment(BaseModel):
    id: int
    url: str


class ClosePullRequest(PullRef):
    comment: str | None = None


class CreateIssue(RepoRef):
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    number: int
    url: str
    provenance: Provenance | None = None


class GetFile(RepoRef):
    path: str
    ref: str | None = None


class FileContent(BaseModel):
    path: str
    content: str | None
    exists: bool
    sha: str | None = None


class Repository(BaseModel):
    id: str
    full_name: str
    name: str
    owner: str
    default_branch: str
    private: bool
    archived: bool
    fork: bool
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    url: str
    clone_url: str
    permission: str


class RepoFilter(BaseModel):
    org: str | None = Field(None, description="List an organization's repositories.")
    owner: str | None = Field(None, description="Only repositories owned by this user/org.")
    affiliation: str = Field("owner,organization_member", description="For the authenticated user's repos.")
    permission: Literal["admin", "maintain", "push", "triage", "pull"] | None = Field(
        None, description="Minimum permission the connection must have."
    )
    archived: bool | None = False
    fork: bool | None = None
    visibility: Literal["all", "public", "private"] = "all"
    topic: str | None = None
    language: str | None = None
    name: str | None = Field(None, description="Glob on the repository name, e.g. 'svc-*'.")


class PullFilter(RepoRef):
    state: Literal["open", "closed", "all"] = "open"
    head: str | None = None


_PERMISSION_ORDER = ["pull", "triage", "push", "maintain", "admin"]
WRITE_ROLES = {"admin", "maintain", "write"}


def _permission(perms: dict[str, bool] | None) -> str:
    perms = perms or {}
    for level in reversed(_PERMISSION_ORDER):
        if perms.get(level):
            return level
    return "none"


def _at_least(actual: str, wanted: str) -> bool:
    if actual == "none":
        return False
    return _PERMISSION_ORDER.index(actual) >= _PERMISSION_ORDER.index(wanted)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    return f"{header}.{payload}.{_b64url(signature)}"


class GitHubIntegration(Integration):
    key = "github"
    title = "GitHub"
    description = "Repositories, pull requests, reviews, merges and webhooks."
    connection_model = GitHubConnection
    events = [
        EventDef("pull_request.*", "Pull request opened, closed, synchronized, ..."),
        EventDef("pull_request_review.submitted", "A review was submitted on a pull request."),
        EventDef("issue_comment.created", "A comment on an issue or pull request."),
        EventDef("issues.*", "Issue opened, labeled, closed, ..."),
        EventDef("push", "Commits pushed to a branch."),
        EventDef("check_suite.completed", "CI finished for a commit."),
    ]

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ http

    async def _user_token(self, ctx: ConnectionContext) -> str | None:
        """In user mode, the initiator's own linked token (None means: act as the app)."""
        policy: AttributionPolicy = ctx.config.attribution
        if policy.mode != "user" or not ctx.initiator:
            return None
        token = await ctx.engine.identities.github_token(ctx.tenant_id, ctx.initiator)
        if token is None and policy.fallback == "fail":
            who = ctx.initiator.get("display_name") or ctx.initiator.get("actor") or "the initiator"
            raise IntegrationError(
                f"{who} has not linked a GitHub account, and this connection acts with the initiator's own "
                "credentials (attribution.mode=user)",
                status=403,
            )
        return token

    async def _token(self, ctx: ConnectionContext) -> str:
        return await self._user_token(ctx) or await self._app_token(ctx)

    async def _app_token(self, ctx: ConnectionContext) -> str:
        cfg: GitHubConnection = ctx.config
        if cfg.token:
            return cfg.token.get_secret_value()
        cache_key = f"{ctx.tenant_id}:{ctx.name}"
        cached = self._tokens.get(cache_key)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        jwt = app_jwt(cfg.app_id or "", cfg.private_key.get_secret_value() if cfg.private_key else "")
        async with ctx.http(base_url=cfg.base_url) as client:
            response = await client.post(
                f"/app/installations/{cfg.installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
            )
        raise_for_status(response, "GitHub")
        data = response.json()
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[cache_key] = (data["token"], expires)
        return data["token"]

    async def _request(self, ctx: ConnectionContext, method: str, path: str, **kwargs: Any) -> Any:
        token = await self._token(ctx)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with ctx.http(base_url=ctx.config.base_url, headers=headers) as client:
            response = await client.request(method, path, **kwargs)
        raise_for_status(response, "GitHub")
        return response.json() if response.content else {}

    async def test_connection(self, ctx: ConnectionContext) -> dict[str, Any]:
        if ctx.config.token:
            user = await self._request(ctx, "GET", "/user")
            return {"ok": True, "login": user.get("login")}
        await self._token(ctx)
        return {"ok": True, "installation_id": ctx.config.installation_id}

    async def sandbox_credentials(self, ctx: ConnectionContext) -> dict[str, str]:
        token = await self._token(ctx)
        cfg: GitHubConnection = ctx.config
        host = "github.com" if cfg.base_url == API else cfg.base_url.split("//", 1)[-1].split("/", 1)[0]
        return {
            "GITHUB_TOKEN": token,
            "GH_TOKEN": token,
            "GH_HOST": host,
            "GIT_AUTHOR_NAME": cfg.git_author_name,
            "GIT_AUTHOR_EMAIL": cfg.git_author_email,
            "GIT_COMMITTER_NAME": cfg.git_author_name,
            "GIT_COMMITTER_EMAIL": cfg.git_author_email,
            # credential helper: `git clone https://$GH_HOST/owner/repo` works without prompts
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.https://x-access-token:{token}@{host}/.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://{host}/",
        }

    # ------------------------------------------------------------------ attribution and governance

    async def _user_permission(self, ctx: ConnectionContext, repo: str, login: str | None) -> str:
        if not login:
            return "unknown"
        try:
            data = await self._request(ctx, "GET", f"/repos/{repo}/collaborators/{login}/permission")
        except IntegrationError as exc:
            if exc.status in (403, 404):
                return "none"
            raise
        return data.get("role_name") or data.get("permission") or "none"

    async def provenance(self, ctx: ConnectionContext, repo: str) -> Provenance:
        policy: AttributionPolicy = ctx.config.attribution
        initiator = ctx.initiator or {}
        login = initiator.get("github")
        user_mode = await self._user_token(ctx) is not None
        permission = await self._user_permission(ctx, repo, login)
        can_merge = permission in WRITE_ROLES
        labels: list[str] = []
        if policy.labels:
            prefix = policy.label_prefix
            labels = [f"{prefix}automated", f"{prefix}initiator-{'can' if can_merge else 'cannot'}-merge"]
            if user_mode:
                labels.append(f"{prefix}user-credentials")
        return Provenance(
            mode="user" if user_mode else "app",
            initiator=login or initiator.get("display_name"),
            initiator_permission=permission,
            initiator_can_merge=can_merge,
            labels=labels,
        )

    def _requested_by(self, ctx: ConnectionContext, body: str) -> str:
        initiator = ctx.initiator or {}
        if not ctx.config.attribution.requested_by_trailer or not initiator:
            return body
        who = f"@{initiator['github']}" if initiator.get("github") else (
            f"{initiator.get('display_name') or initiator.get('actor')} (no linked GitHub account)"
        )
        return f"{body.rstrip()}\n\nRequested-by: {who}"

    async def _audit(self, ctx: ConnectionContext, action_name: str, target: str, **data: Any) -> None:
        actor = (ctx.initiator or {}).get("actor")
        await ctx.engine.store.audit(ctx.tenant_id, actor, f"github.{action_name}", target, run_id=ctx.run_id, initiator=ctx.initiator, **data)

    async def governance(self, ctx: ConnectionContext, repo: str, number: int, head_sha: str) -> dict[str, Any]:
        """Merge-gating decision for the current initiator (enforced in merge_pull_request)."""
        policy: AttributionPolicy = ctx.config.attribution
        prov = await self.provenance(ctx, repo)
        result: dict[str, Any] = {
            "mode": prov.mode,
            "initiator": prov.initiator,
            "initiator_permission": prov.initiator_permission,
            "elevation": "not_needed",
            "reasons": [],
        }
        if prov.mode == "app" and not prov.initiator_can_merge:
            who = prov.initiator or "the initiator"
            if policy.elevation == "deny":
                result["elevation"] = "denied"
                result["reasons"].append(f"{who} lacks write access to {repo} and policy forbids merging on the app's rights")
            elif policy.elevation == "qualified_approval":
                reviews = await self._request(ctx, "GET", f"/repos/{repo}/pulls/{number}/reviews", params={"per_page": 100})
                latest: dict[str, str] = {}
                for review in reviews:
                    if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                        latest[review["user"]["login"]] = review["state"]
                approvers = [login for login, state in latest.items() if state == "APPROVED"]
                qualified = [a for a in approvers if await self._user_permission(ctx, repo, a) in WRITE_ROLES]
                result["qualified_approvers"] = qualified
                if qualified:
                    result["elevation"] = "qualified_approval"
                else:
                    result["elevation"] = "awaiting_qualified_approval"
                    result["reasons"].append(
                        f"{who} lacks write access to {repo}; an approval from someone with write access is required"
                    )
            else:
                result["elevation"] = "allowed"
        if policy.require_merge_check:
            status = await self._request(ctx, "GET", f"/repos/{repo}/commits/{head_sha}/status")
            check = next((st for st in status.get("statuses", []) if st.get("context") == MERGE_CHECK_CONTEXT), None)
            result["merge_check"] = check.get("state") if check else "missing"
            if result["merge_check"] != "success":
                result["reasons"].append(f"merge check ({MERGE_CHECK_CONTEXT}) is {result['merge_check']}")
        if policy.required_labels:
            present = {label["name"] for label in await self._request(ctx, "GET", f"/repos/{repo}/issues/{number}/labels")}
            missing = [label for label in policy.required_labels if label not in present]
            if missing:
                result["reasons"].append(f"missing required labels: {', '.join(missing)}")
        return result

    # ------------------------------------------------------------------ actions

    @action(description="Get repository details and the connection's permission.", input=RepoRef, output=Repository, mutating=False)
    async def get_repository(self, ctx: ConnectionContext, p: RepoRef) -> Repository:
        return self._repo(await self._request(ctx, "GET", f"/repos/{p.repo}"))

    @action(description="Open a pull request (returns the existing one if the branch already has an open PR).", input=CreatePullRequest, output=PullRequest)
    async def create_pull_request(self, ctx: ConnectionContext, p: CreatePullRequest) -> PullRequest:
        base = p.base or (await self._request(ctx, "GET", f"/repos/{p.repo}"))["default_branch"]
        owner = p.repo.split("/")[0]
        existing = await self._request(
            ctx, "GET", f"/repos/{p.repo}/pulls", params={"head": f"{owner}:{p.head}", "state": "open"}
        )
        prov = await self.provenance(ctx, p.repo)
        if existing:
            pr = existing[0]
            return PullRequest(number=pr["number"], url=pr["html_url"], state=pr["state"], title=pr["title"], head=p.head, base=pr["base"]["ref"], draft=pr.get("draft", False), created=False, provenance=prov)
        pr = await self._request(
            ctx,
            "POST",
            f"/repos/{p.repo}/pulls",
            json={"title": p.title, "head": p.head, "base": base, "body": self._requested_by(ctx, p.body), "draft": p.draft},
        )
        labels = list(dict.fromkeys([*p.labels, *prov.labels]))
        if labels:
            await self._request(ctx, "POST", f"/repos/{p.repo}/issues/{pr['number']}/labels", json={"labels": labels})
        await self._assign(ctx, p.repo, pr["number"], pull=True)
        await self._audit(ctx, "pull_request.created", pr["html_url"], provenance=prov.model_dump())
        return PullRequest(number=pr["number"], url=pr["html_url"], state=pr["state"], title=pr["title"], head=p.head, base=base, draft=p.draft, provenance=prov)

    async def _assign(self, ctx: ConnectionContext, repo: str, number: int, *, pull: bool) -> None:
        """Forced ownership: assign the initiator and/or request their review (best effort)."""
        policy: AttributionPolicy = ctx.config.attribution
        login = (ctx.initiator or {}).get("github")
        if not login:
            return
        try:
            if policy.assign_initiator:
                await self._request(ctx, "POST", f"/repos/{repo}/issues/{number}/assignees", json={"assignees": [login]})
            if pull and policy.request_initiator_review:
                await self._request(ctx, "POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", json={"reviewers": [login]})
        except IntegrationError as exc:
            if exc.retryable:
                raise
            await self._audit(ctx, "assignment.failed", f"{repo}#{number}", error=str(exc))

    @action(description="Pull request state, mergeability, review decision and CI status.", input=PullRef, output=PullRequestStatus, mutating=False)
    async def get_pull_request(self, ctx: ConnectionContext, p: PullRef) -> PullRequestStatus:
        pr = await self._request(ctx, "GET", f"/repos/{p.repo}/pulls/{p.number}")
        reviews = await self._request(ctx, "GET", f"/repos/{p.repo}/pulls/{p.number}/reviews", params={"per_page": 100})
        latest: dict[str, str] = {}
        for review in reviews:
            state = review.get("state")
            if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[review["user"]["login"]] = state
        approvals = sum(1 for s in latest.values() if s == "APPROVED")
        changes = sum(1 for s in latest.values() if s == "CHANGES_REQUESTED")
        decision = "changes_requested" if changes else "approved" if approvals else "review_required"
        checks = await self._checks(ctx, p.repo, pr["head"]["sha"])
        return PullRequestStatus(
            number=pr["number"],
            url=pr["html_url"],
            state=pr["state"],
            merged=bool(pr.get("merged")),
            draft=bool(pr.get("draft")),
            mergeable=pr.get("mergeable"),
            mergeable_state=pr.get("mergeable_state"),
            review_decision=decision,  # type: ignore[arg-type]
            approvals=approvals,
            changes_requested=changes,
            checks=checks,  # type: ignore[arg-type]
            head_sha=pr["head"]["sha"],
            updated_at=pr.get("updated_at"),
        )

    async def _checks(self, ctx: ConnectionContext, repo: str, sha: str) -> str:
        runs = await self._request(ctx, "GET", f"/repos/{repo}/commits/{sha}/check-runs", params={"per_page": 100})
        status = await self._request(ctx, "GET", f"/repos/{repo}/commits/{sha}/status")
        conclusions = [r.get("conclusion") for r in runs.get("check_runs", [])]
        pending = any(r.get("status") != "completed" for r in runs.get("check_runs", []))
        if status.get("total_count"):
            conclusions.append({"success": "success", "pending": None}.get(status.get("state"), "failure"))
            pending = pending or status.get("state") == "pending"
        if not conclusions:
            return "none"
        if any(c in ("failure", "timed_out", "cancelled", "action_required") for c in conclusions):
            return "failure"
        return "pending" if pending else "success"

    @action(description="Check whether this connection may merge the PR right now, and why not.", input=PullRef, output=CanMerge, mutating=False)
    async def can_merge(self, ctx: ConnectionContext, p: PullRef) -> CanMerge:
        repo = await self._request(ctx, "GET", f"/repos/{p.repo}")
        permission = _permission(repo.get("permissions"))
        status = await self.get_pull_request(ctx, p)
        reasons = []
        if not _at_least(permission, "push"):
            reasons.append(f"connection has '{permission}' permission; merging needs push or higher")
        if status.state != "open":
            reasons.append("pull request is merged" if status.merged else f"pull request is {status.state}")
        if status.draft:
            reasons.append("pull request is a draft")
        if status.review_decision != "approved":
            reasons.append(f"review decision is {status.review_decision}")
        if status.mergeable is False or status.mergeable_state in ("dirty", "blocked", "behind"):
            reasons.append(f"not mergeable (state: {status.mergeable_state})")
        if status.checks == "failure":
            reasons.append("checks are failing")
        governance = await self.governance(ctx, p.repo, p.number, status.head_sha)
        reasons += governance["reasons"]
        return CanMerge(can_merge=not reasons, reasons=reasons, permission=permission, governance=governance)

    @action(description="Merge a pull request if permitted. Returns merged=false with a reason instead of failing.", input=MergePullRequest, output=MergeResult)
    async def merge_pull_request(self, ctx: ConnectionContext, p: MergePullRequest) -> MergeResult:
        check = await self.can_merge(ctx, PullRef(repo=p.repo, number=p.number))
        blocking = [r for r in check.reasons if p.require_approval or not r.startswith("review decision")]
        if blocking:
            return MergeResult(merged=False, reason="; ".join(blocking), governance=check.governance)
        body: dict[str, Any] = {"merge_method": p.method}
        if p.commit_title:
            body["commit_title"] = p.commit_title
        try:
            result = await self._request(ctx, "PUT", f"/repos/{p.repo}/pulls/{p.number}/merge", json=body)
        except IntegrationError as exc:
            if exc.status in (405, 409, 422):
                return MergeResult(merged=False, reason=str(exc))
            raise
        merged = bool(result.get("merged"))
        policy: AttributionPolicy = ctx.config.attribution
        if merged and check.governance.get("elevation") in ("qualified_approval", "allowed") and policy.labels:
            label = f"{policy.label_prefix}merged-via-{check.governance['elevation'].replace('_', '-')}"
            await self._request(ctx, "POST", f"/repos/{p.repo}/issues/{p.number}/labels", json={"labels": [label]})
        await self._audit(ctx, "pull_request.merged" if merged else "pull_request.merge_failed", f"{p.repo}#{p.number}", governance=check.governance)
        return MergeResult(merged=merged, sha=result.get("sha"), reason=result.get("message"), governance=check.governance)

    @action(description="Record an agent-assisted merge check verdict as the 'shoal/merge-check' commit status on the PR head.", input=MergeCheck, output=MergeCheckResult)
    async def set_merge_check(self, ctx: ConnectionContext, p: MergeCheck) -> MergeCheckResult:
        pr = await self._request(ctx, "GET", f"/repos/{p.repo}/pulls/{p.number}")
        sha = pr["head"]["sha"]
        body: dict[str, Any] = {
            "state": "success" if p.verdict == "pass" else "failure",
            "context": MERGE_CHECK_CONTEXT,
            "description": p.summary[:140],
        }
        if p.details_url:
            body["target_url"] = p.details_url
        await self._request(ctx, "POST", f"/repos/{p.repo}/statuses/{sha}", json=body)
        await self._audit(ctx, "merge_check", f"{p.repo}#{p.number}", verdict=p.verdict, summary=p.summary, sha=sha)
        return MergeCheckResult(state=body["state"], sha=sha)

    @action(description="Comment on a pull request or issue.", input=AddComment, output=Comment)
    async def add_comment(self, ctx: ConnectionContext, p: AddComment) -> Comment:
        data = await self._request(ctx, "POST", f"/repos/{p.repo}/issues/{p.number}/comments", json={"body": p.body})
        return Comment(id=data["id"], url=data["html_url"])

    @action(description="Close a pull request, optionally with a comment.", input=ClosePullRequest, output=MergeResult)
    async def close_pull_request(self, ctx: ConnectionContext, p: ClosePullRequest) -> MergeResult:
        if p.comment:
            await self.add_comment(ctx, AddComment(repo=p.repo, number=p.number, body=p.comment))
        await self._request(ctx, "PATCH", f"/repos/{p.repo}/pulls/{p.number}", json={"state": "closed"})
        return MergeResult(merged=False, reason="closed")

    @action(description="Open an issue.", input=CreateIssue, output=Issue)
    async def create_issue(self, ctx: ConnectionContext, p: CreateIssue) -> Issue:
        prov = await self.provenance(ctx, p.repo)
        labels = list(dict.fromkeys([*p.labels, *prov.labels]))
        data = await self._request(ctx, "POST", f"/repos/{p.repo}/issues", json={"title": p.title, "body": self._requested_by(ctx, p.body), "labels": labels})
        await self._assign(ctx, p.repo, data["number"], pull=False)
        return Issue(number=data["number"], url=data["html_url"], provenance=prov)

    @action(description="Read a file from a repository.", input=GetFile, output=FileContent, mutating=False)
    async def get_file(self, ctx: ConnectionContext, p: GetFile) -> FileContent:
        params = {"ref": p.ref} if p.ref else None
        try:
            data = await self._request(ctx, "GET", f"/repos/{p.repo}/contents/{p.path.lstrip('/')}", params=params)
        except IntegrationError as exc:
            if exc.status == 404:
                return FileContent(path=p.path, content=None, exists=False)
            raise
        content = base64.b64decode(data.get("content", "")).decode(errors="replace") if data.get("encoding") == "base64" else data.get("content")
        return FileContent(path=p.path, content=content, exists=True, sha=data.get("sha"))

    # ------------------------------------------------------------------ resources

    def _repo(self, r: dict[str, Any]) -> Repository:
        return Repository(
            id=r["full_name"],
            full_name=r["full_name"],
            name=r["name"],
            owner=r["owner"]["login"],
            default_branch=r.get("default_branch") or "main",
            private=bool(r.get("private")),
            archived=bool(r.get("archived")),
            fork=bool(r.get("fork")),
            language=r.get("language"),
            topics=r.get("topics") or [],
            url=r["html_url"],
            clone_url=r["clone_url"],
            permission=_permission(r.get("permissions")),
        )

    @resource("repository", description="Repositories of an org or the authenticated user.", filter=RepoFilter)
    async def list_repositories(self, ctx: ConnectionContext, f: RepoFilter, cursor: str | None, limit: int) -> Page:
        page = int(cursor or 1)
        if f.org:
            path, params = f"/orgs/{f.org}/repos", {"type": "all" if f.visibility == "all" else f.visibility}
        else:
            path, params = "/user/repos", {"affiliation": f.affiliation, "visibility": f.visibility}
        data = await self._request(ctx, "GET", path, params={**params, "per_page": 100, "page": page})
        items = []
        for raw in data:
            repo = self._repo(raw)
            if f.owner and repo.owner.lower() != f.owner.lower():
                continue
            if f.archived is not None and repo.archived != f.archived:
                continue
            if f.fork is not None and repo.fork != f.fork:
                continue
            if f.permission and not _at_least(repo.permission, f.permission):
                continue
            if f.topic and f.topic not in repo.topics:
                continue
            if f.language and (repo.language or "").lower() != f.language.lower():
                continue
            if f.name and not fnmatch.fnmatch(repo.name, f.name):
                continue
            items.append(repo.model_dump())
        return Page(items=items, next_cursor=str(page + 1) if len(data) == 100 else None)

    @resource("pull_request", description="Pull requests of a repository.", filter=PullFilter)
    async def list_pull_requests(self, ctx: ConnectionContext, f: PullFilter, cursor: str | None, limit: int) -> Page:
        page = int(cursor or 1)
        params: dict[str, Any] = {"state": f.state, "per_page": 100, "page": page}
        if f.head:
            params["head"] = f.head
        data = await self._request(ctx, "GET", f"/repos/{f.repo}/pulls", params=params)
        items = [
            {"id": f"{f.repo}#{pr['number']}", "repo": f.repo, "number": pr["number"], "title": pr["title"], "state": pr["state"], "url": pr["html_url"], "head": pr["head"]["ref"], "author": pr["user"]["login"]}
            for pr in data
        ]
        return Page(items=items, next_cursor=str(page + 1) if len(data) == 100 else None)

    # ------------------------------------------------------------------ webhooks

    async def verify_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> bool:
        secret = ctx.config.webhook_secret
        if secret is None:
            return False  # unsigned deliveries could fire triggers as anyone; set webhook_secret to receive webhooks
        signature = headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def parse_webhook(self, ctx: ConnectionContext, headers: dict[str, str], body: bytes) -> list[IntegrationEvent]:
        kind = headers.get("x-github-event", "unknown")
        if kind == "ping":
            return []
        payload = json.loads(body or b"{}")
        action_name = payload.get("action")
        repo = (payload.get("repository") or {}).get("full_name")
        numbers: list[int] = []
        if "pull_request" in payload:
            numbers.append(payload["pull_request"]["number"])
        elif kind == "issue_comment" and payload.get("issue", {}).get("pull_request"):
            numbers.append(payload["issue"]["number"])
        for container in ("check_suite", "check_run"):
            for pr in (payload.get(container) or {}).get("pull_requests", []):
                numbers.append(pr["number"])
        data = {
            **payload,
            "event": kind,
            "repo": repo,
            "number": numbers[0] if numbers else (payload.get("issue") or {}).get("number"),
            "sender": (payload.get("sender") or {}).get("login"),
            "received_at": datetime.now(UTC).isoformat(),
        }
        return [
            IntegrationEvent(
                type=f"{kind}.{action_name}" if action_name else kind,
                id=headers.get("x-github-delivery"),
                data=data,
                wake_keys=[f"github:pr:{repo}#{n}" for n in numbers] + ([f"github:repo:{repo}"] if repo else []),
                actor={"github": data["sender"]} if data["sender"] else {},
            )
        ]

    def webhook_setup_instructions(self, url: str) -> str:
        return (
            f"In the repository or organization settings, add a webhook with payload URL {url}, content type "
            "application/json, the connection's webhook secret (required: unsigned deliveries are rejected), and events: Pull requests, Pull request reviews, "
            "Issue comments, Check suites (plus anything your triggers use)."
        )
