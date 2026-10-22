# Retention and archive capacity

Run the offline inspector on a current protected storage backup:

```sh
atropos storage-retention --input /private/engine-backup.json
```

It accepts Redis format 2 and SQL format 3 archives for SQLite, PostgreSQL and
MySQL/MariaDB. Install the documented service extra for private archive reading.
No service profile, database connection, network destination or running worker is
needed. Existing private-file, digest and format bounds apply; symlinks and public
files are refused. A checksum is an integrity check, not proof of authenticity or
successful restoration. Use only trusted archives and rehearse restoration.

The JSON report contains capture time, namespace, digest, supported archive limits
and remaining capacity. SQL archives expose aggregate row counts and run outcomes
separately for real and test namespaces. Redis DUMP values remain opaque: the report
counts keys by category, and `statuses: null` explicitly means outcomes are unknown.
No run IDs, targets, inputs, rules, credentials or raw dump values are printed.

Exit **0** means the archive is below the warning threshold. Exit **1** means at
least one metric has reached **80%** of its supported format limit. Exit **2** means
input inspection failed. These thresholds do not modify runtime limits or authorize
purging. Counts refer to this archive, not current storage or filesystem free space.
An older archive can understate current growth. Compare successive dated reports.

Current limits are 10,000 Redis keys / 32 MiB of decoded key-and-dump data, or
100,000 SQL rows / 32 MiB of serialized payload, with a common 48 MiB archive limit.
The inspector rejects an archive already outside its supported format. Monitor
successful snapshots before their bounds are reached. Remaining rows or bytes
cannot be converted into a guaranteed number of future interventions: rules, audit,
results and evidence all consume capacity. Server memory and disk require separate
monitoring; these numbers do not measure them.

## Preserve evidence before maintenance

A completed or old run is not automatically disposable. Keep its run identity,
deduplication receipt, admission/observation evidence, referenced rule revision,
audit and results together. Removing deduplication can turn a replayed event into
a new intervention. Keep uncertain runs and target exclusion locks until the
external outcome has been reconciled through the documented operator procedure.
A backup does not make it safe to remove those live protections.

1. Inspect [queue capacity](queue-capacity.md). If incoming work threatens the bound,
   explicitly stop admissions using the documented capacity controls; preserve
   accepted work. Execution pause alone does not stop admission.
2. Pause both execution namespaces, drain in-flight steps and make the existing
   [quiescent storage backup](storage-backup.md). Keep real/test evidence together.
3. Inspect its capacity and retain its digest, capture time and matching protected
   configuration/credential set. Never place secret archives in source control.
4. Restore to an empty disposable destination on the same supported backend/schema.
   Keep restored execution paused and prevent duplicate access to real targets.
   Verify rules, memberships, audit, queued work, uncertain locks and deduplication.
5. Resume only after current authorization, source freshness and external outcomes
   have been reconciled. Do not replay a new event merely to test recovery.

The application currently retains all live evidence. It provides no general
live-history purge, TTL policy or automatic archive expiry. Native rule archive is
a lifecycle state, not physical deletion. Existing draft deletion remains limited
to the separately authorized never-activated/unlinked case. Do not use SQL DELETE,
Redis expiry/eviction or stream trimming to implement a retention policy.

Operators choose how many verified **backup copies** to retain according to their
recovery requirements; deleting a backup copy is distinct from deleting live
engine records. Keep an independently verified restore point and unresolved
incident evidence. No arbitrary age-based policy is selected here. At a capacity
warning, plan supported archive growth or a reviewed archival mechanism before the
current bound is exhausted. Online purge and sustained-growth qualification remain
open; the report does not certify 1.0 readiness or production deployment.
