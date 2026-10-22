# Notify through Alertmanager

Atropos can send an intervention notification to an explicitly approved
Alertmanager v2 endpoint. This is opt-in: existing configurations and `core.alert`
keep their behavior. Alertmanager owns routing, grouping and downstream delivery.
Use engine 0.1.0a17 or later with the service extra; no new client API is required.

Add a named action to the same private engine profile used by the API and worker:

```yaml
execution_adapters:
  ops.notify:
    backend: alertmanager
    target: demo/container/web
    origin: https://alerts.example
    token_file: secrets/alertmanager.token
    # Optional private CA, with certificate and hostname verification:
    # ca_file: secrets/alertmanager-ca.pem
    labels:
      alertname: InterventionComplete
      severity: info
    timeout: 5
    lifetime_seconds: 300
    simulation: test_receiver
```

The target must exist in the engine inventory. The bare HTTPS origin cannot
contain a path, query, fragment or credentials. An explicit
`allow_loopback_http: true` is permitted only for numeric loopback test endpoints.
There is no insecure TLS option, redirect following, environment proxy or retry.
Use an authenticated ingress that accepts the dedicated bearer token; do not
expose an unauthenticated remote Alertmanager. Tokens follow the API token format
(43–128 ASCII letters, digits, `_` or `-`). Protect profile, token and optional CA
files with mode 0600. Relative paths resolve beside the engine profile.

Labels are administration-owned: 1–32 labels, including a nonempty `alertname`.
Names use ASCII letters, digits and underscores and cannot start with a digit,
`__` or `atropos_`. Values are nonempty printable strings up to 1024 characters.
Only `subject` and `description` may be supplied by the approved workflow, each a
nonempty string up to 4096 characters. Workflow inputs cannot choose routing
labels, destination, token or transport settings.

Grant the concrete action to the operator, obtain its revision with
`atropos execution-adapters --config /private/engine.yml`, and pin it under
`adapter_revisions` in the approved binding, just like the
[Auton executor](execution-adapters.md). Append a step after the intervention:

```yaml
notify:
  action: ops.notify
  timeout: 5
  with:
    subject: Intervention completed
    description: The approved intervention step completed successfully.
```

The previous step's `next: notify` schedules it after confirmed success. Use a
separate approved step on `on_error` for a confirmed failure message. Uncertain
outcomes never follow success or error branches. Current permissions, binding,
active generation and observation freshness are checked before the alert step
as for every intervention step. An expired proof can therefore cancel an alert
after an earlier action succeeded; inspect the intervention history.

Changing destination, labels, target, token/CA path or simulation policy changes
the adapter revision, requiring reviewed bindings and a worker restart. Old
queued work is never silently redirected. Token contents may rotate in place.

## Acceptance and uncertainty

The adapter posts one alert to `/api/v2/alerts`. Reserved labels identify the
canonical target and a stable run/step delivery ID; annotations identify the run,
step, summary and description. `startsAt` and `endsAt` bound its lifetime to
`lifetime_seconds` (1–3600, default 300). There is no periodic refresh or automatic
resolve request. Configure Alertmanager grouping delays and notification routing
with this finite lifetime in mind. A stable fingerprint is not an exactly-once
notification guarantee.

HTTP 200 records `alertmanager_accepted`. This means API acceptance, not email,
webhook or human receipt. The adapter bounds the response to 4096 bytes and the
attempt to the smaller of the workflow timeout and configured timeout (1–30
seconds). A lost reply, timeout, redirect, non-200 or oversized reply leaves the
run `uncertain`, preserves its target lock and is never automatically retried.
Inspect Alertmanager using `atropos_delivery_id` before resolving the run.
Credentials and response bodies are never retained in the receipt.

## Test isolation

`simulation: test_receiver` registers a separate simulator. By default it returns
a labelled local alert. If the engine has an approved `test_alert_delivery`, it
uses the [dedicated test receiver](test-alert-delivery.md), its token, fixed
`/v1/test-alerts` route and matching acknowledgement contract. It never contacts
the real Alertmanager origin or reads its token. Omitting simulation, or selecting
`unsupported`, refuses test admission. The normal `core.alert` remains unchanged.

The same immutable marker crosses the test run, Auton noop result and alert.
A test receiver must preserve it through downstream hops. It is not a standard
Alertmanager API endpoint. A simulation validates orchestration, not execution
of the remote Auton program or delivery to a real channel.
