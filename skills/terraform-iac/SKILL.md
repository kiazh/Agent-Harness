---
name: terraform-iac
description: "Terraform & infrastructure as code — modules, state management, plan review, drift detection, workspaces, remote backends, providers"
triggers:
  - "terraform"
  - "infrastructure as code"
  - "iac"
  - "hcl"
  - "terragrunt"
  - "pulumi"
  - "state file"
version: 1.0.0
---

# Terraform & Infrastructure as Code

## Core Skills

| Skill | Practice |
|---|---|
| Modules | Reusable, versioned modules with clear inputs and outputs |
| Remote state | Locked backend (S3 + DynamoDB or equivalent), never local-only |
| Plan review | Read every plan diff before apply; no blind applies |
| Drift detection | Scheduled plan to catch manual console changes |
| Workspaces | Separate state per environment, not one shared root |
| Variables | Typed inputs with validation and safe defaults |
| Providers | Pin provider versions; upgrade deliberately |

## Pitfalls
- Local state loses locks and invites concurrent corruption.
- `terraform apply -auto-approve` in CI without a reviewed plan skips the gate.
- Hard-coded credentials in `.tf` or state; use a secret store and encryption.
- Deleting a resource without `prevent_destroy` can destroy production data.

## Practice Project
Write a small module, back it with locked remote state, provision one environment, then simulate drift and correct it through a reviewed plan and apply.

## Key Resources
- [Terraform docs](https://developer.hashicorp.com/terraform/docs)
- [OpenTofu](https://opentofu.org/)
- [Terragrunt](https://terragrunt.gruntwork.io/)
