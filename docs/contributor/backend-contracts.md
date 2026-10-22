# Extensible storage and delivery

`storage_contract.py` defines technology-independent admission, claim and delivery
interfaces. `backend_registry.py` selects implementations at the configuration
boundary. Core workflow admission, workers, rule lifecycle, authorization and
search do not import a storage driver. SQLite, PostgreSQL, MySQL/MariaDB and Redis implement persistence;
Redis imports are lazy at the adapter boundary and its package is optional.

## Installed adapters

A trusted installed distribution can expose factories through entry points:

```toml
[project.entry-points."atropos.storage_backends"]
document-service = "my_adapter:open_storage"

[project.entry-points."atropos.queue_backends"]
message-service = "my_adapter:open_queue"
```

Storage factories receive the role configuration dictionary. Queue factories
receive that dictionary and keyword arguments `namespace` and `lease_ms`.
Configurations are administrative input; requests and workflow definitions never
select plugins. Unknown/duplicate registrations fail closed. Built-in names are
reserved. Names do not form a hardcoded SQL vendor allowlist.

A storage backend exposes `client` as its shared evidence/transaction-domain
identity, `workflow_store(namespace, queue)`, `repository(organization, namespace)`,
`lifecycle_store(repository)`, `maintenance(namespace)` when supported, and
`close()`. Repository and lifecycle adapters implement the operations used by
`RuleLifecycle`, including availability, revision CAS, reports, active generation
CAS, retirement and bounded complete admission history. Evidence get/set values
are opaque strings with optional expiry. Conditions, proof consumption, audit,
capacity checks, deduplication, run insertion and durable outbox insertion must
share one transaction domain. An adapter must reject a capability it cannot
provide; it must never emulate these guarantees with independent writes.

A queue exposes `send(delivery_id, run_id)`, `receive(consumer, block_ms)`,
`acknowledge(delivery_id)` and `close()`. Receive returns either `None` or
`(delivery_id, {'run_id': run_id})`. IDs are scoped to the configured namespace;
repeated send must be safe. Delivery may duplicate or disappear: authoritative
storage retains unfinished work and must be able to reconstruct delivery.
Receiving a message does not authorize execution. Test and real namespaces must
remain distinct throughout delivery, claims and evidence. No payload contains
credentials, action parameters or authority grants.

## Durable outbox and fencing

SQLite uses native `BEGIN IMMEDIATE` transactions, WAL and `synchronous=FULL`.
Rules/evidence use records and ordered journals; runs use indexed native tables
with queryable status, workflow, target, timestamps and delivery/lease fields.
The pending run is its durable outbox. Publishing happens after admission commits.
A publish/ack failure can leave duplicate messages, but a stale delivery ID or
lease token cannot advance a run. Expired running work becomes uncertain instead
of being replayed; the target lock is retained.

The Redis adapter retains its atomic state/stream admission. With an external
queue, that stream is the durable outbox. Both delivery identity and run identity
are checked against the authoritative stream before claiming. The registry
supports separate queue plugins with either supplied storage backend.

No transactions span two independent services. Authority and durable transitions
stay in storage; the delivery queue is reconstructible. SQLite uses host time
and is intended for one host with multiple local worker processes, not a shared
network-mounted database. Separate delivery does not remove that limitation.

SQL intervention search uses an `InterventionIndex` capability: list distinct
owner/rule revision scopes, authorize them in the domain service, then execute SQL
filters/count/pagination joined to those authorized targets. Counts and pages share
one transaction, and authority/identity are checked again before release. Foreign
keys and immutable-evidence triggers protect the normalized admissions read model.
The search does not load every run payload or scan the lifecycle journal. B-tree
indexes select the owner/rule/namespace and run metadata; arbitrary substring
filters still scan the narrowed authorized rows.

Redis search reads retained history in bounded batches rather than refusing after
10,000 lifecycle events. It checks for concurrent history changes and never reports
a truncated total. Redis statuses may evolve during the scan. No claims are made
about remote SQL transport or untested server versions. See the
[adapter configuration](../user/backends.md) and [validation workflows](README.md).

The Redis snapshot format is not portable. SQLite data-only archive semantics and
explicit non-migration behavior are documented in the user guide.

## Validation on 8 October 2026

Python 3.12: 191 core tests and 63 service tests passed, without skips. The
collection guard found all 254 declarations. The new tests exercise native
SQLite reopen, concurrent admission/claims, rollback of proof consumption and
audit, stale leases, uncertain target exclusion, delayed work, paused execution,
Redis delivery loss and acknowledgment failure, independent queue selection with
Redis storage, lifecycle CAS/retirement and authority-scoped test/real search.
A separate CLI smoke test blocked all Redis imports and successfully submitted
and advanced a SQLite workflow. Existing real-Auton process integration was not
rerun for this change. These checks are not power-loss or third-party-driver
certification.

## Relational schema correction — 9 October 2026

SQLite schema 2 rejects schema 1 rather than silently changing existing databases.
`sqlite_schema.py` defines CHAR(64) run IDs (including random admissions), CHAR(32)
delivery/lease IDs and revision keys, checked status enums, positive auto-increment
journal IDs, checked timestamps, non-null keys and admission foreign keys. Immutable
workflow definitions, input, results and history remain per-run JSON payloads with
checked/indexed metadata; they are not an entire account directory BLOB.
Connections go through Sonicprobe AnySQL, with auto-reconnect disabled; SQLite
specific pragmas and SQL use its documented driver connection.

Expired leases are selected before ready queued work, with a separate indexed
query; execution pause cannot hide expired work behind the 100-row delivery batch.
Regression tests cover this case, SQL constraint/evidence refusal, index selection,
search without full-payload reads and histories above the old search ceiling.
