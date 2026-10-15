---
name: incident-response
description: "Incident response — severity levels, on-call triage, mitigation before diagnosis, communication cadence, blameless postmortems, runbooks"
triggers:
  - "incident"
  - "on-call"
  - "postmortem"
  - "runbook"
  - "sev"
  - "outage"
  - "escalation"
version: 1.0.0
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
