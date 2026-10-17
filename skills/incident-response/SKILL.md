---
name: incident-response
description: Incident response — severity levels, on-call triage, mitigation before diagnosis, communication
  cadence, blameless postmortems, runbooks
triggers:
- incident
- on-call
- postmortem
- runbook
- sev
- outage
- escalation
version: 1.0.0
schema_version: 2
skill_id: incident-response
task_families:
- debugging
- maintenance
capabilities:
- error-handling
- observability
aliases:
- incident
- oncall
- on-call
- postmortem
- outage
- sev
- escalation
stack:
  languages: []
  frameworks: []
  independent: true
preconditions:
- an active or suspected production incident
- an on-call roster with an escalation policy
- access to a comms channel and status page
required_tools:
- pagerduty
exclusions:
- writing feature code
- long-term reliability redesign work
- pre-incident capacity planning
sections:
- id: core-skills
  heading: Core Skills
  stage: debug
  requires_sections: []
- id: pitfalls
  heading: Pitfalls
  stage: review
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: deliver
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Incident Response

## Core Skills

| Skill | Practice |
|---|---|
| Severity levels | Clear SEV definitions tied to user impact and blast radius |
| Triage | Confirm impact, assign an incident commander, open a channel |
| Mitigate first | Stop the bleeding (rollback, flag off, scale) before root cause |
| Communication | Fixed cadence updates to stakeholders and status page |
| Roles | Commander, communications lead, scribe, subject experts |
| Escalation | Documented path and timeouts when no responder answers |
| Runbooks | Executable steps per alert, tested before you need them |
| Postmortem | Blameless timeline, contributing factors, tracked actions |

## Pitfalls
- Debugging the root cause while the site is down delays mitigation.
- Silent incidents erode trust; update stakeholders on a clock.
- Blame kills the honesty a useful postmortem depends on.
- Action items without owners and dates never land.

## Practice Project
Run a mock incident from alert to postmortem: declare a severity, mitigate with a rollback, post scheduled updates, then write a blameless timeline with contributing factors and owned follow-up actions.

## Key Resources
- [Google SRE — Managing Incidents](https://sre.google/sre-book/managing-incidents/)
- [PagerDuty Incident Response](https://response.pagerduty.com/)
- [Etsy — Debriefing Facilitation Guide](https://github.com/etsy/DebriefingFacilitationGuide)
