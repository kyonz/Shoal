terraform {
  required_providers {
    shoal = {
      source = "kyonz/shoal"
    }
  }
}

# Credentials usually come from SHOAL_ENDPOINT and SHOAL_API_KEY.
provider "shoal" {
  endpoint  = "https://shoal.example.com"
  namespace = "payments" # default namespace for namespaced resources
}
