---
name: terraform-iac
description: Terraform & infrastructure as code — modules, state management, plan review, drift detection,
  workspaces, remote backends, providers
triggers:
- terraform
- infrastructure as code
- iac
- hcl
- terragrunt
- pulumi
- state file
version: 1.0.0
schema_version: 2
skill_id: terraform-iac
task_families:
- deployment
- maintenance
capabilities:
- configuration
- migration
aliases:
- terraform
- tf
- iac
- infra
- opentofu
- hcl
stack:
  languages:
  - hcl
  frameworks:
  - terraform
  - opentofu
  - terragrunt
  independent: false
preconditions:
- cloud or platform credentials with least privilege
- a locked remote state backend
- a plan diff that can be reviewed before apply
required_tools:
- terraform
- git
exclusions:
- application code changes
- one-off manual console changes outside code
- Kubernetes workload manifests (use kubernetes-ops)
sections:
- id: core-skills
  heading: Core Skills
  stage: plan
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: implement
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
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
