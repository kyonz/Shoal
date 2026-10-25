# Skill sources (marketplaces)

Skills are `SKILL.md` files plus the scripts and references next to them. You can
write `Skill` resources by hand, or bring them in from a **skill source**: a git
repository in the Agent Skills layout, a skill registry, or any marketplace with
a plugin. Importing turns a skill from a source into an ordinary `Skill` resource
in a namespace, so agents use it exactly like a hand-written one.

```
SkillSource (tenant-wide, admins)      Skill (per namespace)
  acme-skills  ── browse / import ──▶   pdf      labels: shoal.dev/skill-source=acme-skills
  git, ref v2.3                                          shoal.dev/skill-source-skill=skills/pdf
                                                         shoal.dev/skill-version=1.1.0
                                                         shoal.dev/skill-ref=<commit sha>
```

## Adding a source

Sources are tenant-wide, like gateways: they live in the `shared` namespace and
are managed by tenant admins (`skill-sources:manage`; `skill-sources:read` to
view). Credentials are never written into the source. Set them as secrets in
`shared` and map config fields to them under `secrets`; a config key that looks
like a credential (`token`, `api_key`, ...) is rejected.

```yaml
apiVersion: shoal.dev/v1
kind: SkillSource
metadata: {name: acme-skills}
spec:
  source: git
  title: Acme skills
  config:
    url: https://github.com/acme/agent-skills
    ref: v2.3.0           # branch, tag or commit the source is pinned to
    path: skills          # optional subdirectory
  secrets:
    token: acme-skills-token   # secret in `shared`, for a private repository
  namespaces: [platform, payments]   # who may browse and import (empty: everyone)
```

```bash
shoal secrets set acme-skills-token -n shared
shoal apply -f skill-source.yaml
```

`POST /v1/skill-sources/{name}/test` checks that the source can be reached (for git: the commit and how many skills it found).

Terraform: `shoal_skill_source` (see the provider docs). The plugins installed on
the server and their config schemas are at `GET /v1/skill-source-types`.

### Built-in sources

| `source` | Config | What it reads |
| --- | --- | --- |
| `git` | `url`, `ref` (default `main`), `path`, `refresh_seconds`; secret `token` (+ `username`) | Every directory with a `SKILL.md` is a skill, with the directory (relative to `path`) as its id. That covers single-skill repositories, `skills/<name>/` collections and plugin marketplaces (`plugins/<p>/skills/<name>/`). Branches are re-fetched at most every `refresh_seconds` (an update check always fetches); imports record the exact commit. A skill without a `version` is versioned by commit, and an update check only flags it when its own files changed. |
| `registry` | `url`, `headers`; secret `token` | Any service speaking `shoal.skill-source/v1` (below): a marketplace, or another source run as its own service. |

A skill's version is `version` (or `metadata.version`) from its frontmatter, else
the short commit. Text files up to 2 MB per skill are imported; binary files are
listed as skipped.

The git source only accepts `https://` and ssh URLs. To read repositories from the
server's own disk (development, air-gapped mirrors), the operator sets
`SHOAL_SKILL_SOURCE_ALLOW_LOCAL=true`. Checkouts are cached under
`SHOAL_SKILL_SOURCE_CACHE_DIR` (default: the system temp directory).

## Browsing and importing

Anyone who can read skills in a namespace can browse the sources visible to it.
Importing needs `skills:import` and `skills:write` there (the `editor` role has both).

| | API | CLI |
| --- | --- | --- |
| Sources this namespace can see | `GET /v1/namespaces/{ns}/skill-sources` | `shoal skills sources` |
| List or search a source | `GET …/skill-sources/{source}/skills?q=` | `shoal skills browse SOURCE -q pdf` |
| One skill (SKILL.md, files, version) | `GET …/skill-sources/{source}/skills/{id}?version=` | `shoal skills show SOURCE ID` |
| Import (or preview with `dry_run`) | `POST /v1/namespaces/{ns}/skill-imports` | `shoal skills import SOURCE ID [--name] [--version] [--dry-run]` |
| Imported skills, with update check | `GET /v1/namespaces/{ns}/skill-imports?check=true` | `shoal skills imports --check` |
| Upgrade (diff first) | `POST …/skill-imports/{name}/upgrade` | `shoal skills upgrade NAME [--version]` |
| Roll back | `POST /v1/namespaces/{ns}/skills/{name}/rollback` | `shoal skills rollback NAME VERSION` |

The resource name defaults to the skill's name; pass `name` to import the same
skill twice or to avoid a clash. An import never overwrites a skill that was
not imported from the same source and skill id (409). `version` pins the import
to a tag, commit or registry version instead of what the source is pinned to.

Every import and upgrade returns the diff from the namespace's current skill
(`changes`, as JSON-pointer `add`/`remove`/`replace` entries), and with `dry_run`
nothing is written. An applied import is a new version of the `Skill` resource
with a change note such as `Import acme-skills/skills/pdf 1.1.0 (3f2a…)`, so the
usual version history, diff and rollback apply. Rolling back restores the labels
too, so the skill again reports the version it came from. Imports and upgrades
are audited as `skill.imported` and `skill.upgraded`.

## Writing a source plugin

A source implements `list` (and optionally `search`; the default filters the
listing) and `fetch`, with the same SDK as integrations:

```python
from shoal.sdk import BaseModel, SecretStr, SkillBundle, SkillPage, SkillSource, SkillSourceContext, SkillSummary


class AcmeConfig(BaseModel):
    url: str
    api_key: SecretStr


class AcmeSkills(SkillSource):
    key = "acme"
    title = "Acme marketplace"
    config_model = AcmeConfig

    async def list(self, ctx: SkillSourceContext, cursor: str | None = None, limit: int = 50) -> SkillPage:
        async with ctx.http(base_url=ctx.config.url) as http:
            data = (await http.get("/skills", params={"page": cursor})).json()
        return SkillPage(items=[SkillSummary(id=s["slug"], name=s["name"], description=s["summary"],
                                             version=s["latest"]) for s in data["skills"]],
                         next_cursor=data.get("next"))

    async def fetch(self, ctx: SkillSourceContext, skill_id: str, version: str | None = None) -> SkillBundle:
        ...  # return SKILL.md body (parse_skill_md helps), files and version
```

Ship it with an entry point and the tenant can add `source: acme` sources:

```toml
[project.entry-points."shoal.skill_sources"]
acme = "shoal_acme:AcmeSkills"
```

Fields typed `SecretStr` are listed as `secret_fields` in the catalogue and come
from secrets in `shared`.

### Running a source as a service

`shoal.sdk.skill_server` serves any source over `shoal.skill-source/v1`, so it can
run on its own (its own release cadence, network zone and credentials) and be
added to Shoal as a `registry` source:

```bash
SHOAL_SKILL_SOURCE_TOKEN=... python -m shoal.sdk.skill_server shoal_acme:AcmeSkills --config acme.json --port 8080
```

### Protocol: `shoal.skill-source/v1`

Three read-only calls, with `Authorization: Bearer <token>` when the source sets one:

| Call | Response |
| --- | --- |
| `GET /manifest` | `{"protocol": "shoal.skill-source/v1", "key", "title", "description"}` |
| `GET /skills?q=&cursor=&limit=` | `{"items": [{"id", "name", "description", "version", "tags", "homepage"}], "next_cursor"}` |
| `GET /skills/{id}?version=` | `{"id", "name", "description", "version", "ref", "instructions", "files": {path: text}, "metadata", "skipped"}` |

Ids may contain `/`. Errors are `{"error": {"message", "retryable"}}` with a 4xx or
5xx status; a 404 from `GET /skills/{id}` means the skill or version does not exist.
