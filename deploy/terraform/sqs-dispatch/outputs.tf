output "queue_url" {
  description = "SHOAL_DISPATCH_SQS_QUEUE_URL"
  value       = aws_sqs_queue.dispatch["standard"].url
}

output "lane_queue_urls" {
  description = "SHOAL_DISPATCH_SQS_LANE_QUEUE_URLS (as a map; jsonencode it for the environment)"
  value       = { for lane, q in aws_sqs_queue.dispatch : lane => q.url if lane != "standard" }
}

output "policy_arn" {
  description = "IAM policy granting the queue actions Shoal needs."
  value       = aws_iam_policy.dispatch.arn
}

output "environment" {
  description = "Environment variables for Shoal's API servers and workers."
  value = merge(
    {
      SHOAL_DISPATCH_QUEUE         = "sqs"
      SHOAL_DISPATCH_SQS_QUEUE_URL = aws_sqs_queue.dispatch["standard"].url
    },
    var.agent_queue ? {
      SHOAL_DISPATCH_SQS_LANE_QUEUE_URLS = jsonencode({ agent = aws_sqs_queue.dispatch["agent"].url })
    } : {},
  )
}

output "helm_values" {
  description = "The chart's dispatch values."
  value = {
    queue = "sqs"
    sqs = {
      queueUrl      = aws_sqs_queue.dispatch["standard"].url
      agentQueueUrl = var.agent_queue ? aws_sqs_queue.dispatch["agent"].url : ""
    }
  }
}
