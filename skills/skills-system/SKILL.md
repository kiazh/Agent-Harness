---
name: skills-system
description: "Skills system (meta-skills) — SKILL.md parsing, trigger matching, progressive disclosure, skill composition, skill curator, skill templates, skill marketplace"
triggers:
  - "skill"
  - "trigger"
  - "progressive disclosure"
  - "curator"
  - "template"
  - "marketplace"
version: 1.0.0
---

# Skills System (Meta-Skills)

## Core Skills

| Skill | Practice |
|---|---|
| SKILL.md parsing | Parse YAML frontmatter + content |
| Trigger matching | Embedding-based trigger matching |
| Progressive disclosure | Load metadata always, content on trigger |
| Skill composition | Skill chaining: search → extract → summarize |
| Skill curator | Track usage, archive stale skills |
| Skill templates | Jinja2 templates for skill prompts |
| Skill marketplace | Community-curated skill sharing |

## Key Resources
- [Hermes Agent skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)

## Practice Project
Build a skill registry: load skills from skills/ directory, match triggers by embedding similarity, inject skill content into prompt when triggered.
