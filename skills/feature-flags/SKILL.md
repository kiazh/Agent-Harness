---
name: feature-flags
description: Feature flags & progressive delivery — kill switches, staged rollout, targeting rules, flag
  lifecycle, experimentation, safe defaults
triggers:
- feature flag
- feature flags
- progressive rollout
- staged rollout
- kill switch
- unleash
- targeting rule
version: 1.0.0
schema_version: 2
skill_id: feature-flags
task_families:
- deployment
- backend-api
capabilities:
- configuration
- observability
aliases:
- feature-flag
- flags
- progressive-delivery
- kill-switch
- rollout
- unleash
stack:
  languages: []
  frameworks:
  - openfeature
  - unleash
  - launchdarkly
  independent: true
preconditions:
- a flag service or SDK available to the service
- a release path that can read flag state at runtime
- a primary metric to judge the rollout
required_tools: []
exclusions:
- permanent application configuration values
- statistical experiment analysis (use data-analysis)
- authentication or authorization policy decisions
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

# Feature Flags & Progressive Delivery

## Core Skills

| Skill | Practice |
|---|---|
| Kill switch | Wrap a risky path so it can be disabled without a deploy |
| Staged rollout | Ramp from internal, to 1%, 10%, 50%, 100% |
| Targeting rules | Segment by user id, tenant, region, or attribute |
| Safe defaults | Fail closed when the flag service is unreachable |
| Flag lifecycle | Short-lived release flags; delete after full rollout |
| Experimentation | Split traffic and measure a single primary metric |
| Observability | Tag metrics and logs with the evaluated variant |

## Pitfalls
- Long-lived flags become hidden configuration; expire and remove them.
- Flags evaluated per request without caching add latency and load.
- Changing a default value is a deploy; treat it as a release decision.
- Nested flags multiply untested combinations; keep the matrix small.

## Practice Project
Add one release flag to the service: default off, ramp by percentage, expose the variant in telemetry, kill-switch the feature, then delete the flag once rollout completes.

## Key Resources
- [OpenFeature](https://openfeature.dev/)
- [Unleash](https://www.getunleash.io/)
- [LaunchDarkly — flag lifecycle](https://launchdarkly.com/blog/)
