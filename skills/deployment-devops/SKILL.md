---
name: deployment-devops
description: Deployment & DevOps — Docker, CI/CD, monitoring, logging, configuration
triggers:
- docker
- deploy
- ci/cd
- monitor
- prometheus
- grafana
version: 1.0.0
schema_version: 2
skill_id: deployment-devops
task_families:
- deployment
- maintenance
capabilities:
- configuration
- observability
aliases:
- devops
- docker
- cicd
- ci-cd
- deploy
- containerize
stack:
  languages: []
  frameworks:
  - docker
  - github-actions
  - prometheus
  - grafana
  independent: true
preconditions:
- a service that must be containerized or deployed
- access to the CI/CD pipeline configuration
- a target runtime environment with health-check support
required_tools:
- docker
- docker compose
- git
- github-actions
exclusions:
- designing application business logic
- Kubernetes manifest and cluster work (use kubernetes-ops)
- database query or schema tuning
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Deployment & DevOps

## Core Skills

| Skill | Practice |
|---|---|
| Docker | Containerize Machina + PostgreSQL |
| CI/CD | GitHub Actions: lint → test → build → deploy |
| Monitoring | Prometheus metrics + Grafana dashboards |
| Logging | Structured logging with correlation IDs |
| Configuration | YAML config with env var overrides |

## Key Resources
- [Docker docs](https://docs.docker.com/)
- [GitHub Actions](https://docs.github.com/en/actions)
- [Prometheus](https://prometheus.io/)
- [Grafana](https://grafana.com/)

## Practice Project
Dockerize Machina: docker compose up starts PostgreSQL + pgvector + Machina. Add health check endpoint.
