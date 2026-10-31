# Atropos roadmap

Updated 31 October 2026. Atropos 1.0 is a deterministic rule and
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

## Four steps to the first stable release

The [1.0 scope and delivery gates](docs/contributor/v1-delivery.md) fix the baseline
to implemented capabilities, define concrete exit criteria and separate later
features. Existing qualification is reused within its proven scope.

1. **G1 — scope and contracts:** the functional baseline is documented; record the
   exact candidate support manifest. The version-1 YAML rule/workflow models
   and ZEN 2.1.2 are explicitly selected for 1.0; no second language is required.
2. **G2 — installation and complete recovery:** qualify a fresh operational site
   and recovery on another host, with credentials, state, audit, queued/uncertain
   work and explicit authority-checked resume.
3. **G3 — final integration and endurance:** qualify installed sources, approved
   execution targets, actual notification destinations and representative retained
   histories/workloads for the configurations claimed as supported.
4. **G4 — release candidate:** finalize engine/client packages, documentation and
   evidence after the preceding gates pass. Publication is a separate action.

The initial operational target is Linux amd64 with SQLite and the integrated
queue; existing SQL/Redis adapters retain their documented qualification limits.
An explicitly labelled prerelease may precede operational gate closure.
G1–G4 are closed for the [bounded reference](docs/user/v1.0.md).
The [engine and client 1.0.0 release](https://github.com/decryptus/atropos/releases/tag/v1.0.0)
is published after all six required workflows passed on the exact source.
The downloaded archives, module hashes and installed package identities are verified;
registry publication and real external recipients remain separate.
New features do not automatically join this baseline; relevant
defects are corrected without reopening unrelated completed checks. No new campaign
or external notification is authorized merely by this roadmap.

## Qualification and limits

Core and service suites at the stable source checkpoint passed 228 and 94 tests
respectively, with no skips. A controlled integrated SQLite/Redis chain completed
four hours of load, quiescence and recreation recovery with two real synthetic
effects. These are bounded acceptance results, not an exactly-once guarantee,
general leak-absence proof or production-capacity promise.

Installed-distribution acceptance builds engine and client wheels from source
archives on Python 3.11/3.12. It verifies installed source bytes, license and
runtime/metadata version agreement, isolated dependencies, the actual API and
SQLite backup/restore with retained execution pause and explicit resume. The stable publisher reuses these exact artifacts after the reviewed operational
gates close; publication to a package registry remains a separate operation.
See [package validation](docs/contributor/installed-packages.md).

Atraxis's [separate-host recovery acceptance](https://github.com/decryptus/atraxis/blob/main/docs/contributing.md#recovery-on-a-separate-host)
also exercises the installed engine with identical exported images and cold
volumes on two disposable hosts. It retains audits, queued work, uncertainty and
target locks without replay, and checks current reconciliation rights. It does
not establish live host-loss recovery or the current state of a surviving real
external recipient. A separate operational recovery and approved diagnostic/local-receiver path
qualify the bounded 1.0 reference; they do not qualify arbitrary surviving effects
or external email/chat recipients.

High availability, hot backup, external backup providers, cross-backend
conversion and additional queue adapters remain future work. Each new adapter
requires its own durability, authorization and recovery evidence.

The support matrix names the qualified versioned contracts. Unsupported
formats are rejected explicitly, without implicit migration or data deletion.
