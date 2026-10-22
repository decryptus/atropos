# Explicit dispatch from a rule decision

An authorized operator can preview a rule decision and explicitly submit its
approved workflow. Atropos evaluates the active rule itself, verifies the exact
binding and generation, and persists the run and admission audit together.
Atraxis forwards commands; it neither stores bindings nor interprets conditions.
Activation, `evaluate` and `observe` still execute no workflow automatically.

## Approve a binding in Atropos

Install the current engine with its `service` extra and its separate
client package, as described in [service setup](rule-service.md). Upgrade the
Atraxis client package with the engine: the current contract requires client 0.1.0a3.

The administrator prepares a private mode-0600 YAML file from
[the example](../../examples/dispatch-bindings.yml). Each binding fixes:

- Organization, rule name and exact immutable rule revision.
- Exact result: both `decision` and `matched_rule`.
- Complete approved workflow and static input parameters.
- Maximum admission age before each attempt, from 1 to 300 seconds.

Replace the example revision with a tested, active revision. The complete binding
has a SHA-256 identifier; changing its workflow, parameters or expiry produces a
new identifier. Removing it prevents future attempts of its queued runs. Keep old
bindings only while they remain approved. The file accepts at most 64 bindings,
is bounded to 1 MiB and uses strict YAML parsing. Rules and remote callers cannot
register actions, upload bindings or supply execution destinations/parameters.

Add this optional field to the **Atropos** service profile, relative to that file:

```yaml
dispatch_bindings: dispatch-bindings.yml
```

Without it, no dispatch binding is approved. Existing grants do not gain action
permissions: the initiating identity requires current intervention `read` and
`evaluate` permissions on the rule scope, and `execute` permission covering the
selected resource and **every** action in the workflow. For the supplied example,
add `core.echo` to the explicitly authorized grant's `actions`. Resource/group
membership comes from Atropos's trusted inventory.

List the approved identifiers using the trusted engine-host configuration:

```sh
atropos rules-bindings --config /private/atropos-service.yml
```

This reads configuration without opening rule storage. Share only the binding ID
and intended usage with the authorized operator. Keep all tokens and storage
credentials in their protected files.

## Review, submit and inspect through Atraxis

Use the regular Atraxis remote profile. `facts.json` supplies **manual evaluation
facts**, not workflow parameters or verified live telemetry:

```sh
atraxis rules --remote-config /private/remote.yml dispatch-plan cpu \
  --target container:web-example --input facts.json --binding BINDING_SHA256
```

The response contains the exact workflow, fixed input, target, binding, maximum
age, and current decision/revision/generation. It enqueues nothing. After review,
copy that exact generation and choose a stable event ID:

```sh
atraxis rules --remote-config /private/remote.yml dispatch cpu \
  --target container:web-example --input facts.json --binding BINDING_SHA256 \
  --expected-generation ACTIVE_GENERATION --event-id incident-001 --confirm
atraxis rules --remote-config /private/remote.yml dispatch-status cpu \
  --event-id incident-001
```

The engine reevaluates the facts at submission and rejects a changed generation
or mismatched result. A queued receipt is not completed execution. The API methods
are `plan_dispatch`, `dispatch` and `dispatch_status`; they use the same authenticated
command route. CLI confirmation is a review affordance, not a security boundary.
The server's permissions and approved binding remain mandatory for every caller.
The existing TUI does not yet provide dispatch controls.

## Start a guarded worker

On the engine host, use the **same** Atropos service profile and namespace:

```sh
atropos rules-worker --config /private/atropos-service.yml
# For one available step only:
atropos rules-worker --config /private/atropos-service.yml --once
```

The worker reloads the exact engine credential mapping, current permissions and
resource membership, approved binding, active revision/generation and admission
age before **every step and retry**. It verifies the stored workflow/parameters
against the approved binding and checks its execution lease before dispatch.
If a check fails or becomes unavailable, the attempt starts no action: the run
becomes `cancelled`, records `execution_authorization_denied`, and takes neither a
retry nor an `on_error` branch. Previous successful effects are not rolled back.

The shipped service and worker provide harmless `core.echo` and `core.fail`
actions, plus a `core.alert` simulator whose real delivery is unconfigured. Real Auton actions can be explicitly configured through
[engine-owned execution adapters](execution-adapters.md), with exact adapter
revisions pinned in each approved binding. No real executor is enabled by default. All consumers of a rule-dispatch namespace must use the guarded
worker. The legacy plain worker refuses attributed runs without an authorizer;
it cannot bypass this check. Conversely the guarded worker refuses old unattributed
local runs and signed-admission pilot runs without decision evidence. Keep those
separate trusted experiments in a different namespace.

## Recovery and limits

One event ID identifies a run within subject, organization and rule. Repeating
identical admission returns that run, without a second queue item or audit event;
changed facts/binding/target/generation conflict. Retain event IDs and deduplication
records. This does not make different event IDs equivalent or prove exactly-once
external effects. Neither the API client nor the gateway retries writes.

After a timeout or lost response, query `dispatch-status` with the same event ID.
Current read permission is required; status remains available after deactivation
or binding removal. Do not generate a new event ID just because the response was
lost. Inspect retained state/audit before any new intervention.

Generation validation, enqueue and admission audit share one Redis transaction.
Policies, identity mappings and bindings are trusted files reloaded before dispatch,
not a distributed transactional policy store. A change immediately after the last
check cannot retroactively stop an action. In-flight cancellation, distributed
policy epochs and downstream execution authorization remain future work.
Revoking only the Atraxis frontend token does not cancel an already admitted run:
revoke its dedicated Atropos identity or execution grant to block future steps.

Admission age is measured with Redis time from enqueue, not from sensor sampling.
The workflow can be cancelled between steps when this age expires. No background automatic
collector-to-dispatch, cooldown or incident aggregation is implemented. For explicit
verified-observation dispatch, see [observations and tests](observations-and-tests.md). Manual facts must not be presented as trusted fresh observations.
Timeouts or unknown external results retain `uncertain` state and target ownership;
the existing explicit reconciliation procedure applies, without blind replay.
