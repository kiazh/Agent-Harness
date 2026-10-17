---
name: research-writing
description: Research & paper writing — paper reading, experimental design, statistical analysis, LaTeX,
  reproducibility, citation verification
triggers:
- paper
- research
- latex
- experiment
- citation
- reproducibility
version: 1.0.0
schema_version: 2
skill_id: research-writing
task_families:
- research
- documentation
capabilities: []
aliases:
- paper-writing
- academic-writing
- latex
- research-paper
- citations
stack:
  languages:
  - latex
  - python
  frameworks: []
  independent: false
preconditions:
- A research question or experimental results
- A venue template and page budget
required_tools:
- shell
exclusions:
- Internal engineering docs (use docs-communication)
- Blog posts or marketing content
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: deliver
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Research & Paper Writing

## Core Skills

| Skill | Practice |
|---|---|
| Paper reading | Read 1 paper/week, write summary |
| Experimental design | Design ablation study for memory system |
| Statistical analysis | scipy for significance testing |
| LaTeX | Write workshop paper with ACL template |
| Reproducibility | Release code + data + seeds |
| Citation verification | Verify all benchmark scores against original papers |

## Key Resources
- [ACL LaTeX template](https://github.com/acl-org/acl-style-files)
- [Overleaf](https://www.overleaf.com/)
- [pytrec_eval](https://github.com/cvangysel/pytrec_eval)
- [ranx](https://github.com/amenranx/ranx)

## Practice Project
Write a 4-page workshop paper: "Token-Efficient Context Management with Reversible Eviction for LLM Agents." Include: intro, related work, method, evaluation, conclusion.
