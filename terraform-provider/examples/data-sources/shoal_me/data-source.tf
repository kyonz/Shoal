data "shoal_me" "current" {}

output "acting_as" {
  value = data.shoal_me.current.actor
}
