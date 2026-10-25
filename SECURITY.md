# Security policy

Shoal runs AI agents that execute code and act in external systems on your behalf, so we take
security reports seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/kyonz/ai-workflow-manager/security/advisories/new).

Include as much as you can:

- the affected component (engine, API, worker, CLI, web UI, Terraform provider, Helm chart, image);
- the version or commit;
- steps to reproduce, a proof of concept, or the configuration needed;
- the impact you expect, for example tenant isolation bypass or secret disclosure.

## What to expect

- We acknowledge reports within **3 business days**.
- We send an initial assessment within **10 business days**.
- We keep you updated while we fix it, agree a disclosure date with you, and credit you in the
  advisory unless you prefer otherwise.

## Supported versions

Shoal is in alpha. Security fixes are made on `main` and released in the next version; we do not
backport to older releases yet.

## Scope

Of particular interest:

- crossing tenant or namespace boundaries, or bypassing RBAC;
- reading secret values through the API, logs, run events or exports;
- escaping a sandbox profile's isolation in a way Shoal is responsible for;
- forging webhooks, A2A or remote integration requests, or tokens issued to agents;
- injection through workflow expressions.

Out of scope: the `local` sandbox backend, which documents that it has no isolation; findings that
need an already-compromised administrator account; and denial of service through unbounded load on
a self-hosted instance without quotas configured.
