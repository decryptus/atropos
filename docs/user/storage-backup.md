# Local storage backup and restore

These commands are trusted **engine-host administration**, not remote user APIs.
They use the protected engine profile; Atraxis never needs storage access.
Install the independent client and the Atropos service extra first.

## Execution pause and persistent admission

`storage-pause` stops workers from claiming a **new step**, in both real and test
queues. Existing steps finish and checkpoint. Authorized requests continue to enter
the persistent queue under its existing capacity limit. Duplicate IDs recover the
same receipt. Full queues refuse new requests without evicting accepted work.

An admitted request is not a completed intervention. At resume, the guarded worker
rechecks current identity, permissions, active generation, approved binding and
observation freshness. Work that expired or lost permission is cancelled, not
silently refreshed or executed. The original test context stays in its separate
queue. A local step already claimed when pause begins may still execute and must
finish before the snapshot. An expired in-flight lease is quarantined by a running
worker even while paused; the external outcome must be reconciled independently.

Execution pause is independent of `queue-configure --max-outstanding 0`, which
still explicitly stops new admissions. Pause/resume never changes those limits.
SQLite, PostgreSQL, MySQL/MariaDB and standalone Redis storage are supported.
SQL storage can use integrated
delivery or a separate Redis delivery queue. RabbitMQ and other adapters do not
yet supply the required snapshot/recovery capability.

## Supported scope and durability

For Redis storage, use a dedicated standalone Redis database containing only this engine namespace
and its derived test namespace. The atomic snapshot contains rule revisions,
active generations, reports, audit, queued/delayed runs and stream consumer state,
terminal/uncertain runs, target locks and deduplication evidence. Active leases and
running steps make the snapshot fail; queued/delayed work may remain.
An uncertain run must retain one non-expiring target lock and its uncertainty
index entry. Missing, duplicate, expiring or orphaned lock ownership is refused.
A restore that fails these checks removes its newly restored keys and leaves the
empty destination intact. These checks do not authenticate an archive: keep it
private and trusted, and inspect remote outcomes before resuming execution.

Configure persistent volumes, AOF, a suitable fsync policy and no eviction. The
application does not configure or verify these deployment guarantees. Acknowledged
writes can be lost according to the backend's persistence policy. AOF is not an
independent backup. The backup is a point-in-time snapshot: requests arriving after
its capture remain in live storage, but are not in that archive. A catastrophic
loss of that storage requires producer reconciliation/replay with original event
IDs. Execution pause alone cannot promise zero data loss.

Format **2** supports up to 10,000 keys and 32 MiB of raw key/dump data (48 MiB file).
It preserves binary values and absolute expirations. Restore requires the same
namespace and Redis major version, an empty dedicated database and a trusted
archive. Older archive formats are rejected explicitly; there is no migration,
merge, overwrite, FLUSHDB or Cluster/HA recovery. The bounded atomic storage read
briefly serializes other storage commands; this is not a hot backup implementation.

## PostgreSQL and MySQL/MariaDB snapshots

The same commands support `storage.backend: postgresql` and `storage.backend: mysql`.
SQL archives use data-only format **3** and identify their backend and schema:
SQLite schema 2; PostgreSQL/MySQL schema 1. Restore requires an empty database on
the same backend/schema. A MySQL archive can be used on the supported MariaDB
adapter and vice versa; PostgreSQL/SQLite conversion is not provided. Server
configuration and credentials are not included in the archive.

Both execution queues must be paused and active leases drained. Admission stays
bounded and authorized; capture locks the SQL transaction domain long enough to
obtain a consistent snapshot. Restore commits records, audit, runs and admission
evidence together, then leaves execution paused. A separate Redis queue must have
fresh real/test delivery namespaces; workers reconstruct delivery from the SQL
outbox after explicit reconciliation and resume. Source and destination services
must be stopped for restore; do not run cloned sources in parallel.

Server identity counters can consume unused numbers after a failed transaction.
Failed restore does not commit partial rows; such a gap is harmless and is not
repaired by deleting or reusing identifiers. Restore avoids MySQL `ALTER TABLE`,
which would implicitly commit. All archive size and dedicated-namespace limits
also apply to server SQL snapshots. See [server configuration](backends.md).

## SQLite storage and Redis delivery

SQLite archives use data-only format **3**, preserving schema 2 records, immutable
journal IDs and sequence, runs, admissions, deadlines, pause/capacity settings,
rules, active versions, reports, audit and target locks. They contain no SQL to
execute. The snapshot is one bounded transaction (100,000 rows and 32 MiB of
serialized data; the common file limit is 48 MiB). Exceeding a limit fails without
truncating history. In-flight work must finish or be quarantined before capture.
Admissions may continue after the capture while the archive is published.

Use the same commands below for SQLite. Restore accepts only empty schema 2
storage and rolls back all inserted data on validation failure. Both execution
namespaces remain paused; original absolute expirations are not extended.

With a separate Redis delivery queue, **stop source and destination services** and
configure a fresh delivery database or fresh real/test delivery namespaces before
restore. Any existing delivery key, even an empty stream, makes restoration fail
without modifying SQLite or Redis. Redis messages are transport notifications:
the durable outbox in SQLite is authoritative. After explicit resume, workers
repopulate the fresh queue, including delayed work when due. Old consumer state
is deliberately not imported. No accepted task in the SQLite snapshot depends on
copying a second Redis snapshot at exactly the same instant.

Back up the live Atraxis account database separately with `atraxis accounts …
backup`, along with its operator profiles. For a coordinated recovery point, stop
account administration while capturing both archives. Restoring older account
permissions can re-enable previously revoked access: review current rights before
resuming either engine queue. Never run the source and restored engines together.

## Take a backup

1. Pause new execution and inspect the state:

   ```sh
   atropos storage-pause --config /private/engine.yml
   atropos storage-status --config /private/engine.yml
   ```

   Let in-flight steps finish. Workers can remain running to quarantine expired
   leases; uncertain external effects retain their target locks. Producers can
   continue submitting within queue capacity. Their normal timeouts and retries
   must retain the original event IDs.

2. Create a private directory and capture the snapshot:

   ```sh
   install -d -m 700 /private/backups
   atropos storage-backup --config /private/engine.yml \
     --output /private/backups/engine-20261008.json
   atropos storage-backup-inspect --input /private/backups/engine-20261008.json
   ```

   `storage-backup` also pauses execution itself, and refuses active steps instead
   of waiting indefinitely. It holds an operation fence through capture and file
   publication: competing backups and `storage-resume` are refused. Output is
   mode 0600, fsynced with its directory, and never overwrites a file. SHA-256
   detects corruption; inspection prints metadata, not values or credentials.
   Success or an ordinary error leaves execution paused for explicit resume.

3. Copy the profile, bindings, signing keys, passwords, CA files and test receiver
   secrets **separately** to protected storage. They are not in the storage archive.
   Preserve relative paths and independently check references outside the directory:

   ```sh
   cp -a /private/engine /private/backups/configuration-20261008
   chmod -R go-rwx /private/backups/configuration-20261008
   ```

   Keep the configuration copy paired with the snapshot. These are plaintext local
   archives with credentials, not encrypted or authenticated signatures. Do not
   commit them to GitHub. Use approved protected backup storage and trusted archives.

4. Inspect the result, reconcile any uncertain external work, then explicitly resume:

   ```sh
   atropos storage-resume --config /private/engine.yml --confirm-reconciled
   ```

   This releases the execution pause only. It never clears target locks, changes
   admission limits or automatically retries an uncertain intervention.

If the backup process dies, its operation fence intentionally has no automatic
expiry. Stop/verify that process, read `backup_operation` with `storage-status`,
then release that exact operation before a new backup or resume:

```sh
atropos storage-backup-recover --config /private/engine.yml \
  --operation REPLACE_WITH_REPORTED_OPERATION --confirm-backup-stopped
```

The flag is an administrator assertion, not process discovery. Recovery leaves
execution paused. A partial file is not a valid backup; inspect it before use and
choose a fresh output path for another attempt.

## Restore into fresh storage

Stop original producers, API and workers to prevent two engines acting on the same
targets. Independently inspect external jobs. Provision empty destination storage. For Redis storage, use authentication,
persistence, no eviction and the same Redis major version. For SQLite, use a new
private local database and fresh separate Redis delivery namespaces if configured.
Restore the paired configuration and explicitly change `storage.url` to the new
destination, reviewing credentials, policies, endpoints and signing keys.

```sh
atropos storage-restore --config /private/restored-engine.yml \
  --input /private/backups/engine-20261008.json --confirm-source-stopped
```

Restore preserves admission limits and forces **both execution queues paused**.
Queued/delayed work, results and uncertain locks survive. Redis storage preserves
consumer state; SQLite reconstructs separate delivery from its durable outbox. The
source's temporary backup-operation fence is not restored. Invalid dump payloads,
in-flight runs or inconsistent queue references roll back keys created by the
attempt. A lost connection requires destination inspection; never blindly repeat
an uncertain mutation. Expired ephemeral proofs are not revived.

Review rules, active versions, audit, queues, deduplication and target locks. Start
the service and guarded workers, then resume explicitly after reconciling effects
and event IDs created after the snapshot. Do not manufacture fresh event IDs.
Explicit `resolve` remains required for proven uncertain outcomes and terminates
the run without fabricating results for later steps.

Hot backup without execution pause targets **2.0.0**. External providers,
configuration archive automation, retention/rotation and encryption remain future
work. No exactly-once or zero-data-loss guarantee is made.
