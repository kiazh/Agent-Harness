---
name: docs-communication
description: Documentation & communication — MkDocs, API docs, architecture docs, tutorials, README
triggers:
- docs
- documentation
- mkdocs
- readme
- tutorial
- adr
version: 1.0.0
schema_version: 2
skill_id: docs-communication
task_families:
- documentation
capabilities: []
aliases:
- docs
- documentation
- mkdocs
- readme
- adr
- tutorials
- documentation-site
- docs-site
- website
stack:
  languages:
  - markdown
  - python
  frameworks:
  - mkdocs
  independent: false
preconditions:
- Content or a codebase that needs explaining
- 'A docs target: repo README or MkDocs site'
required_tools:
- read
- write
exclusions:
- Inline code comments as the only deliverable
- Marketing copy or non-technical content
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

# Documentation & Communication

## Core Skills

| Skill | Practice |
|---|---|
| MkDocs | Build docs site with MkDocs Material |
| API docs | mkdocstrings for Python API docs |
| Architecture docs | Write ADRs (Architecture Decision Records) |
| Tutorials | "Getting Started" and "Building Your First Skill" |
| README | Clear README with badges, quick start, examples |

## Key Resources
- [MkDocs](https://www.mkdocs.org/)
- [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
- [Diátaxis](https://diataxis.fr/)

## Practice Project
Build docs site: Getting Started, Architecture, API Reference, Skills Guide, Deployment Guide.
