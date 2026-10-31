# First stable release: scope and delivery gates

Scope baseline: 29 October 2026. This plan bounds Atropos 1.0 and its independent
API client; it does not change package versions, publish a release or declare the
support matrix beyond its recorded evidence. [Atraxis's matching scope](https://github.com/decryptus/atraxis/blob/main/docs/v1-delivery.md)
owns the operator interfaces, inventory and account administration.

## Included functionality

Finish and qualify the implemented capabilities below. Keep the runtime independent
of Atraxis and of any AI provider; authoring assistance is not execution authority.

| Area | Included in the baseline | Boundary |
| --- | --- | --- |
| Rules | Existing drafts, immutable revisions, scenario evidence, explicit activation/rollback/deactivation and retirement | Current scoped rights, exact generations and audit; imports and evaluation never activate or dispatch implicitly |
| Workflows | Current bounded workflow graph, registered actions, authorized admission, target exclusion, retries and deduplication | Unknown external outcomes remain uncertain; no blind replay or exactly-once promise |
| Authority | Existing subject/organization/domain/operation/action/provider scopes and trusted inventory/membership projections | Recheck authority at execution; delivery receipt or signed admission is not a grant |
| Service and client | Authenticated rule API and independent installed client on Python 3.11/3.12 | The client has no engine, Redis or ZEN dependency; remote access requires verified TLS |
| Storage and delivery | Existing SQLite default, Redis, PostgreSQL and MySQL/MariaDB storage implementations; integrated queue and Redis delivery | Generic role contracts remain; an optional combination is supported only when the release manifest names qualifying evidence |
| Execution and notification | Registered Auton and Alertmanager adapters, explicit test isolation and approved test receiver handling | Approved targets/destinations only; adapter acceptance does not prove downstream receipt |
| Recovery | Execution pause, coordinated cold backup, explicit restore/resume, retained audit/queue/locks and uncertainty reconciliation | Current authority and surviving external effects must be reconciled before resume |

Existing backends are retained; this scope does not silently remove SQL or Redis
support. The release support matrix must distinguish implementations with contract
tests from configurations qualified for the advertised operational use. Adding a
new backend or queue adapter is not required to finish this baseline.

## Contract baseline and language decision

| Contract | Current reference |
| --- | --- |
| Workflow definitions | [Workflow version 1](../user/README.md#workflow-contract): bounded acyclic graph and registered actions |
| Rule authoring/evaluation | [Current YAML authoring model and ZEN integration](../user/rules.md); no arbitrary JDM import or silent lossy conversion |
| Lifecycle and retirement | [Revision/generation and evidence checks](../user/rule-lifecycle.md), [separate retirement rights](../user/rule-retirement.md) |
| API and independent client | [Rule service request/response and identity contract](../user/rule-service.md), [client package](../../client/README.md) |
| Dispatch and observations | [Explicit workflow dispatch](../user/rule-dispatch.md), [signed observations and marked simulations](../user/observations-and-tests.md) |
| Persistence and delivery | [Technology-independent contracts](backend-contracts.md), [documented backend profiles](../user/backends.md) |
| Backup and uncertainty | [Storage backup](../user/storage-backup.md), [operator reconciliation](../user/reconciliation.md) |

**1.0 language decision — 29 October 2026:** retain the existing version-1
YAML rule authoring model, its same-schema JSON representation and ZEN 2.1.2
for evaluation. Retain the separate version-1 bounded workflow graph for execution.
This explicit delivery decision follows the owner's instruction to close the 1.0
scope around implemented capabilities and proceed to a release.

Rule ordering is first-match with an explicit default. Inputs remain typed,
required and finite; conditions use the documented comparison operators and
bounded `all`/`any`/`not`/local-check references. A decision label never executes
an action. Workflow steps use registered actions and explicit authorized bindings.
The [rule guide](../user/rules.md) is the semantic reference, including complexity
limits. Native arbitrary JDM import, expressions, a second language, automatic
translation, implicit units/time windows and embedded scripts are outside 1.0.
The [1.0 support matrix](../user/v1.0.md) identifies the qualified versioned
contracts. Unsupported formats remain explicitly rejected.

The candidate manifest must pin engine/client/Atraxis source and package versions,
dependencies, format versions and each claimed storage/queue/transport profile.
Unsupported prototype formats continue to fail explicitly. No compatibility layer,
cross-backend conversion, data deletion or automatic migration is introduced here.

## Four delivery gates

The gate identifiers and order match Atraxis. Each gate closes on recorded evidence,
not on the existence of a feature or a test name alone.

| Gate | Deliverable | Exit criterion |
| --- | --- | --- |
| G1 — scope and contracts | This functional baseline and candidate support manifest | Explicit language decision; matching client/service and documented formats; exact qualified configurations and versions identified |
| G2 — installation and complete recovery | Fresh operational installation and coordinated recovery on a distinct host | Verify real identity/TLS/storage, all persistent state and configuration, audits, deduplication, queued work and uncertainty; preserve pause and enforce current revoked rights before resume |
| G3 — final integration and endurance | Approved source → rule → action → notification path and representative workload/history evidence | Real approved targets/destinations work within documented limits; marker isolation, failures, uncertainty and duplicate semantics hold; measurements identify their exact backend/configuration |
| G4 — release candidate | Engine/client packages, documentation and consolidated evidence | Provenance/license/version/dependency checks and required CI pass; G1–G3 have no unresolved blocker for the claimed support matrix |

G1–G3 are closed for the [bounded 1.0 reference](../user/v1.0.md). The reviewed
manifest is `docs/releases/1.0.0-qualification.json`; it binds the engine/client
runtime identity and hashes of retained private operational records. G4 requires
final required CI and the separate [stable publication](release-stable.md). Keep already acquired
[package evidence](installed-packages.md), backend contracts, isolated integration,
separate-host recovery and workload evidence within their stated scope. Do not
reset them just because the delivery plan is made explicit.

## Evidence reuse and change control

- Preserve each result's exact inputs, source/configuration, expected behavior,
  actual outcome and limits. Synthetic, operational and proposed work are distinct.
- Repeat checks only for a relevant change, failure, uncovered requirement or
  documented evidence gap. Required PR checks remain applicable; do not start a new
  endurance campaign or real notification delivery implicitly.
- Correct defects affecting a gate within scope. Put unrelated feature additions
  on the later roadmap and record why any completed check must be reopened.
- Missing real destinations stay an open integration prerequisite. They do not
  block independent contract, recovery or packaging work.
- No percentage-complete estimate, release date, throughput/SLO claim or general
  leak-absence claim follows from this scope. Publishing G4 requires a separate
  release decision; no tags, registry, deployment or Read the Docs action is implied.

## Candidate publication and stopping rule

An explicitly labelled prerelease may be published before the operational gates
close, provided its exact packages pass the required CI and its release notes
list the outstanding gates. This is a distribution checkpoint, not G4 closure.
The first supported deployment target is Linux amd64, Python 3.11/3.12 packages,
and Python 3.12 operator Compose with SQLite storage and the integrated queue.
Redis and SQL implementations remain available with their existing contract-test
evidence; they do not enlarge the initial operational qualification matrix.
No new backend, language or feature is required to reach this target.

The reference installation, coordinated recovery on another host and approved
HTTPS diagnostic with actual local receiver receipt are qualified. The retained
workload keeps its original synthetic SQLite/Redis scope. Final engine/client
packages are published only after their exact sources pass the required checks.
Missing site/recipient choices must be reported as concrete prerequisites, not
replaced by repeated synthetic checks or an ever-expanding feature list.

## Deferred beyond this baseline

High availability, hot backup, external backup providers, cross-backend conversion,
additional queue/storage adapters, distributed global concurrency and new automatic
action/plugin mechanisms remain later work. Sandboxed arbitrary actions and a new
human-approval workflow step are not added to the current registered-action model.
Do not delete existing adapters or data to enforce this boundary.

Return to the [roadmap](../../ROADMAP.md) or the [contributor guide](README.md).
