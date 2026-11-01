# Atropos user guide

Start with the [1.0 supported reference](v1.0.md) for versions, installation,
qualification and upgrade limits.

## Contents

- [Install and configure](#install-and-configure)
- [Workflow contract](#workflow-contract)
- [Operate a run](#operate-a-run)
- [Python integration](#python-integration)
- [Failure and recovery](#failure-and-recovery)
- [Limits](#limits)
- [Storage and queue adapters](backends.md)
- [Decision rules and ready-made templates](rules.md)
- [Draft, test and active version lifecycle](rule-lifecycle.md)
- [Standalone rule service](rule-service.md)
- [Explicit decision-to-workflow dispatch](rule-dispatch.md)
- [Verified observations and end-to-end test mode](observations-and-tests.md)
- [Dedicated test alert receivers](test-alert-delivery.md)
- [Independent API client](../../client/README.md)
- [Shared queue admission capacity](queue-capacity.md)
- [Configure an Auton executor](execution-adapters.md)
- [Local storage backup and restore](storage-backup.md)
- [Archive, unarchive and delete rules](rule-retirement.md)
- [Retention and archive capacity](retention.md)
- [Account membership and group grants](membership.md)
- [Search admitted interventions](intervention-search.md)
- [Notify through Alertmanager](alertmanager.md)
- [Reconcile an uncertain outcome](reconciliation.md)

## Install and configure

Follow the [installation instructions](../../README.md#start-here) with Python 3.11+.
SQLite is the default (`sqlite:///atropos.db`) with its integrated queue.
See [storage and queue adapters](backends.md) to configure SQLite with Redis
delivery, retain Redis storage explicitly, or install other adapters. No automatic
data migration occurs when changing backends. Generic role parameters replace
all former product-prefixed configuration names.

For Redis in either role, install `python -m pip install 'atraxis-atropos[redis]==1.0.0'`.
The [Redis example](../../examples/redis.conf) uses AOF and no eviction. Provision
a persistent volume, size memory and monitor disk capacity. `appendfsync everysec`
can lose approximately the last second of writes after a crash; choose the fsync
policy for your durability needs and test backups/restoration. AOF is not a
replacement for backups. The application does not configure or verify the Redis
server's persistence or deployment settings.

Use one namespace exclusively for Atropos, with one consumer group managed by
Atropos. Do not add other consumers/groups to its stream. Prefixes are logical
separation, not an authorization or multi-tenant security boundary. Only trusted
processes may write its Redis state.

## Workflow contract

See [the runnable example](../../examples/inspect.yml). Version 1 requires exactly
`version`, `name`, `start` and `steps`. Step names reference a directed acyclic
graph; loops and unreachable steps are rejected. Retries are explicitly bounded.

| Step field | Meaning |
| --- | --- |
| `action` | Name from the application's trusted action registry |
| `with` | JSON object passed to the action; default `{}` |
| `next` | Step after success; omitted/null ends successfully |
| `on_error` | Step after confirmed failure once retries are exhausted |
| `on_decision` | Map a result's string `decision` to a next step or null |
| `timeout` | Observation deadline, 0.01–3600 seconds; default 30 |
| `retry` | `attempts` 1–5 and `delay` 0.01–3600 seconds; default one attempt |

When `on_decision` is nonempty it takes precedence over `next`. An unmatched
decision fails the workflow; a null branch ends it successfully. `on_error` is
never followed after an uncertain outcome. An error handler can complete the
workflow successfully; inspect history for prior confirmed failures.

Inputs support explicit JSON Pointers: `{"$ref": "/input/value"}`,
`{"$ref": "/results/previous/key"}`, `{"$ref": "/target"}` or
`{"$ref": "/run_id"}`. No expression evaluation, shell interpolation or imports
are performed. A reference is resolved immediately before its step; a missing
value fails that step before action dispatch. Structural validation does not
prove every runtime input/result reference will exist.

The CLI registers `core.echo` (returns its inputs) and `core.fail` (confirmed
demonstration failure). It does not load arbitrary plugins from a workflow.
Decision engines can later be bound as registered actions returning a decision;
the 1.0 reference uses the bounded YAML rule model with ZEN 2.1.2. See
[YAML rules and versioning](rules.md) for the separate decision-rule format.

## Operate a run

```sh
atropos validate examples/inspect.yml
atropos plan examples/inspect.yml
atropos submit examples/inspect.yml --target demo-node --dedup-key incident-001
atropos worker --concurrency 2
atropos status RUN_ID
```

`validate` and `plan` need no storage connection and run no actions. `plan` describes
structure; it is **not a simulation of external effects or decision results**.
Submission accepts an optional `--input input.json`. Capture the returned run ID.
The same deduplication key and definition/inputs/target return the existing run;
changing the content with that key is rejected. Without a key, each submission
creates a new run. Deduplication applies only while the corresponding state is
retained, and is scoped to the namespace.

`worker --once` attempts one step per configured worker, not a complete workflow.
The regular worker polls continuously and promotes due retries. Concurrency is
1–32 **per CLI process**; it is not a global worker budget. The target lock spans
the entire workflow, including waits and uncertainty. Use a consistent resource
identity: two different target strings are two different locks, even if they
refer to the same physical resource.

Use the global option before the command: `atropos --namespace demo worker`.
Exit codes: 0 command completed/submission accepted; 2 invalid input or conflict;
3 backend error; 130 keyboard interruption. A successful `status` command does not
mean the inspected workflow succeeded.

## Python integration

The business service and worker are independent of the CLI:

```python
import asyncio
import redis
from atropos.actions import Action, builtin_actions
from atropos.definitions import load
from atropos.redis_store import RedisStore
from atropos.service import WorkflowService
from atropos.worker import Worker

client = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True,
                             socket_connect_timeout=3, socket_timeout=3)
store = RedisStore(client)
store.initialize()
actions = builtin_actions()
service = WorkflowService(store, actions)
run = service.submit(load("examples/inspect.yml"), {}, "demo-node", "demo-001")
asyncio.run(Worker(store, actions).run_once(block_ms=0))
```

A registered `Action` is a trusted async callable `(params, context) -> dict`.
It must cooperate with cancellation and must not block the event loop.
`context.idempotency_key` is stable per run/step. The external system must itself
honor any idempotency promise; passing a key is not an exactly-once guarantee.
Return a finite JSON object of at most 8 KiB. Raise `ActionFailed` only for a
confirmed failure; raise `OutcomeUnknown` when the effect may have occurred.
Set `retry_safe=True` only when repeating a confirmed failed attempt is safe.
Unexpected exceptions and invalid/oversized results are treated as uncertain.

The optional Auton adapter uses the existing client, never a CLI subprocess:

```python
from atropos.auton_adapter import auton_action

# Supply an installed Auton client with OperationService and approved settings.
actions["node.inspect"] = auton_action(
    target="node-a", uris=["https://approved-auton.example"], endpoint="inspect")
```

Build the registry before creating `WorkflowService`/`Worker`, which copy it.
Target/URL/endpoint and optional `auth` are supplied by the trusted application.
They are not read from rule inputs. The adapter derives a stable job ID, returns
only job identity/status/exit code, and disables retries. It stops observation
on cancellation; it does not cancel an admitted remote job. For service-owned configuration, pinned bindings and a local test simulator, see
[configured executors](execution-adapters.md). Actual local client/daemon execution
and operator restore have dedicated acceptance tests; remote deployment still
requires its own TLS, identity and endpoint review.

## Failure and recovery

| State | Meaning |
| --- | --- |
| `queued` | Accepted or ready for another step |
| `running` | A worker holds a renewable lease for the current step |
| `retrying` | Confirmed failed attempt, awaiting an allowed retry |
| `succeeded` | Normal graph completed, or operator explicitly resolved the run |
| `failed` | Confirmed terminal failure, invalid step input or explicit resolution |
| `uncertain` | An external effect may still be running or may have completed |

Confirmed steps are checkpointed before the next step is queued. A delivery
abandoned before a claim can be reclaimed. A claimed in-flight step whose lease
expires is quarantined instead of repeated. Stale workers cannot commit after
losing their lease. A timeout does not prove a remote job stopped.

An uncertain run retains its target lock. Inspect the external system and prove
the action has stopped before running:

```sh
atropos resolve RUN_ID --outcome failed
```

`--outcome succeeded` is also available only for an independently confirmed
terminal result. Resolution is an operator assertion, not an automatic check.
It **terminates the run**, releases the lock and never invents intermediate
results or resumes remaining steps. Retained history and the `resolution` field
distinguish this path from normal completion. Subsequent work needs a new explicit
submission and a new deduplication identity.

## Limits

- The plain workflow CLI is for trusted local operators. Use the separate rule
  service and guarded worker for authenticated, scoped rule dispatch. Sandboxed
  action execution and a human-approval workflow step are not implemented.
- Definitions/admission data are bounded to 256 KiB, at most 64 steps, at most
  five attempts per step, 8 KiB per result and 1 MiB per persisted snapshot.
- Validation bounds structure, not CPU/memory use of arbitrary registered code.
- State/deduplication/history retention is indefinite. There is no safe purge
  command yet; plan storage capacity and do not delete unresolved state/locks.
- Global concurrency, Sentinel, Cluster and Valkey
  compatibility are not implemented or validated. No HA or throughput claim.
- No exactly-once guarantee for external effects or crash-loss guarantee beyond
  the underlying Redis persistence/backup configuration.

For development status and testing, see the [contributor guide](../contributor/README.md).

