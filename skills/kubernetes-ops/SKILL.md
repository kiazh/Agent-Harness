---
name: kubernetes-ops
description: "Kubernetes operations — deployments, probes, resource requests and limits, autoscaling, namespaces, rollouts, operators, troubleshooting"
triggers:
  - "kubernetes"
  - "k8s"
  - "kubectl"
  - "helm"
  - "pod"
  - "namespace"
  - "hpa"
  - "operator"
version: 1.0.0
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
