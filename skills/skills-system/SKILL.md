---
name: skills-system
description: Skills system (meta-skills) — SKILL.md parsing, trigger matching, progressive disclosure,
  skill composition, skill curator, skill templates, skill marketplace
triggers:
- skill
- trigger
- progressive disclosure
- curator
- template
- marketplace
version: 1.0.0
schema_version: 2
skill_id: skills-system
task_families:
- maintenance
- documentation
capabilities:
- configuration
aliases:
- skills
- skill-authoring
- skill-registry
- meta-skills
- skill-curator
stack:
  languages:
  - python
  - yaml
  - markdown
  frameworks: []
  independent: false
preconditions:
- A skills directory or registry to load and match
- SKILL.md files with YAML front matter
required_tools:
- read
- write
exclusions:
- Running an installed skill to do its own domain work
- General prompt authoring (use prompt-engineering)
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
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# Skills System (Meta-Skills)

## Core Skills

| Skill | Practice |
|---|---|
| SKILL.md parsing | Parse YAML frontmatter + content |
| Trigger matching | Keyword/IDF trigger matching with whole-word boundaries |
| Progressive disclosure | Load metadata always, content on trigger |
| Skill composition | Skill chaining: search → extract → summarize |
| Skill curator | Track usage, archive stale skills |
| Skill templates | SKILL.md templates for repeatable workflows |
| Skill marketplace | Community-curated skill sharing |

## Key Resources
- [Hermes Agent skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)

## Practice Project
Build a skill registry: load skills from skills/ directory, match triggers by embedding similarity, inject skill content into prompt when triggered.
