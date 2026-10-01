---
name: deployment-devops
description: "Deployment & DevOps — Docker, CI/CD, monitoring, logging, configuration"
triggers:
  - "docker"
  - "deploy"
  - "ci/cd"
  - "monitor"
  - "prometheus"
  - "grafana"
version: 1.0.0
---

# Deployment & DevOps

## Core Skills

| Skill | Practice |
|---|---|
| Docker | Containerize AgentHarness + PostgreSQL |
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
Dockerize AgentHarness: docker compose up starts PostgreSQL + pgvector + AgentHarness. Add health check endpoint.
