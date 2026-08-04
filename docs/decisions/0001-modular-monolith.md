# 0001: Modular monolith, not microservices

## Status

Accepted (Milestone 1).

## Context

The platform needs to support several distinct concerns -- connection
management, credential storage, connector execution, scheduling, execution
history -- for a product still finding its shape. Microservices would let
each concern scale and deploy independently, at the cost of distributed
transactions, network calls between what are currently simple function
calls, and substantially more operational surface (service discovery,
inter-service auth, multiple deployables) than the product currently
justifies.

## Decision

Build a single Django codebase with clear internal module boundaries
(`apps/core`, `apps/connectors`, `apps/execution`, ...), split into three
*processes* (web, worker, scheduler) that share that codebase, rather than
separate services. Module boundaries are enforced by convention and code
review (each app owns its models and is reached only through its public
interface), not by network calls.

## Consequences

- Cross-module changes are a single PR, not a coordinated multi-service
  rollout -- fast iteration while the domain model is still settling.
- No distributed-systems failure modes (partial deploys, cross-service
  version skew) to design around yet.
- If a specific component genuinely needs independent scaling later (e.g.
  worker throughput vastly outgrowing web), the process split already
  exists as the seam to pull it out along -- this decision doesn't block
  that, it just doesn't pre-build for it.
- Discipline is required to keep app boundaries real (e.g. no app reaching
  into another's models directly) since nothing enforces it at the
  language/network level the way a service boundary would.
