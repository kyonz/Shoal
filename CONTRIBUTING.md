# Contributing to Shoal

Thanks for helping build Shoal. This guide covers how to propose a change, set up a development
environment and get a pull request merged.

## Ways to contribute

- **Report a bug** or **request a feature** with the [issue templates](https://github.com/kyonz/ai-workflow-manager/issues/new/choose).
- **Improve the docs.** Fixes to `docs/`, examples and READMEs are always welcome.
- **Write an integration** with the SDK or the remote protocol ([guide](docs/integrations.md)). Many
  integrations can live in their own repository; open an issue first if you want one in core.
- **Fix an issue.** Issues labelled `good first issue` are a good place to start. Comment on the
  issue so others know you are working on it.

For a large change (a new step type, a new plugin contract, an API change), open an issue to discuss
the design before writing code. [docs/architecture.md](docs/architecture.md) is the reference design
and should be updated alongside the code.

## Development setup

| Part | Needs | Directory |
|---|---|---|
| Engine, API, worker, CLI | Python 3.11+, [uv](https://docs.astral.sh/uv/) | `engine/` |
| Web UI | Node.js 20+ | `frontend/` |
| Terraform provider | Go (version in `terraform-provider/go.mod`), Terraform | `terraform-provider/` |
| Helm chart | Helm 3 | `deploy/helm/shoal/` |

### Engine

```sh
cd engine
uv sync --all-extras
uv run pytest -q                 # tests
uv run ruff check .              # lint
SHOAL_ADMIN_TOKEN=dev uv run shoal serve   # API + worker on 127.0.0.1:8080 with SQLite
```

The schema is managed with Alembic migrations in `engine/shoal/store/migrations/`;
the server applies them at start-up (`shoal db upgrade` does it by hand). After
changing `shoal/store/tables.py`, generate a migration with
`SHOAL_DATABASE_URL=… uv run shoal db revision -m "what changed"` against an
up-to-date database and review it; `tests/test_migrations.py` fails while the
models and migrations disagree.

Tests use SQLite by default. To run them against Postgres as well, point
`SHOAL_TEST_DATABASE_URL` at a scratch database (its `public` schema is reset
for every test), e.g.
`SHOAL_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/shoal_test scripts/check.sh engine`.

### Web UI

```sh
cd frontend
npm install
npm run dev                      # expects the engine on :8080
npm run lint && npm run typecheck && npm test && npm run build
```

### Terraform provider

```sh
cd terraform-provider
make build
go test ./...
make testacc                     # acceptance tests against a real engine started from ../engine
make docs                        # regenerate docs/ after changing a schema
```

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request focused on one change. Small PRs are reviewed faster.
3. Add or update tests. Bug fixes should come with a test that fails without the fix.
4. Update the docs and examples that your change affects, including generated Terraform docs.
5. Run the checks locally before you push: ruff and pytest for the engine, lint, type check, tests and build for the
   UI, `gofmt`, `go vet` and tests for the provider, and `helm lint` for the chart.
6. Fill in the pull request template, and link the issue it closes.

A maintainer will review your PR. We may ask for changes; once it is approved and green it will be
merged with a squash commit.

### Commit messages

Write the subject in the imperative mood ("Add Linear integration", not "Added ..."), keep it under
about 72 characters, and explain *why* in the body when it is not obvious.

### Compatibility

The HTTP API, the workflow schema (`apiVersion`) and the plugin entry points are public contracts.
Changes to them must be backwards compatible, or be called out as breaking in the PR description so
they can go into a major release.

## Developer Certificate of Origin

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE) and that you have the right to submit it. Please sign off each commit
(`git commit -s`) to certify the [Developer Certificate of Origin](https://developercertificate.org/).

## Code of Conduct

Everyone taking part in Shoal is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not report security vulnerabilities in public issues. See [SECURITY.md](SECURITY.md).
