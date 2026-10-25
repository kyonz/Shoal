# SQS dispatch queues for Shoal

Creates standard SQS queues for Shoal's [dispatch queue](../../../docs/dispatch-queues.md)
(one for the agent lane too, by default), an IAM policy with the four queue actions Shoal
uses, and optionally attaches it to the roles your API servers and workers run as.

```hcl
module "shoal_dispatch" {
  source     = "github.com/kyonz/Shoal//deploy/terraform/sqs-dispatch"
  role_names = [aws_iam_role.shoal.name] # e.g. the IRSA role of the chart's service account
}

# Feed the chart:
#   dispatch = module.shoal_dispatch.helm_values
# or the environment:
#   module.shoal_dispatch.environment
```

| Input | Default | |
|---|---|---|
| `name` | `shoal-dispatch` | Main queue name; the agent lane's queue adds `-agent` |
| `agent_queue` | `true` | Give the agent lane its own queue |
| `role_names` | `[]` | Roles to attach the policy to |
| `message_retention_seconds` | `86400` | Long timers are re-sent every 15 minutes, so a day is plenty |
| `dead_letter_queue` | `false` | Add dead-letter queues (the database sweep still finds their runs) |
| `max_receive_count` | `1000` | Receives before a message is dead-lettered; keep it high, capped tenants' messages come back often |
| `kms_key_id` | `null` | Customer-managed key (default: SQS-managed encryption) |
| `tags` | `{}` | |

Outputs: `queue_url`, `lane_queue_urls`, `policy_arn`, `environment` and `helm_values`.
