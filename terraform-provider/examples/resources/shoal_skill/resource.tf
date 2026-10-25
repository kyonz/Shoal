resource "shoal_skill" "python_testing" {
  name = "python-testing"
  spec = {
    description  = "How we write and run Python tests"
    instructions = file("${path.module}/skills/python-testing/SKILL.md")
    files = {
      "conftest-template.py" = file("${path.module}/skills/python-testing/conftest.py")
    }
  }
}
