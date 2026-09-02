terraform {
  required_version = ">= 1.10, < 2.0"

  # The selected environment supplies its own local state path at init.
  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.62.0"
    }
  }
}
