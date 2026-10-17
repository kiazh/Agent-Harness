---
name: grpc-services
description: gRPC services — protobuf schemas, service definitions, streaming RPCs, deadlines, interceptors,
  error codes, grpc-web
triggers:
- grpc
- protobuf
- protocol buffers
- protoc
- streaming rpc
- grpc-web
version: 1.0.0
schema_version: 2
skill_id: grpc-services
task_families:
- backend-api
capabilities:
- api-design
- error-handling
- type-safety
aliases:
- grpc
- protobuf
- protocol-buffers
- protoc
- streaming-rpc
stack:
  languages:
  - protobuf
  frameworks:
  - grpc
  - grpc-web
  independent: false
preconditions:
- A service boundary that benefits from typed RPC
- A Protocol Buffer toolchain such as protoc or buf
required_tools:
- shell
exclusions:
- Small public REST endpoints
- In-process calls that never cross a boundary
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
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# gRPC Services

## Core Skills

| Skill | Practice |
|---|---|
| Protobuf schema | Design messages with stable field numbers and package names |
| Service definitions | Unary, server, client and bidirectional streaming stubs |
| Compatibility | Never reuse or renumber a removed field; reserve it |
| Deadlines | Set per-call deadlines and propagate cancellation |
| Interceptors | Auth, logging, tracing and retry as cross-cutting layers |
| Status codes | Map failures to the canonical gRPC status set |
| grpc-web | Gateway or proxy for browser clients and CORS |

## Pitfalls
- Without deadlines a slow dependency stalls the caller indefinitely.
- Adding a field with a reused number silently corrupts old clients.
- Ignoring streaming backpressure exhausts memory on large payloads.
- Leaking error details in status messages exposes internals.

## Practice Project
Define one service with a unary and a server-streaming method, generate stubs, add an auth interceptor and a deadline, map errors to status codes, and expose it to a browser client through grpc-web.

## Key Resources
- [gRPC docs](https://grpc.io/docs/)
- [Protocol Buffers](https://protobuf.dev/)
- [gRPC status codes](https://grpc.io/docs/guides/status-codes/)
