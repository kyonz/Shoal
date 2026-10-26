variable "name" {
  description = "Name of the main queue; the agent lane's queue gets an -agent suffix."
  type        = string
  default     = "shoal-dispatch"
}

variable "agent_queue" {
  description = "Give the agent lane its own queue (recommended with a separate agent worker pool)."
  type        = bool
  default     = true
}

variable "role_names" {
  description = "IAM roles of Shoal's API servers and workers (e.g. the IRSA role) to attach the policy to."
  type        = list(string)
  default     = []
}

variable "message_retention_seconds" {
  description = "How long an undelivered wake-up is kept. Long timers are sent again every 15 minutes, so a day is plenty."
  type        = number
  default     = 86400
}

variable "dead_letter_queue" {
  description = "Move messages received max_receive_count times to a dead-letter queue (the database sweep still finds their runs)."
  type        = bool
  default     = false
}

variable "max_receive_count" {
  description = "Receives before a message goes to the dead-letter queue."
  type        = number
  default     = 1000
}

variable "kms_key_id" {
  description = "Customer-managed KMS key for the queues (default: SQS-managed encryption)."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for every resource."
  type        = map(string)
  default     = {}
}
