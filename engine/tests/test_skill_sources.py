from __future__ import annotations

import subprocess

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.sdk import SkillSourceError, parse_skill_md
from shoal.sdk.skill_server import create_app as create_skill_server
from shoal.skills.git import GitSkillSource

ADMIN = "test-admin-token"

PDF_V1 = """---
name: pdf
description: Read and fill PDF forms.
metadata:
  version: 1.0.0
  tags: [documents, forms]
---
# PDF

Use scripts/fill.py to fill forms.
"""


def git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo)},
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "skills-repo"
    (root / "skills" / "pdf" / "scripts").mkdir(parents=True)
    (root / "skills" / "pdf" / "SKILL.md").write_text(PDF_V1)
    (root / "skills" / "pdf" / "scripts" / "fill.py").write_text("print('fill')\n")
    (root / "skills" / "pdf" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    (root / "skills" / "docx").mkdir()
    (root / "skills" / "docx" / "SKILL.md").write_text("---\nname: docx\ndescription: Edit Word documents.\n---\nBody\n")
    (root / "README.md").write_text("not a skill\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "v1")
    git(root, "tag", "v1")
    return root


def commit_v2(root) -> str:
    (root / "skills" / "pdf" / "SKILL.md").write_text(PDF_V1.replace("1.0.0", "1.1.0").replace("fill forms.", "fill and sign forms."))
    git(root, "commit", "-qam", "v2")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
async def api(engine, tmp_path):
    engine.settings.admin_token = SecretStr(ADMIN)
    engine.settings.skill_source_allow_local = True
    engine.settings.skill_source_cache_dir = str(tmp_path / "skill-cache")
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme", "name": "Acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        assert r.status_code == 201, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield client


async def add_git_source(api, repo, name: str = "acme-skills", **spec) -> None:
    body = {"spec": {"source": "git", "title": "Acme skills", "config": {"url": str(repo), "ref": "main", "refresh_seconds": 0}, **spec}}
    r = await api.put(f"/v1/skill-sources/{name}", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["metadata"]["namespace"] == "shared"


def test_parse_skill_md():
    meta, body = parse_skill_md(PDF_V1)
    assert meta["name"] == "pdf" and body.startswith("# PDF")
    assert parse_skill_md("just text") == ({}, "just text")
    with pytest.raises(SkillSourceError):
        parse_skill_md("---\n- a list\n---\nbody")


async def test_source_config_rejects_clear_text_credentials(api, repo):
    body = {"spec": {"source": "git", "config": {"url": str(repo), "token": "ghp_secret"}}}
    r = await api.put("/v1/skill-sources/bad", json=body)
    assert r.status_code == 422, r.text
    types = (await api.get("/v1/skill-source-types")).json()
    assert {t["key"] for t in types} >= {"git", "registry"}
    assert "token" in next(t for t in types if t["key"] == "git")["secret_fields"]


async def test_browse_import_upgrade_and_roll_back(api, repo):
    perms = (await api.get("/v1/me/permissions")).json()
    assert {"skill-sources:read", "skill-sources:manage"} <= set(perms["tenant"])
    assert "skills:import" in perms["namespaces"]["default"]
    await add_git_source(api, repo)
    assert (await api.post("/v1/skill-sources/acme-skills/test")).json()["skills"] == 2
    assert [s["name"] for s in (await api.get("/v1/namespaces/default/skill-sources")).json()] == ["acme-skills"]

    page = (await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills")).json()
    assert [s["id"] for s in page["items"]] == ["skills/docx", "skills/pdf"]
    pdf = page["items"][1]
    assert pdf["version"] == "1.0.0" and pdf["tags"] == ["documents", "forms"] and pdf["installed"] == []
    found = (await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills", params={"q": "word"})).json()
    assert [s["id"] for s in found["items"]] == ["skills/docx"]

    detail = (await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills/skills/pdf")).json()
    assert detail["files"] == {"scripts/fill.py": "print('fill')\n"}
    assert detail["skipped"] == ["logo.png"] and len(detail["ref"]) == 40

    preview = (await api.post("/v1/namespaces/default/skill-imports", json={"source": "acme-skills", "skill": "skills/pdf", "dry_run": True})).json()
    assert preview["outcome"] == "created" and preview["dry_run"]
    assert (await api.get("/v1/namespaces/default/skills/pdf")).status_code == 404

    r = await api.post("/v1/namespaces/default/skill-imports", json={"source": "acme-skills", "skill": "skills/pdf"})
    assert r.status_code == 201, r.text
    assert r.json()["resource_version"] == 1
    skill = (await api.get("/v1/namespaces/default/skills/pdf")).json()
    labels = skill["metadata"]["labels"]
    assert labels["shoal.dev/skill-source"] == "acme-skills"
    assert labels["shoal.dev/skill-source-skill"] == "skills/pdf"
    assert labels["shoal.dev/skill-version"] == "1.0.0" and labels["shoal.dev/skill-ref"] == detail["ref"]
    assert skill["spec"]["instructions"].startswith("# PDF") and "scripts/fill.py" in skill["spec"]["files"]

    again = (await api.post("/v1/namespaces/default/skill-imports", json={"source": "acme-skills", "skill": "skills/pdf"})).json()
    assert again["outcome"] == "unchanged" and again["changes"] == []
    imports = (await api.get("/v1/namespaces/default/skill-imports", params={"check": True})).json()
    assert imports[0]["name"] == "pdf" and imports[0]["update_available"] is False

    v2 = commit_v2(repo)
    imports = (await api.get("/v1/namespaces/default/skill-imports", params={"check": True})).json()
    assert imports[0]["update_available"] is True and imports[0]["latest_version"] == "1.1.0"

    diff = (await api.post("/v1/namespaces/default/skill-imports/pdf/upgrade", json={"dry_run": True})).json()
    assert diff["outcome"] == "updated" and diff["previous_version"] == "1.0.0" and diff["version"] == "1.1.0"
    paths = {c["path"] for c in diff["changes"]}
    assert "/spec/instructions" in paths and "/metadata/labels/shoal.dev~1skill-version" in paths

    upgraded = (await api.post("/v1/namespaces/default/skill-imports/pdf/upgrade", json={})).json()
    assert upgraded["resource_version"] == 2 and upgraded["ref"] == v2
    assert "sign" in (await api.get("/v1/namespaces/default/skills/pdf")).json()["spec"]["instructions"]
    versions = (await api.get("/v1/namespaces/default/skills/pdf/versions")).json()
    assert versions[0]["message"].startswith("Import acme-skills/skills/pdf 1.1.0")

    # Roll back through ordinary versioning: the labels go back with the content.
    r = await api.post("/v1/namespaces/default/skills/pdf/rollback", json={"version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["labels"]["shoal.dev/skill-version"] == "1.0.0"

    # Pin an import to an older ref.
    pinned = (await api.post("/v1/namespaces/default/skill-imports",
                             json={"source": "acme-skills", "skill": "skills/docx", "name": "word", "version": "v1"})).json()
    assert pinned["name"] == "word" and pinned["version"] == pinned["ref"][:12]
    listing = (await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills")).json()
    assert {i["id"]: [x["name"] for x in i["installed"]] for i in listing["items"]} == {"skills/docx": ["word"], "skills/pdf": ["pdf"]}

    audit = (await api.get("/v1/audit")).json()
    assert {"skill.imported", "skill.upgraded"} <= {e["action"] for e in audit["items"]}


async def test_import_refuses_to_overwrite_other_skills(api, repo):
    await add_git_source(api, repo)
    own = {"spec": {"description": "Hand written", "instructions": "mine"}}
    assert (await api.put("/v1/namespaces/default/skills/pdf", json=own)).status_code == 201
    r = await api.post("/v1/namespaces/default/skill-imports", json={"source": "acme-skills", "skill": "skills/pdf"})
    assert r.status_code == 409, r.text
    r = await api.post("/v1/namespaces/default/skill-imports", json={"source": "acme-skills", "skill": "skills/nope"})
    assert r.status_code == 404, r.text
    r = await api.post("/v1/namespaces/default/skill-imports/pdf/upgrade", json={})
    assert r.status_code == 422, r.text


async def test_sources_are_limited_to_their_namespaces_and_permissions(api, repo):
    await add_git_source(api, repo, namespaces=["team-a"])
    assert (await api.put("/v1/namespaces/team-a", json={})).status_code == 201
    assert (await api.get("/v1/namespaces/default/skill-sources")).json() == []
    r = await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills")
    assert r.status_code == 404
    assert (await api.get("/v1/namespaces/team-a/skill-sources/acme-skills/skills")).status_code == 200

    sa = await api.put("/v1/service-accounts/viewer", json={})
    await api.post("/v1/role-bindings", json={"role": "viewer", "subject_kind": "user", "subject_id": sa.json()["id"], "namespace": None})
    token = (await api.post("/v1/api-keys", json={"name": "k", "service_account": "viewer"})).json()["token"]
    viewer = {"Authorization": f"Bearer {token}"}
    assert (await api.get("/v1/namespaces/team-a/skill-sources/acme-skills/skills", headers=viewer)).status_code == 200
    r = await api.post("/v1/namespaces/team-a/skill-imports", json={"source": "acme-skills", "skill": "skills/pdf"}, headers=viewer)
    assert r.status_code == 403
    r = await api.put("/v1/skill-sources/other", json={"spec": {"source": "git", "config": {"url": str(repo)}}}, headers=viewer)
    assert r.status_code == 403


async def test_git_sources_need_a_remote_url_unless_local_is_allowed(api, engine, repo):
    engine.settings.skill_source_allow_local = False
    await add_git_source(api, repo)
    r = await api.get("/v1/namespaces/default/skill-sources/acme-skills/skills")
    assert r.status_code == 422 and "https" in r.json()["detail"]


async def test_registry_source_against_a_served_skill_source(api, engine, repo):
    """A skill source served over shoal.skill-source/v1, imported through the built-in registry source."""
    server = create_skill_server(GitSkillSource, {"url": str(repo), "ref": "main"}, token="reg-token", allow_local=True)
    engine.http_transport = httpx.ASGITransport(app=server)
    r = await api.put("/v1/namespaces/shared/secrets/registry-token", json={"value": "reg-token"})
    assert r.status_code == 201, r.text
    body = {"spec": {"source": "registry", "config": {"url": "http://registry.test"}, "secrets": {"token": "registry-token"}}}
    assert (await api.put("/v1/skill-sources/remote", json=body)).status_code == 201
    assert (await api.post("/v1/skill-sources/remote/test")).json()["ok"] is True

    found = (await api.get("/v1/namespaces/default/skill-sources/remote/skills", params={"q": "pdf"})).json()
    assert [s["id"] for s in found["items"]] == ["skills/pdf"]
    r = await api.post("/v1/namespaces/default/skill-imports", json={"source": "remote", "skill": "skills/pdf"})
    assert r.status_code == 201, r.text
    assert r.json()["version"] == "1.0.0" and len(r.json()["ref"]) == 40

    # A wrong token is the registry's 401, surfaced as an upstream error.
    assert (await api.put("/v1/namespaces/shared/secrets/registry-token", json={"value": "nope"})).status_code == 200
    r = await api.get("/v1/namespaces/default/skill-sources/remote/skills")
    assert r.status_code == 502 and r.json()["code"] == "skill_source_error"
