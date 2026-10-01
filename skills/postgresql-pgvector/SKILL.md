---
name: postgresql-pgvector
description: "PostgreSQL + pgvector mastery — schema design, connection pooling, vector queries, LISTEN/NOTIFY, partitioning, BRIN indexes, advisory locks, RLS, HNSW tuning"
triggers:
  - "postgres"
  - "pgvector"
  - "vector search"
  - "schema"
  - "index"
  - "partition"
  - "rls"
version: 1.0.0
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
