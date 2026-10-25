# Amazon SQS queues for Shoal's dispatch queue (SHOAL_DISPATCH_QUEUE=sqs), and an
# IAM policy that lets Shoal's API servers and workers use them.
# See docs/dispatch-queues.md.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  lanes = var.agent_queue ? toset(["standard", "agent"]) : toset(["standard"])
}

resource "aws_sqs_queue" "dead_letter" {
  for_each                  = var.dead_letter_queue ? local.lanes : toset([])
  name                      = each.key == "standard" ? "${var.name}-dlq" : "${var.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = var.kms_key_id == null
  kms_master_key_id         = var.kms_key_id
  tags                      = var.tags
}

resource "aws_sqs_queue" "dispatch" {
  for_each = local.lanes
  # A standard queue: FIFO queues allow no per-message delay.
  name                       = each.key == "standard" ? var.name : "${var.name}-${each.key}"
  visibility_timeout_seconds = 30
  message_retention_seconds  = var.message_retention_seconds
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = var.kms_key_id == null
  kms_master_key_id          = var.kms_key_id
  # Runs over their tenant's concurrency cap go back on the queue until a slot frees up, so allow many receives.
  redrive_policy = var.dead_letter_queue ? jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter[each.key].arn
    maxReceiveCount     = var.max_receive_count
  }) : null
  tags = var.tags
}

data "aws_iam_policy_document" "dispatch" {
  statement {
    sid = "ShoalDispatchQueue"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [for q in aws_sqs_queue.dispatch : q.arn]
  }
  dynamic "statement" {
    for_each = var.kms_key_id == null ? [] : [var.kms_key_id]
    content {
      sid       = "ShoalDispatchQueueKms"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_policy" "dispatch" {
  name        = "${var.name}-dispatch"
  description = "Send and receive Shoal dispatch queue wake-ups"
  policy      = data.aws_iam_policy_document.dispatch.json
  tags        = var.tags
}

resource "aws_iam_role_policy_attachment" "dispatch" {
  for_each   = toset(var.role_names)
  role       = each.value
  policy_arn = aws_iam_policy.dispatch.arn
}
