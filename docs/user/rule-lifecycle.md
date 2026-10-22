# Drafts, tests and active rule versions

This Python API manages decision rules. The CLI catalog remains available. Atraxis now supplies a separate
[local operator CLI and TUI](https://github.com/decryptus/atraxis/blob/main/docs/rule-console.md);
these controls now call the independent [Atropos HTTP service](rule-service.md).
A web editor remains unimplemented.
An active version is selected for explicit `evaluate_active` calls. Activation
alone does not start a collector, send alerts, submit jobs or execute actions.

## Contents

- [Configure the application boundary](#configure-the-application-boundary)
- [Create, test and activate](#create-test-and-activate)
- [Edit, stop and roll back](#edit-stop-and-roll-back)
- [Scopes and permissions](#scopes-and-permissions)
- [History and limits](#history-and-limits)

## Configure the application boundary

Use the source checkout with its `zen` extra. Supply an explicit Redis client,
an authenticated actor and a loader returning the current trusted `AccessPolicy`:

```python
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.rule_lifecycle_store import RedisLifecycleStore
from atropos.rule_repository import RedisRuleRepository

repository = RedisRuleRepository(redis_client, organization='org')
lifecycle = RuleLifecycle(repository, RedisLifecycleStore(repository), policy_loader)
# Derive these values from authentication, never from an uploaded rule or form.
actor = Principal(authenticated_subject, authenticated_organization)
```

The application must isolate organizations and configure policy/inventory
freshness. Low-level repository/store access is trusted administration, not an
authorization boundary. The separate HTTP service supplies token-based identity mapping; a policy
administration UI is not yet supplied. Existing standalone rules without scope metadata must be
imported under a new rule name as a scoped draft before lifecycle activation;
there is no implicit privilege to migrate an unscoped existing history.

## Create, test and activate

```python
from atropos.rule_catalog import instantiate

draft = lifecycle.save_draft(
    actor, instantiate(['cpu-high'], name='cpu'),
    rule_kind='intervention', selections=[('container_group', 'webapps')],
    expected_revision=None,
)
report = lifecycle.test(actor, 'cpu', draft['revision'], [
    {'name': 'High CPU', 'input': {'cpu_percent': 95},
     'expected': {'decision': 'review', 'matched_rule': 'combined'}},
    {'name': 'Normal CPU', 'input': {'cpu_percent': 20},
     'expected': {'decision': 'ignore', 'matched_rule': '__default__'}},
])
state = lifecycle.active(actor, 'cpu')
active = lifecycle.activate(
    actor, 'cpu', draft['revision'], test_id=report['id'],
    expected_generation=state['generation'],
)
result = lifecycle.evaluate_active(
    actor, 'cpu', ('container', 'web-01'), {'cpu_percent': 95},
)
```

Choose a target actually included in the draft's resolved scope. Testing evaluates
synthetic observations and compares exact expected decisions through real ZEN.
It runs no intervention. Activation requires the **latest test report for that
exact revision** to pass, with matching content, scope and compiler fingerprints.
An earlier successful report cannot override a newer failed report. A successful
scenario suite is evidence for those scenarios, not proof of every possible case.

`import_draft(actor, yaml_text, format='yaml', rule_kind=..., selections=...,
expected_revision=...)` supports the same validated authoring schema in YAML/JSON.
Imports require both `import` permission for provider `upload` and draft/save
permissions for provider `internal`. No URL or remote provider is fetched.

## Edit, stop and roll back

Editing creates a new immutable draft using `save_draft(...,
expected_revision=previous_draft_revision)`. The current active revision remains
unchanged. Changing scope also creates a new revision and needs a new test report.

Every activation/deactivation/rollback changes a unique **generation**, even when
returning to the same revision. Pass the generation last read from `active`.
A concurrent change rejects the operation; refresh and review before retrying.
This prevents a stale request from overwriting intervening changes.

```python
stopped = lifecycle.deactivate(actor, 'cpu', expected_generation=active['generation'])
# Reactivation remains explicit and requires the latest passing test report.
reactivated = lifecycle.activate(
    actor, 'cpu', draft['revision'], test_id=report['id'],
    expected_generation=stopped['generation'],
)
# A rollback selects a previously active immutable revision; it does not edit it.
back = lifecycle.rollback(
    actor, 'cpu', older_revision, test_id=older_latest_passing_report,
    expected_generation=reactivated['generation'],
)
```

Rollback requires `rollback` and `activate` permission and a passing report for
the selected previously active revision. It does not change the draft head.
Restoring an old document via the low-level repository creates a **new draft**,
retains its scope metadata and never activates it; the new revision must be tested.
Deactivation stops future explicit evaluations. It does not cancel running jobs;
this lifecycle does not dispatch jobs in the first place.

## Scopes and permissions

Selections may be servers, containers, server groups or container groups. A
server selection picks that server; a server-group selection resolves member
servers. To target containers, select containers or container groups explicitly.
A container group can span hosts, without adding those hosts to the selection.

Membership comes only from trusted inventory. Each draft freezes the resolved
resource identities (maximum 128); later group expansion does not silently add
targets to an active version. The policy loader rechecks current grants and
inventory on each operation. Losing group membership can therefore remove access
to a previously selected resource. Empty/unknown groups fail validation.

| Operation | Required permission |
| --- | --- |
| New draft | `create` and `save` to `internal` |
| Edit draft/scope | `update` and `save` on both previous and new scopes |
| Read rule/report/history | `read` |
| Run synthetic scenarios | `test` |
| Activate exact version | `activate` on new and previously active scope |
| Roll back | `rollback` and `activate` on both scopes |
| Deactivate | `deactivate` on the active scope |
| Evaluate active decision | `evaluate` on its frozen scope, with an included target |

`evaluate` is a new permission distinct from action `execute`. Decisions do not
bypass the registered-action and execution permissions in the workflow service.
All three domain labels (supervision/intervention/alerting) retain independent
permissions, but this API handles the shared ZEN model only; it does not generate
PromQL, native monit-docker configuration or Alertmanager routes.

## History and limits

`history` returns immutable revision metadata, `test_report` retrieves exact test
evidence, and `audit` returns active-version changes with actor, time, prior
version and generation. History/audit pages filter out entries the actor cannot
read; a page can consequently be empty. Pagination is over underlying entries.
Test reports retain supplied scenarios and results in trusted Redis; use synthetic
facts and apply appropriate storage access/retention policies.

Tests accept 1–100 cases and 64 KiB of scenario data. Active state and its audit
event commit atomically in Redis. A newer test report racing activation causes a
conflict. Evaluation discards its result if the active generation changes while
ZEN runs, and checks permissions again before returning.

Policy checks use the loader's current snapshot. Distributed policy epochs,
strongly atomic policy revocation with Redis writes, authoritative inventory
freshness, HTTPS identity integration and execution-time dispatch bindings remain
future work. A removed resource may require trusted inventory/policy repair before
its old scope can be deactivated. No privileged bypass is provided implicitly.
Redis loss/rollback can lose lifecycle evidence; use persistence and tested backups.

## Draft head and authorized export

`draft(actor, name)` reads the latest saved revision after scope authorization.
`export_yaml(actor, name, revision)` requires both read permission and export
permission for provider `download`. It emits only the authoring document; scope
and grants are not exported as implicit authority. The existing low-level
repository export remains a trusted storage API.
