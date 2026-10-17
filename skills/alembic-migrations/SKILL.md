---
name: alembic-migrations
description: Alembic migrations — schema versioning, migration scripts, rollback procedures, data migrations,
  migration testing
triggers:
- alembic
- migration
- schema change
- rollback
- version
version: 1.0.0
schema_version: 2
skill_id: alembic-migrations
task_families:
- backend-api
- maintenance
capabilities:
- database
- migration
aliases:
- alembic
- db-migrations
- schema-migrations
stack:
  languages:
  - python
  - sql
  frameworks:
  - alembic
  - sqlalchemy
  independent: false
preconditions:
- SQLAlchemy models or an existing database schema
- Alembic available or permission to initialize it
required_tools:
- shell
exclusions:
- Ad-hoc queries that do not change schema
- NoSQL stores with no migration format
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

# Alembic Migrations

## Core Skills

| Skill | Practice |
|---|---|
| Schema versioning | Initialize Alembic, create first migration |
| Migration scripts | Write migration to add new column |
| Rollback procedures | Implement downgrade migration |
| Data migrations | Write data migration with SQL |
| Migration testing | Test upgrade/downgrade cycle |

## Key Resources
- [Alembic docs](https://alembic.sqlalchemy.org/)
- [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)

## Practice Project
Set up Alembic for Machina schema. Create migrations for: adding a new table, adding a column, creating an index. Test upgrade and downgrade.
