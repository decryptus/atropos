# Deliver alerts to an explicit test receiver

Atropos 0.1.0a8 can send a test workflow's `core.alert` output to a dedicated
HTTP test receiver. Intervention actions remain simulated. Without this optional
configuration, alerts remain local results with `delivery: simulated`.

Configure only Atropos. Atraxis users select the approved rule/binding and test
event; they cannot supply a receiver URL, token or transport through a rule input.
The normal worker still refuses real `core.alert` delivery.

## Configure the test worker

Add to the private Atropos service profile:

```yaml
test_alert_delivery:
  backend: webhook
  url: https://test-receiver.example
  token_file: test-receiver.token
  timeout: 5
```

The URL is the receiver's base URL, with no path, query, fragment or credentials.
Atropos posts to `/v1/test-alerts`. HTTPS verifies the certificate and hostname
using the system trust store. From engine 0.1.0a18, optional `ca_file` selects a
private PEM CA bundle (maximum 1 MiB), resolved beside the service profile. Keep
it a private regular file, mode 0600, owned by the worker user or root. It is read
and validated before each send; an invalid or unreadable bundle is a known
pre-send refusal. Changing its configured path requires a worker restart; replacing
its contents at the same protected path rotates trust on the next attempt.
`ca_file` cannot be combined with loopback HTTP. There is no switch to disable verification and no
redirect following, environment proxy or automatic retry. `timeout` is an integer
from 1 to 30 seconds; the workflow step's timeout can shorten it.

Provision a dedicated receiver bearer token in a private regular file, mode 0600,
owned by the worker user or root. Token files use the same format as API tokens:
43–128 ASCII letters, digits, `_` or `-`, with an optional trailing newline. Use a
generated secret, never an example value, and configure the receiver to accept it
only on its test endpoint. Relative paths resolve beside the service profile.
Tokens are reloaded before each attempt and never returned in delivery receipts.

For a disposable local receiver only, a literal loopback IP may use HTTP with
`allow_loopback_http: true`. Remote HTTP and the hostname `localhost` are rejected.
Keep the existing scoped `read`/`test` action grants and run:

```sh
atropos rules-worker --config /private/atropos-service.yml --test
```

The worker pins its delivery configuration at startup. Adding, removing or
changing that configuration stops affected attempts before sending; restart the
worker explicitly after reviewing the new destination. Rotating the token in the
same protected file does not require a restart. A restart applies the configured
receiver to pending test work as well, so review pending work before rerouting.

## Receiver contract

The JSON request includes `version: 1`, `test_context: {test: true, id: ...}`,
`delivery_id`, `run_id`, `step`, `target` and an `alert`. The alert retains the same
test context and a subject prefixed `[TEST]`; its payload describes simulated
interventions. Its `delivery: simulated` field describes that original local
alert result, not proof of downstream delivery.

Headers include `Authorization: Bearer ...`, `X-Atropos-Test: true`,
`X-Atropos-Test-ID` and `Idempotency-Key`. The latter equals `delivery_id`, a stable
identifier for this run/step. Values inside the alert payload are data only: a
receiver must never interpret them as destination, credential or execution
overrides.

A compatible receiver must authenticate the sender, reject missing/inconsistent
test markers, and accept only into an explicitly authorized test inbox or test
channel. It must preserve the marker through further hops, deduplicate the stable
delivery ID durably, and reject reuse of that ID with different content. Once it
has accepted responsibility, it responds HTTP 200 or 202, Content-Type
`application/json`, with exactly these fields (substitute the received IDs):

```json
{
  "version": 1,
  "accepted": true,
  "test_context": {"test": true, "id": "SAME_TEST_ID"},
  "delivery_id": "SAME_DELIVERY_ID"
}
```

The response must be no larger than 4096 bytes. A generic HTTP 200, absent marker,
different ID, duplicate JSON field or redirect is not an acknowledgement.
Atropos returns `delivery: test_receiver_acknowledged`, the shared test context
and delivery ID. This confirms the receiver's acknowledgement; it does not prove
an email was read or a downstream messaging service delivered it.

## Failure and isolation

Invalid configuration, an unreadable token or changed destination is refused
before sending. A missing/invalid acknowledgement, timeout or lost connection
leaves the test run `uncertain`. Atropos keeps its test target lock and does not
retry or follow an error branch, because the receiver may already have accepted
the alert. Inspect the receiver using the test ID and step before resolving the run;
do not submit a new event simply to resend it.

The parent terminates and reaps the transport process on timeout/cancellation.
Stopping a sender cannot revoke an already accepted request. Test queues and
locks remain separate from normal work; normal interventions never become test
deliveries through this configuration.

The receiver and its test-only downstream routing are trusted administration
boundaries. Atropos cannot detect a receiver misconfigured to forward into a real
channel. A standard Alertmanager API, Slack webhook or email endpoint does not
implement this acknowledgement contract directly: an appropriate test adapter is
still required. No production receiver or messaging account is provisioned by
this feature. The existing Auton adapter still refuses test execution.
