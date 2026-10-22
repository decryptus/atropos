# Atropos roadmap

Updated 22 October 2026. Atropos is an experimental deterministic rule and
workflow engine, with a separately packaged API client. This roadmap is a product
direction, not a release-date or production-readiness commitment.

## Available today

- Versioned rule drafts, scenario evidence and explicit scoped activation,
  rollback, archive and retirement controls.
- Authorized workflow admission, bounded queues, target exclusion, deduplication,
  worker checks and retained audit.
- Explicit uncertain outcomes and operator reconciliation, without automatically
  replaying an external action whose result is unknown.
- SQLite storage by default, Redis storage/delivery options, and PostgreSQL and
  MySQL/MariaDB storage adapters with tested contracts and verified remote TLS.
- Paused-execution backup and explicit restore/resume with retained durable work,
  uncertain locks and execution-time authorization checks.
- Registered Auton execution and Alertmanager transport adapters, plus isolated
  marked simulations.
- An independent HTTP client without engine, Redis or ZEN dependencies.

The runtime works without Atraxis and without an AI provider. Decision evaluation
does not imply action execution. Adapter acceptance does not prove downstream
notification delivery.

## Next milestones

1. Complete operational qualification of installed sources, approved execution
   targets and notification destinations.
2. Exercise host-loss recovery and complete operator backup, credential renewal
   and recovery guidance.
3. Extend endurance and retained-history measurements across representative
   workloads and supported storage backends.
4. Finalize the supported rule/workflow contract and intervention-language choice.
   The existing ZEN integration is a tested candidate.
5. Complete packaging and distribution and qualify a stable release candidate
   with explicit supported configurations and limitations.

## Qualification and limits

Core and service suites at the current source checkpoint passed 224 and 94 tests
respectively, with no skips. A controlled integrated SQLite/Redis chain completed
four hours of load, quiescence and recreation recovery with two real synthetic
effects. These are bounded acceptance results, not an exactly-once guarantee,
general leak-absence proof or production-capacity promise.

High availability, hot backup, external backup providers, cross-backend
conversion and additional queue adapters remain future work. Each new adapter
requires its own durability, authorization and recovery evidence.

Current configuration, API and storage formats are experimental. Unsupported
formats are rejected explicitly, without implicit migration or data deletion.
