# Shared queue admission capacity

Atropos limits outstanding work per namespace. The default is **1,000 entries**.
All upgraded producers read the same stored limit inside the admission
transaction. This includes API-driven dispatch, administrator test dispatch and
host-side workflow submission; a process-local setting cannot override it.

The budget is the sum of stream entries (waiting or currently owned by a
worker), delayed entries and quarantined uncertain runs. Normal transitions
preserve one budget unit per unfinished run. Duplicate/stale transport entries
may conservatively consume extra capacity until processed; the budget is not a
count of unique business events. Historical terminal snapshots are not counted.

When full, a **new** run is refused before its state, queue entry, admission audit
or observation proof consumption is committed. An identical retained
idempotency key still returns its existing receipt, subject to the existing
admission/authorization checks. No accepted task or uncertain target lock is
removed to make space. Workers continue processing accepted tasks even when
admissions are paused. Completion/cancellation releases capacity; an uncertain
run retains capacity until its outcome is explicitly reconciled.

## Inspect and configure

These are trusted engine-host administration commands. Use the engine's storage
connection through `ATROPOS_STORAGE_URL` and the correct namespace:

```sh
atropos --namespace production queue-status
atropos --namespace production queue-configure --max-outstanding 500
atropos --namespace production queue-status --test
atropos --namespace production queue-configure --test --max-outstanding 100
```

`--test` selects the same isolated namespace as the administrator test worker.
Real and test namespaces have independent limits. Configuration is stored
centrally and survives storage restart. Valid limits are integers from 0 to
1,000,000. **Zero pauses new admissions**; lowering a limit below current usage
preserves all accepted work and blocks additions until capacity is available.

Status reports `max_outstanding`, `pending_entries`, `delayed_entries`,
`uncertain_runs`, `outstanding`, `available` and `full`. It is an atomic snapshot,
not a reservation. No queue or limit key is created by reading an empty namespace.
Malformed stored limit data refuses new admission rather than falling back to an
unbounded queue; a trusted administrator must repair it explicitly.

## API and operator feedback

The rule API reports HTTP 429 with `rule_queue_full`. The matching client verifies
the service identity header and bounded error envelope before treating this as a
known refusal. An ambiguous/proxy 429 remains an unknown mutation outcome. Neither
case is retried automatically. Atraxis CLI reports the fixed code; its Textual
test flow states that the new test was not admitted. Choose a fresh explicit
attempt only after reviewing capacity and retained event status where necessary.

## Upgrade and limits

Upgrade **all admission producers** before relying on this budget: old binaries
can bypass a policy they do not implement. Use engine 0.1.0a11 and client
0.1.0a6 with the matching Atraxis gateway. Existing queues need no new run index
or destructive migration; their stream/delayed/quarantine entries count immediately.
If they exceed the default, accepted work remains and new admission is refused.

This is a count budget, not a byte quota, per-user quota or global worker
concurrency limit. Rule versions, terminal run history and audit records still
need storage sizing, backup and a reviewed retention/archive policy. Nothing is
automatically expired or purged by this feature. Redis standalone is supported;
Cluster/HA and production throughput are not claimed.

## Maintenance is separate from capacity

For backup maintenance, use [execution pause](storage-backup.md). It leaves
admission capacity unchanged, keeps authorized arrivals in the persistent queue,
and blocks new step claims until explicit resume. A zero capacity remains a
separate choice to refuse new admissions. Restored queued work is subject to the
same current authorization and freshness checks before execution.
