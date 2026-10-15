---
name: grpc-services
description: "gRPC services — protobuf schemas, service definitions, streaming RPCs, deadlines, interceptors, error codes, grpc-web"
triggers:
  - "grpc"
  - "protobuf"
  - "protocol buffers"
  - "protoc"
  - "streaming rpc"
  - "grpc-web"
version: 1.0.0
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
