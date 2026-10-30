---
type: "query"
date: "2026-10-13T02:59:44.724241+00:00"
question: "云端 compose ps 只显示 memsys-qdrant-1，memory 和 gateway 没有日志，为什么域名不能访问？"
contributor: "graphify"
outcome: "useful"
source_nodes: ["MemoryService", "Models"]
---

# Q: 云端 compose ps 只显示 memsys-qdrant-1，memory 和 gateway 没有日志，为什么域名不能访问？

## Answer

Expanded from original query via vocab: serve service config memory embedding model. The existing graph locates MemoryService and Models but predates deployment files, so current deploy/compose.yaml and serve.py were checked directly. The user-provided server output confirms only Qdrant exists; memory and gateway containers have not been created. Public DNS lookup of whb.win still returned NXDOMAIN. Configure DNS and ACME email, then start only memory and gateway with compose up --no-deps to avoid starting cloud model dependencies. Model endpoints are still unconfirmed; ingress health does not prove model readiness. No model inference or evaluation was run.

## Outcome

- Signal: useful

## Source Nodes

- MemoryService
- Models