# Storage and queue backends

Atropos configures **roles**, not product-specific parameter names. The shipped
storage adapters are SQLite, PostgreSQL, MySQL/MariaDB and Redis. The queue can use the storage adapter's
integrated durable queue or a separate Redis delivery adapter. Additional SQL or
non-SQL adapters can be installed as plugins.

## SQLite storage with Redis delivery

Install `python -m pip install '.[redis]'` when using Redis in either role.
For the rule service, also install the client and service extras described in
[the service guide](rule-service.md). In the protected engine profile:

```yaml
storage:
  backend: sqlite
  url: sqlite:////var/lib/atropos/engine.db
queue:
  backend: redis
  url: redis://127.0.0.1:6379/0
```

The directory must already exist and be writable by the service account. Use a
local filesystem on one host for SQLite; do not place its WAL database on a
network filesystem. Use an absolute path in a daemon profile. A relative URL
such as `sqlite:///atropos.db` resolves against the process working directory.

SQLite intervention searches use a consistent WAL read snapshot, so a search
does not reserve the database writer. Opening an initialized database also
avoids a write reservation. Mutations still use an immediate write transaction.
Current permissions and identity are rechecked after the search snapshot ends
and before results are returned. This requires Sonicprobe 0.3.62; the schema and
configured busy timeout are unchanged.

SQLite persists immutable rule revisions, scenario reports, active generations,
audit events, workflow definitions inside admitted runs, inputs, results, step
history, deduplication and pending execution state. Redis distributes delivery
messages. A Redis outage does not erase accepted work: workers deliver it from
the committed storage state when the queue becomes available again.

## Local setup without Redis

The plain CLI defaults to SQLite at `sqlite:///atropos.db` and the integrated
queue. To choose an explicit file:

```sh
export ATROPOS_STORAGE_BACKEND=sqlite
export ATROPOS_STORAGE_URL=sqlite:////var/lib/atropos/engine.db
export ATROPOS_QUEUE_BACKEND=storage
```

In a service profile, `queue: {backend: storage}` selects the storage adapter's
integrated queue. Omitting the queue block has the same meaning. No separate URL
or options are accepted for this integrated role.

## PostgreSQL and MySQL/MariaDB storage

These adapters use **Sonicprobe AnySQL**, with the same rules, workflow, search,
maintenance and durable-outbox implementation as SQLite. SQLite remains the default.
The validated server targets are PostgreSQL 16, MySQL 8.4 and MariaDB 11.4.
Older major/minor versions are rejected. Install the appropriate driver extra:

```sh
python -m pip install '.[postgresql]'  # PostgreSQL
python -m pip install '.[mysql]'       # MySQL or MariaDB; requires mysqlclient build dependencies
```

Connections request a five-second driver connection timeout. PostgreSQL requires
a Sonicprobe adapter exposing `CONNECT_TIMEOUT_SUPPORTED = True`; an older
adapter is rejected before connecting rather than silently ignoring the timeout.
This does not bound every SQL statement or guarantee a total DNS deadline.

Create a dedicated empty database in UTF-8 first. Give the engine account schema
creation rights for initialization and DML/temporary-table rights for operation.
It never creates or drops the database. For PostgreSQL, the dedicated database
uses the `public` schema; no unrelated objects belong in that schema. For example:

```yaml
storage:
  backend: postgresql
  url: postgresql://engine:REPLACE_WITH_PRIVATE_PASSWORD@127.0.0.1:5432/atropos
queue:
  backend: storage
```

For MySQL **or MariaDB**, use:

```yaml
storage:
  backend: mysql
  url: mysql://engine:REPLACE_WITH_PRIVATE_PASSWORD@127.0.0.1:3306/atropos
queue:
  backend: redis
  url: redis://127.0.0.1:6379/0
```

Keep credentials in the private engine profile. Percent-encode URL credentials.
Without TLS, the server must be reached on loopback (`127.0.0.1`, `::1` or
`localhost`). Remote SQL requires explicit verified TLS through AnySQL:

```yaml
storage:
  backend: postgresql
  url: postgresql://engine:REPLACE_WITH_PRIVATE_PASSWORD@sql.example.net:5432/atropos
  options:
    tls:
      ca: /etc/atropos/tls/server-ca.pem
      # Optional client identity; supply both files when the server requires it:
      # cert: /etc/atropos/tls/client.pem
      # key: /etc/atropos/tls/client.key
```

For MySQL/MariaDB use `backend: mysql` and a `mysql://` URL with the same TLS
options. Certificate paths must be absolute, readable by the engine account, and
point to PEM files. Keep the client private key readable only by that account.
The URL host must match the server certificate; MySQL TLS rejects `localhost`
because its driver can select a Unix socket. Use the certificate's DNS name or
IP address. The CA chain, validity period and server name are always verified.
Missing files, an invalid certificate or an unencrypted server stop the connection;
there is no insecure fallback or automatic reconnect. Restart the service after
changing certificate files or configuration. TLS does not change SQL permissions.

TLS belongs in `options.tls`, not URL query parameters. Unknown settings,
incomplete client identities, relative paths and native driver TLS overrides
are rejected before connecting.
Only an optional `mode=rw` or `mode=rwc` is accepted; both mean write access to an
already-created database. `mode=ro` is rejected because the engine owns admission,
leases and maintenance writes. This does not change Sonicprobe's RO behavior.

PostgreSQL requires UTF-8 and `fsync=on`; the connection requests synchronous
commits. MySQL/MariaDB require InnoDB and `innodb_flush_log_at_trx_commit=1`.
The adapter selects strict SQL checks, binary comparisons and read-committed
transactions. All engine writers lock one shared database row for each transaction.
This preserves queue-capacity, deduplication and target-exclusion invariants across
processes, at the cost of serializing mutations. Worker actions and Redis delivery
happen outside that transaction. Server time supplies lease and proof-expiry checks.
A dropped connection or uncertain commit is surfaced; no statement is replayed.

New server databases use schema **1**, with `CHAR(64)` run IDs, `CHAR(32)` revision,
delivery and claim tokens, native journal auto-increment/identity, sign and status
constraints, foreign keys and immutable admission/run identity checks. PostgreSQL
uses positive `BIGINT` constraints; MySQL/MariaDB use `BIGINT UNSIGNED` bounded to
the common journal range. Indexed admission columns serve search without decoding
run payloads. Unsupported or partially initialized schemas are rejected without
cleanup or migration; retain that database and inspect the failure before using a
new destination. MySQL DDL is not transactional, so failed initialization can leave
an explicitly rejected partial schema.

## Redis storage

Existing Redis storage remains supported when selected explicitly:

```yaml
storage:
  backend: redis
  url: redis://127.0.0.1:6379/0
queue:
  backend: storage
```

Redis requires a dedicated standalone 6.2+ instance with persistent storage and
no eviction. Remote Redis URLs must use verified `rediss://` TLS. Redis Cluster
is not supported. A separate delivery queue can also be selected with Redis
storage; the storage stream remains the durable admission outbox.

## Configuration rules

Both roles accept `backend`, optional `url`, and optional `options`. Each adapter
validates its own URL and options. SQL server adapters accept the explicit `options.tls` block above; other
bundled adapters reject nonempty options. CLI equivalents are `ATROPOS_STORAGE_BACKEND`, `ATROPOS_STORAGE_URL`,
`ATROPOS_STORAGE_OPTIONS` and the matching `ATROPOS_QUEUE_*` names. Options use a
JSON object. Plugin configuration does not require a URL if its adapter uses
other options.

Product names appear as **adapter values**, never as parameter prefixes. The
former `redis_url` and `ATROPOS_REDIS_URL` spellings are rejected. Set the storage
backend explicitly when retaining an existing Redis deployment. Changing a
backend does not migrate data; a different destination starts with its own
state. No stored data is deleted or automatically converted. Running services
require a restart after changing either role's destination or options.

Execution pause/resume and backup/restore support the bundled storage adapters. SQL
archives preserve the durable outbox and work with integrated delivery or Redis
delivery. Restoring with Redis delivery requires fresh real/test delivery
namespaces; workers rebuild delivery from the restored SQL state. Redis storage
snapshots support its integrated queue only. See [backup and restore](storage-backup.md).
Cross-backend conversion and prototype-schema migration are not provided.

All delivery is at least once. Storage alone grants a fenced execution claim;
expired in-flight work becomes uncertain and keeps target exclusion until an
operator reconciles it. No backend promises exactly-once external effects.


## Relational SQLite format 2

Current SQLite databases use schema 2. Fixed hexadecimal identifiers have exact
length constraints, numeric values reject invalid negative values, statuses are
checked and admissions reference their durable runs and journal evidence.
Run IDs are consistently 64 hexadecimal characters. Journal IDs auto-increment.

The previous schema 1 prototype is rejected without modifying its contents.
Preserve the original database and use its matching application version to inspect
or export it. This version does not supply an in-place migration or authorize
reinitializing an existing file. New databases must use a separate destination.
Existing files must be private, owned regular files (0600), with no hardlinks.

Connections use Sonicprobe AnySQL with automatic replay disabled. Account membership reads continue to use SQLite in explicit read-only mode.
The engine SQL adapters described above are separate from that membership source.

## Live Atraxis membership

The engine can read the same live Atraxis schema 3 account database without
importing Atraxis:

```yaml
membership:
  backend: sqlite
  path: /srv/atraxis-accounts/directory.sqlite3
```

API and worker profiles must identify the same authority. Mount the private account
directory read-only and keep compatible file ownership. Each check reads the current
committed projection; disabled/removed membership is not cached. A missing database,
unsupported schema or read failure denies access. The existing `file` backend remains
available for explicit JSON deployments. Never mix a live store with stale copies.
