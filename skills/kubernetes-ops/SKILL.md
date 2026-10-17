---
name: kubernetes-ops
description: Kubernetes operations — deployments, probes, resource requests and limits, autoscaling, namespaces,
  rollouts, operators, troubleshooting
triggers:
- kubernetes
- k8s
- kubectl
- helm
- pod
- namespace
- hpa
- operator
version: 1.0.0
schema_version: 2
skill_id: kubernetes-ops
task_families:
- deployment
- debugging
- maintenance
capabilities:
- configuration
- observability
- performance
aliases:
- k8s
- kubectl
- helm
- cluster-ops
- kubernetes
stack:
  languages:
  - yaml
  frameworks:
  - kubernetes
  - helm
  independent: false
preconditions:
- access to a Kubernetes cluster and namespace
- kubectl or Helm installed locally
- workload manifests or a chart to deploy
required_tools:
- kubectl
- helm
exclusions:
- local Docker Compose development
- cloud account and network provisioning (use terraform-iac)
- authoring application source code
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Kubernetes Operations

## Core Skills

| Skill | Practice |
|---|---|
| Workloads | Deployments, StatefulSets and Jobs with rollout strategy |
| Probes | Liveness, readiness and startup probes tuned to real startup time |
| Resources | Requests and limits per container; avoid unbounded burst |
| Autoscaling | Horizontal pod autoscaler on a meaningful signal |
| Namespaces | Isolation, quotas and network policy per environment |
| Config & secrets | ConfigMaps and Secret refs, never baked into images |
| Rollouts | Rolling update and rollback with observed health |
| Troubleshooting | Describe, events, logs, exec — in that order |

## Pitfalls
- A liveness probe that fails under load triggers crash loops, not recovery.
- Missing requests cause the scheduler to overcommit and evict pods.
- `latest` image tags make rollbacks ambiguous; pin by digest.
- One namespace for everything erases the isolation the quotas rely on.

## Practice Project
Deploy a stateless service with pinned image, requests/limits, all three probes, an HPA, and a ConfigMap; then practice a rolling update and a rollback while watching events and logs.

## Key Resources
- [Kubernetes docs](https://kubernetes.io/docs/)
- [Helm](https://helm.sh/docs/)
- [kubectl cheat sheet](https://kubernetes.io/docs/reference/kubectl/quick-reference/)
