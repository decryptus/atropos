# Verified observations and end-to-end tests

Atropos can require observations collected by Atraxis before admitting a workflow.
Administrators can also test a complete workflow with simulated actions and a
labelled alert. These uses have separate permissions and execution paths.

| Operation | Input | Queue and effects |
| --- | --- | --- |
| `simulate-dispatch` | A scenario file | Rule evaluation and workflow preview only; no queue or action |
| `test-dispatch` | A scenario file | Isolated test queue; registered simulators only; labelled simulated alert |
| `observe-dispatch-plan` | Current authenticated inventory | Preview only; creates a short-lived verification challenge |
| `observe-dispatch` | Newly collected current inventory | Confirmed admission to the normal guarded queue |

Existing activation and `observe` remain non-dispatching. No background automatic
intervention has been enabled. Real actions require explicit [Auton](execution-adapters.md) and
[Alertmanager](alertmanager.md) configuration and reviewed binding revisions.

## Require collected observations

Install engine 0.1.0a20 and client 0.1.0a10 with the `observations` extra.
The engine `service` extra and Atraxis `rules` extra select that optional signing
support. The base API client still has no rule engine, Redis or ZEN dependency.

Keep the existing Atraxis authenticated inventory client and exact logical-target
to source/full-Docker-ID mapping. Configure a dedicated signing key on the trusted
Atraxis gateway, and only its public key on Atropos. For example, on a protected
host directory:

```sh
umask 077
openssl genpkey -algorithm ED25519 -out observation-private.pem
openssl pkey -in observation-private.pem -pubout -out observation-public.pem
```

These commands create a new key pair; do not overwrite an existing key during
routine setup. Protect both files with mode 0600. Provision the public key through
your trusted administration channel. Never give the private key to terminal users
or put it in Git, command arguments, diagnostics or a rule file.

In the Atraxis gateway profile, paths are relative to that profile:

```yaml
observation_signing:
  key_id: gateway-1
  private_key_file: observation-private.pem
```

In the Atropos service profile:

```yaml
observation_keys:
  gateway-1: observation-public.pem
```

Add this optional requirement to the engine-owned approved dispatch binding:

```yaml
observation:
  key_id: gateway-1
  source_id: monit
  native_id: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  filesystem: null
  max_age_seconds: 60
```

Replace the synthetic Docker ID with the exact approved container ID. Filesystem
checks need the same explicit `[group, path]` on both sides instead of `null`.
The requirement changes the immutable binding ID: list it again with
`atropos rules-bindings --config /private/atropos-service.yml`.

A binding with this requirement rejects the ordinary manual `dispatch` and
`dispatch-plan` calls. Tests can still use simulated values, but only in test mode.
Bindings without an observation requirement retain their explicit manual behavior.

## Preview and confirm using current measurements

```sh
atraxis rules --remote-config /private/remote.yml observe-dispatch-plan cpu \
  --target container:web-example --binding BINDING_SHA256
atraxis rules --remote-config /private/remote.yml observe-dispatch cpu \
  --target container:web-example --binding BINDING_SHA256 \
  --expected-generation REVIEWED_GENERATION --event-id incident-001 --confirm
```

The preview displays the projected facts, source, full container identity,
workflow and active generation. Confirmation fetches observations again. If the
source is unavailable, historical, stale, ambiguous or different, or if the rule
changed, admission is refused. A terminal cannot supply facts, source metadata,
signing keys or signed envelopes to these public gateway operations.

Atropos first issues a random challenge bound to its authenticated actor, target,
rule revision, generation and binding. After fetching inventory, the gateway signs
the challenge, facts, source identity and snapshot age. Atropos checks the signature
against its locally configured key and the exact approved source before evaluation.

The challenge lasts at most 60 seconds, measured with Redis time. There is one
outstanding challenge per engine credential: a concurrent new preview/confirmation
invalidates the previous challenge rather than growing unbounded temporary state.
Retry a failed preview explicitly; never blindly retry a confirmed dispatch.
Successful admission consumes the challenge in the transaction that enqueues and
audits the run. Use `dispatch-status` with the original event ID after a lost reply.

Effective age conservatively includes the snapshot age, inventory transport and
the time since the challenge was issued. Remaining lifetime is bounded by the
snapshot limit, the binding's limit and the challenge lifetime. This can reject
slow requests early; it never grants more freshness because of gateway clock drift.
The worker verifies the retained signature, current key, source binding and expiry
again before each step/retry, in addition to existing permission and version checks.

The signature attests what a trusted gateway collected through its configured
inventory connection. It is not a signature from monit-docker's hardware, proof
against a compromised collector, or a substitute for verified HTTPS and trusted
source configuration. Signed measurement evidence is retained in protected run
storage; receipts expose only a summary, not signatures or credential references.

## Administrator tests without execution rights

A scoped `read` and `test` grant permits simulation; an end-to-end test additionally
requires that grant to cover every tested action. It does **not** require `execute`.
Permissions remain per organization, domain and resource/group. Granting test
rights does not grant intervention rights.

For a pure preview, supply scenario facts such as `{"cpu_percent":95}`:

```sh
atraxis rules --remote-config /private/remote.yml simulate-dispatch cpu \
  --target container:web-example --input scenario.json --binding BINDING_SHA256
```

This evaluates the binding's exact saved revision, including a draft revision,
and shows whether its expected decision matches. It does not enqueue, run actions,
activate a rule or create passing scenario evidence for activation. It cannot
predict external action outcomes.

For the complete internal workflow path:

```sh
atraxis rules --remote-config /private/remote.yml test-dispatch cpu \
  --target container:web-example --input scenario.json --binding BINDING_SHA256 \
  --event-id admin-test-001
atraxis rules --remote-config /private/remote.yml test-dispatch-status cpu \
  --event-id admin-test-001
```

The test command stamps an immutable context such as
`{"test":true,"id":"..."}`. That same context accompanies the source event,
decision, queued run, action context, step history, results and simulated alert.
The caller cannot turn it off or change the test ID. Test event IDs have their own
idempotency scope. A nonmatching decision is reported without queueing a workflow.

Start a separate worker explicitly in test mode:

```sh
atropos rules-worker --config /private/atropos-service.yml --test
```

It uses an isolated namespace derived from the service namespace. Its queue,
leases, target locks and results are separate from normal work. Test admission is
recorded in the rule audit with its test context. Existing rule revisions and
activation evidence are unchanged. Use the same private service profile for the
API and worker so identity, binding and policy revocation apply to future steps.

Every action must supply a registered simulator. There is no fallback to its real
implementation. The real worker refuses test runs, and the test worker refuses
unmarked runs. Current built-ins simulate `core.echo`, `core.fail` and `core.alert`.
The alert simulator returns a `[TEST]` subject, the shared test context and
`delivery: simulated`. A failure branch can therefore be tested through to its
alert as well as a success branch. See [the test workflow example](../../examples/test-alert-binding.yml).

**By default, no notification is sent.** You can explicitly configure a
[dedicated test receiver](test-alert-delivery.md) to acknowledge test alert delivery
while retaining the same marker. Direct Alertmanager/email integration remains
separate. Configured Auton actions can register an explicit
[local noop simulator](execution-adapters.md); it never contacts the daemon or
models the remote program. The real Auton adapter still refuses test contexts.
Additional adapters must
preserve this context, isolate test delivery destinations and refuse unsupported
test mode; dropping the flag must never promote a test to a real intervention.

As with other workflows, a revoked permission/binding or an expired run cancels
future attempts without undoing previous steps. An uncertain simulated outcome
is retained for inspection in the test namespace. No production deployment or
real intervention is configured by these additions.


## Discover test bindings

The API client's `test_bindings(name, target)` lists only metadata authorized
for the current account and concrete server/container target. Each row names
its workflow, revision, actions and simulator availability. An empty list never
broadens scope. Selection does not bypass later authorization or guarantee that
configuration remains unchanged; submit and worker checks still apply. See the
[independent client guide](../../client/README.md#discover-authorized-test-workflows).
