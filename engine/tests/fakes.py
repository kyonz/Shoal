"""In-memory fakes of the GitHub and Jira REST APIs, served through httpx.MockTransport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeGitHub:
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)
    pulls: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    reviews: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    merges: list[tuple[str, int, str]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    collaborators: dict[tuple[str, str], str] = field(default_factory=dict)
    labels: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    statuses: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    check_runs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (repo, sha) -> check run
    assignees: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    review_requests: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add_repo(self, full_name: str, *, admin: bool = True, archived: bool = False, fork: bool = False) -> None:
        owner, name = full_name.split("/")
        self.collaborators.setdefault((full_name, "reviewer"), "write")
        self.repos[full_name] = {
            "full_name": full_name,
            "name": name,
            "owner": {"login": owner},
            "default_branch": "main",
            "private": True,
            "archived": archived,
            "fork": fork,
            "language": "Python",
            "topics": [],
            "html_url": f"https://github.com/{full_name}",
            "clone_url": f"https://github.com/{full_name}.git",
            "permissions": {"admin": admin, "maintain": admin, "push": admin, "triage": True, "pull": True},
        }

    def approve(self, repo: str, number: int, user: str = "reviewer") -> None:
        head = self.pulls.get(repo, {}).get(number, {}).get("head", {}).get("sha")
        self.reviews.setdefault((repo, number), []).append({"user": {"login": user}, "state": "APPROVED", "commit_id": head})

    def push(self, repo: str, number: int, sha: str) -> None:
        """New commits on the PR branch."""
        self.pulls[repo][number]["head"]["sha"] = sha

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}

        if m := re.fullmatch(r"/orgs/([^/]+)/repos", path):
            org = m.group(1)
            page = int(request.url.params.get("page", 1))
            items = [r for r in self.repos.values() if r["owner"]["login"] == org]
            return httpx.Response(200, json=items if page == 1 else [])
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)", path):
            repo = self.repos.get(m.group(1))
            return httpx.Response(200, json=repo) if repo else httpx.Response(404, json={"message": "Not Found"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls", path):
            repo = m.group(1)
            prs = self.pulls.setdefault(repo, {})
            if method == "GET":
                head = request.url.params.get("head", "")
                branch = head.split(":", 1)[-1]
                return httpx.Response(200, json=[p for p in prs.values() if p["state"] == "open" and (not head or p["head"]["ref"] == branch)])
            number = len(prs) + 1
            prs[number] = {
                "number": number,
                "title": body["title"],
                "state": "open",
                "merged": False,
                "draft": body.get("draft", False),
                "mergeable": True,
                "mergeable_state": "clean",
                "html_url": f"https://github.com/{repo}/pull/{number}",
                "head": {"ref": body["head"], "sha": f"sha{number}"},
                "base": {"ref": body["base"]},
                "user": {"login": "shoal-bot"},
                "body": body.get("body"),
            }
            return httpx.Response(201, json=prs[number])
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", path):
            repo, number = m.group(1), int(m.group(2))
            pr = self.pulls[repo][number]
            if method == "PATCH":
                pr.update(body)
            return httpx.Response(200, json=pr)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)/reviews", path):
            return httpx.Response(200, json=self.reviews.get((m.group(1), int(m.group(2))), []))
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)/merge", path):
            repo, number = m.group(1), int(m.group(2))
            pr = self.pulls[repo][number]
            if body.get("sha") and body["sha"] != pr["head"]["sha"]:
                return httpx.Response(409, json={"message": "Head branch was modified. Review and try the merge again."})
            pr.update(state="closed", merged=True)
            self.merges.append((repo, number, body.get("merge_method", "merge")))
            return httpx.Response(200, json={"merged": True, "sha": "merged-sha", "message": "Pull Request successfully merged"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([^/]+)/check-runs", path):
            run = self.check_runs.get((m.group(1), m.group(2)), {"status": "completed", "conclusion": "success"})
            return httpx.Response(200, json={"total_count": 1, "check_runs": [run]})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([^/]+)/status", path):
            statuses = list(reversed(self.statuses.get((m.group(1), m.group(2)), [])))
            return httpx.Response(200, json={"state": "pending", "total_count": 0, "statuses": statuses})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/statuses/([^/]+)", path):
            self.statuses.setdefault((m.group(1), m.group(2)), []).append(body)
            return httpx.Response(201, json=body)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/collaborators/([^/]+)/permission", path):
            role = self.collaborators.get((m.group(1), m.group(2)))
            if role is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"permission": "write" if role == "maintain" else role, "role_name": role})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/assignees", path):
            self.assignees.setdefault((m.group(1), int(m.group(2))), []).extend(body["assignees"])
            return httpx.Response(201, json={})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)/requested_reviewers", path):
            self.review_requests.setdefault((m.group(1), int(m.group(2))), []).extend(body["reviewers"])
            return httpx.Response(201, json={})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues", path):
            issue = {"number": 100 + len(self.issues), **body}
            self.issues.append(issue)
            return httpx.Response(201, json={**issue, "html_url": f"https://github.com/{m.group(1)}/issues/{issue['number']}"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", path):
            comment = {"id": len(self.comments) + 1, "repo": m.group(1), "number": int(m.group(2)), "body": body["body"]}
            self.comments.append(comment)
            return httpx.Response(201, json={**comment, "html_url": f"https://github.com/{m.group(1)}/pull/{m.group(2)}#c{comment['id']}"})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/labels", path):
            key = (m.group(1), int(m.group(2)))
            if method == "POST":
                self.labels.setdefault(key, []).extend(body["labels"])
            return httpx.Response(200, json=[{"name": n} for n in self.labels.get(key, [])])
        if path == "/user":
            return httpx.Response(200, json={"login": "shoal-bot"})
        return httpx.Response(404, json={"message": f"fake github: no route {method} {path}"})


@dataclass
class FakeJira:
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: list[tuple[str, Any]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    workflow: dict[str, list[str]] = field(
        default_factory=lambda: {
            "Ready for AI": ["In Progress"],
            "In Progress": ["In Review", "To Do"],
            "To Do": ["Ready for AI"],
            "In Review": ["Done"],
        }
    )

    def add_issue(self, key: str, summary: str, description: str, status: str = "Ready for AI") -> None:
        self.issues[key] = {
            "id": str(10000 + len(self.issues)),
            "key": key,
            "fields": {
                "summary": summary,
                "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]},
                "status": {"name": status},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
                "labels": [],
                "project": {"key": key.split("-")[0]},
            },
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)", path):
            return httpx.Response(200, json=self.issues[m.group(1)])
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)/transitions", path):
            issue = self.issues[m.group(1)]
            status = issue["fields"]["status"]["name"]
            if method == "GET":
                targets = self.workflow.get(status, [])
                return httpx.Response(200, json={"transitions": [{"id": str(i), "name": f"Move to {t}", "to": {"name": t}} for i, t in enumerate(targets)]})
            target = self.workflow[status][int(body["transition"]["id"])]
            issue["fields"]["status"]["name"] = target
            return httpx.Response(204)
        if m := re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)/comment", path):
            self.comments.append((m.group(1), body["body"]))
            return httpx.Response(201, json={"id": str(len(self.comments))})
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"displayName": "Automation"})
        return httpx.Response(404, json={"errorMessages": [f"fake jira: no route {method} {path}"]})


class Router:
    """Routes requests by host to the fakes."""

    def __init__(self, github: FakeGitHub, jira: FakeJira):
        self.github, self.jira = github, jira

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return self.github.handle(request)
        if request.url.host.endswith("atlassian.net"):
            return self.jira.handle(request)
        return httpx.Response(502, json={"message": f"unexpected host {request.url.host}"})


def comment_text(adf: Any) -> str:
    from shoal.integrations.jira import adf_to_text

    return adf_to_text(adf) if isinstance(adf, dict) else str(adf)
