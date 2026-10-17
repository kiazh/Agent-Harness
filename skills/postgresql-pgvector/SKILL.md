---
name: postgresql-pgvector
description: PostgreSQL + pgvector mastery — schema design, connection pooling, vector queries, LISTEN/NOTIFY,
  partitioning, BRIN indexes, advisory locks, RLS, HNSW tuning
triggers:
- postgres
- pgvector
- vector search
- schema
- index
- partition
- rls
version: 1.0.0
schema_version: 2
skill_id: postgresql-pgvector
task_families:
- backend-api
- maintenance
capabilities:
- database
- performance
aliases:
- pgvector
- postgres
- postgres-vector
- postgresql
- vector-search
stack:
  languages:
  - sql
  - python
  frameworks:
  - postgresql
  - pgvector
  - asyncpg
  independent: true
preconditions:
- PostgreSQL server with the pgvector extension available
- Permission to create or alter schema, indexes, and policies
- Embedding vectors already produced or producible for the target rows
required_tools:
- psql
- asyncpg
exclusions:
- No PostgreSQL or pgvector in the stack
- Application-code debugging with no schema or query change
- Embedding model selection or training (use embedding-finetuning)
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

# PostgreSQL + pgvector

## Core Skills

| Skill | Practice |
|---|---|
| Schema design | Design context_chunks with all indexes |
| Connection pooling | asyncpg pool with min/max sizing |
| pgvector queries | Top-5 chunks by cosine similarity |
| LISTEN/NOTIFY | Outbox pattern to avoid global lock |
| Partitioning | Range-partition by created_at |
| BRIN indexes | Time-series context retrieval |
| Advisory locks | Distributed lock for session ownership |
| JSONB vs bytea | Benchmark JSONB vs MessagePack |
| Row-Level Security | Per-user data isolation |
| HNSW tuning | Tune m and ef_conformance |

## Key Resources
- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [asyncpg pooling](https://magicstack.github.io/asyncpg/current/api/index.html#connection-pools)
- [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

## Practice Project
Build a minimal context store: insert 10K chunks with embeddings, retrieve top-5 by similarity, measure latency. Target: <5ms p95.
