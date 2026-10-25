# Cloud identities

A namespace can bring its own cloud role to Shoal and let chosen workflows and agents use it. When a granted step runs, Shoal gets short-lived credentials for the role and puts them in the step's sandbox. No long-lived cloud key is stored anywhere. AWS IAM roles are supported today. The provider is a plugin (`shoal.credentials`), so GCP and Azure can follow the same shape.

## Register a role

Namespace admins manage cloud identities (`cloud-identities:write`). Editors can see them and use them in granted workflows, but cannot register roles or change grants.

```yaml
apiVersion: shoal.dev/v1
kind: CloudIdentity
metadata: {name: aws-prod, namespace: platform}
spec:
  provider: aws
  trust: web_identity          # or ambient (see below)
  aws:
    role_arn: arn:aws:iam::123456789012:role/shoal-deploy
    region: eu-west-1
    duration: 1h               # 15m to 12h, within the role's maximum session duration
    session_policy:            # optional: narrow the role further for these sessions
      Version: "2012-10-17"
      Statement: [{Effect: Allow, Action: "s3:*", Resource: "arn:aws:s3:::deploy-artifacts/*"}]
  allow:
    workflows: ["deploy-*"]    # nothing can use the identity until a workflow is listed
    agents: ["infra"]          # optional: only agent steps running these agents
```

An identity in the `shared` namespace can serve several teams. Grants from another namespace must name it: `team-a/deploy` or `*/deploy`.

A grant follows the workflow's name, not its content. Anyone who can edit a granted workflow (or create one whose name matches a pattern) can make its steps do anything the role allows. Prefer exact names over patterns, keep granted workflows in a namespace whose editors you would trust with the role, and narrow the role with `session_policy` or `policy_arns`.

## Use it in a step

```yaml
agents:  # on an Agent
  cloud_identities: [aws-prod]
steps:   # or on a script step
  - id: plan
    type: script
    cloud_identities: [aws-prod]
    run: aws sts get-caller-identity && terraform plan
```

The sandbox gets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_DEFAULT_REGION` and `AWS_CREDENTIAL_EXPIRATION`, so the AWS CLI and SDKs work unchanged. The secret key and session token are redacted from logs, events and transcripts. Use one AWS identity per step.

## Trust: how the chain starts from where Shoal runs

The role's trust policy decides what may assume it. Open the identity in the UI and choose **Trust policy**, or call `GET /v1/namespaces/{ns}/cloud-identities/{name}/trust-policy`, to get the exact policy and setup commands.

- **`web_identity`** (default). Shoal is an OpenID Connect issuer. For each step it signs a five-minute ES256 token whose subject names this identity (`tenant:<tenant id>:namespace:<ns>:cloud-identity:<name>`), and calls `AssumeRoleWithWebIdentity`. Shoal needs no AWS credentials. In AWS you add Shoal's issuer once as an IAM OIDC provider, and the trust policy pins the audience and subject. AWS must reach `<issuer>/.well-known/openid-configuration` and `/.well-known/jwks.json` over public HTTPS. A deployment inside a private network can publish those two documents somewhere public (for example an S3 bucket) and set `SHOAL_OIDC_ISSUER` to that URL.
- **`ambient`**. Shoal uses the deployment's own AWS identity and calls `AssumeRole`. It looks for credentials the way the AWS SDKs do: environment variables, IRSA (`AWS_WEB_IDENTITY_TOKEN_FILE`), EKS Pod Identity or the ECS task role, then the EC2 instance role. The trust policy names the deployment role and requires an ExternalId that is unique to this identity, so another tenant on the same deployment cannot use the role. Operators turn this on with `SHOAL_AWS_AMBIENT=true` and set `SHOAL_AWS_DEPLOYMENT_ROLE_ARN` so the generated policies are complete. With the Helm chart, put the IRSA annotation on `serviceAccount.annotations`.

Federated tenants mint credentials on their own member deployment, with that deployment's issuer or role. Nothing passes through the hub.

## Attribution and audit

Every session records who and what it is for:

- **Source identity** is the person who started the run (their email or GitHub login). It shows in CloudTrail and stays with the session through role chaining. The trust policy allows `sts:SetSourceIdentity`.
- **Session tags** `shoal:tenant`, `shoal:namespace`, `shoal:workflow`, `shoal:run`, `shoal:step`, `shoal:agent` and `shoal:initiator` can drive ABAC conditions in your IAM policies. They are transitive. The trust policy allows `sts:TagSession`.
- **Session name** is `shoal-<run id>-<step id>`.
- Shoal writes `cloud_identity.issued`, `cloud_identity.denied` or `cloud_identity.failed` to the run's events, and the matching `cloud-identity.*` entry to the audit log with the role, session name, source identity and access key id, so you can match a CloudTrail entry to a run.

## Settings

| Setting | Meaning |
|---|---|
| `SHOAL_OIDC_ISSUER` | Issuer URL in tokens (default `SHOAL_PUBLIC_URL`). |
| `SHOAL_IDENTITY_SIGNING_KEY` | PEM EC P-256 key for tokens. By default it is derived from `SHOAL_SECRET_KEY`, so all replicas agree. Rotating either one rotates the published key. |
| `SHOAL_AWS_AMBIENT` | Allow `trust: ambient`. |
| `SHOAL_AWS_DEPLOYMENT_ROLE_ARN` | The deployment's own role, used in generated trust policies. |
| `SHOAL_AWS_STS_ENDPOINT` | STS endpoint override (VPC endpoint, FIPS). |
| `SHOAL_AWS_AUDIENCE` | Token audience (default `sts.amazonaws.com`). |
