# Atropos rule service

Atropos owns rule templates, parsing, validation, scenario tests, immutable
revisions, active versions, evaluation and the rule lifecycle audit. Atraxis is
an API client and user interface; it does not open this storage or run ZEN.
Atropos also owns the workflow runtime. [Explicit dispatch](rule-dispatch.md)
can submit an approved workflow after review; activation and evaluation do not
submit workflows automatically.

## Install and start

In a dedicated virtual environment with the system libmagic library installed,
install the published service extra. It includes the matching independent client:

```sh
python -m pip install 'atraxis-atropos[service]==1.0.0'
# Add [service,redis] instead when selecting a Redis storage or queue backend.
atropos rules-serve --config /private/atropos-service.yml --port 8082
```

Copy [the example service profile](../../examples/rule-service.yml), replace its synthetic inventory, grants, storage
URL and token digest, and protect it with mode 0600 in an administrator-owned
directory. [Storage and queue adapters](backends.md) can be selected independently.
SQLite and Redis storage are supplied; the old `redis_url` spelling is rejected.
Keep the namespace stable to reuse existing rules, versions, tests and audit.

The listener binds only to `127.0.0.1`. Remote callers require a trusted local
HTTPS reverse proxy forwarding `POST /v1/rules/command`. Protect that proxy with
bounded body/connection deadlines and admission controls. Do not expose this
listener through a plain HTTP tunnel. The loopback hop and local host are trusted.
The service does not provision the proxy, certificates or production infrastructure.

## Identity, authority and delegation

Each `identities` entry maps a random bearer-token SHA-256 digest to one subject
and organization. Atropos enforces its current authoritative grants and trusted
resource inventory for every lifecycle operation. Request bodies cannot grant
rights or change the authenticated actor. A container-group grant never implies
access to its whole host. Editing does not imply activation or execution rights.

Generate a random token in a protected directory (never use a password digest):

```python
import hashlib, os, secrets
token = secrets.token_urlsafe(32)
fd = os.open('atropos-access.token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(token + '\n')
print(hashlib.sha256(token.encode('ascii')).hexdigest())
```

Provision the digest into Atropos; provision the token file into the trusted
Atraxis gateway's per-user client profile through your secure credential channel.
The user's terminal uses a separate Atraxis token. **Do not share one administrator
Atropos credential across users.** The gateway sends an `expected_actor`
subject/organization precondition on every engine request; a mismatched token is
rejected before any business operation. It cannot impersonate that actor merely
by supplying the precondition.

Token mapping and policy files reload at each request and lifecycle authorization
boundary. Remove a digest to revoke it. Replace configuration atomically; a change
of storage destination/namespace requires restart. There is no SSO, automatic
expiry, automated rights synchronization or distributed policy epoch yet. Manual
per-user credential provisioning is the current delegation adapter. An already
authorized operation can finish after a concurrent revocation at its final check.

## API

The shared `atropos-client` contract exposes catalog, create-from-template,
save/import draft, read/latest draft, export, history, test/report, active,
activate/rollback/deactivate, audit and pure active evaluation. These delegate to
the engine-owned `RuleApplication` and `RuleLifecycle` services. See the
[lifecycle contract](rule-lifecycle.md) for permissions and evidence requirements.

```json
{"method":"active","arguments":{"name":"cpu"},"expected_actor":{"subject":"alice","organization":"example"}}
```

The authenticated token remains authoritative; `expected_actor` is optional for
direct trusted clients and required by the Atraxis gateway implementation.
A success is `{"result": ...}` with `X-Rule-Service: atropos`. Scenario failures
return successful reports with `passed: false`. HTTP 401/403 denies access, 400/413
rejects input, 404 means unavailable rule/revision/report, and 409 means a conflict.
503 or a missing/invalid response may leave a mutation's outcome unknown.

Strict JSON/framing limits are 512 KiB per request and 16 MiB per response.
No cookie sessions, CORS or browser Origin requests are accepted. The API never
opens caller-provided file paths, fetches arbitrary rule URLs or accepts policy
in a rule body. Source observation collection (`observe`) belongs to Atraxis;
Atropos validates and evaluates the supplied typed facts. Manual facts are not
proof of telemetry provenance, and evaluation never dispatches a job.

A timeout does not cancel server work. **Never blindly retry mutations**: inspect
history, active version, test evidence and audit first. Generation CAS protects
transitions but is not a general request-idempotency protocol. Direct Redis
administration bypasses this API and remains a trusted administrative operation.

## Migrating the earlier embedded prototype

1. Stop old Atraxis rule writers. Leave the independent read-only inventory
   daemon unchanged.
2. Move the old profile's storage, namespace, resources and grants to Atropos.
   Preserve the exact namespace/organization identifiers and Redis database;
   no rule-record conversion is needed. Remove Atraxis-only bindings and
   `inventory_client` fields from the engine profile.
3. Provision dedicated Atropos token mappings for the same subjects/organizations.
4. Configure an Atraxis version-2 gateway profile with its own token mappings and
   per-user Atropos client profiles. Keep bindings and inventory access there.
5. Start the engine and gateway, then inspect existing drafts, active versions and
   audit before authorizing further edits. Do not leave old embedded writers active.

Changing the deployment boundary does not transfer data automatically. Backups,
external providers, live production acceptance and automatic observation-driven
intervention remain separate work. Atropos continues to own saved rules when Atraxis is stopped.

For real execution, see [configured Auton adapters](execution-adapters.md).
For engine-host maintenance, see [local storage backup and restore](storage-backup.md).
