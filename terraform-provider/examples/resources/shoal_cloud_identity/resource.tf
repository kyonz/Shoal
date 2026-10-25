resource "shoal_cloud_identity" "aws_prod" {
  name = "aws-prod"
  spec = {
    provider = "aws"
    trust    = "web_identity"
    aws = {
      role_arn = "arn:aws:iam::123456789012:role/shoal-deploy"
      region   = "eu-west-1"
      duration = "1h"
    }
    allow = {
      workflows = ["deploy-*"]
      agents    = ["infra"]
    }
  }
}
