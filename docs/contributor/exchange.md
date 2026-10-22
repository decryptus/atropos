# Atraxis → Atropos admission pilot

Implementation contract. This is a **transport-independent prototype**,
not an exposed or deployed HTTPS API. Atraxis's existing read-only runtime has not
been given a mutation route. The publisher/receiver classes currently live in the
Atropos integration package and are not wired into Atraxis's authentication.

## Implemented checks

`AdmissionPublisher` evaluates a trusted `AccessPolicy` for the actual subject and
organization before minting a short-lived request token. `AdmissionReceiver`
verifies the token and applies its own trusted policy before queue admission.

- JWT signing through PyJWT, pinned `EdDSA`, tested with Ed25519 keys.
- Explicit trusted public-key IDs; no `jku`, `x5u`, discovery URL or caller-chosen
  algorithm. The receiver has no signing key.
- Required issuer, audience, subject, organization, integer timestamps, nonce,
  method/path and SHA-256 of the exact body bytes. Maximum lifetime 60 seconds.
- Exact POST `/v1/runs` binding, finite bounded JSON, duplicate-key rejection and
  fixed request fields. Maximum body 64 KiB and token 8 KiB.
- Atomic Redis nonce claim before admission. Same token cannot be replayed.
  A fresh authorized token with the same logical deduplication identity returns
  the existing run instead of submitting a duplicate.
- A server-approved workflow name and content revision, never an arbitrary
  submitted workflow. All actions and the requested target must be authorized.
- Resource/group membership resolved from trusted inventory. Request-supplied
  group membership is rejected. The target lock uses a canonical organization /
  resource-kind / resource-ID identity.
- Persisted attribution: subject, organization, issuer and workflow revision.

Install the source checkout with `python -m pip install '.[exchange]'` to use
these classes. Public/private keys in tests are ephemeral synthetic keys; no real
credentials have been read or stored.

## Components

- `authorization.py`: pure user/type/operation/resource/action/provider policy.
- `exchange.py`: approved workflow catalog, publisher and receiving admission gate.
- `WorkflowService`/`RedisStore`: persist approved execution plus attribution.

The actor must come from an authenticated server session, never a form field.
The source service is trusted to bind the correct actor to the request. The
receiver independently limits that actor according to its injected policy.
Production policy synchronization/versioning between Atraxis and Atropos is not
implemented; do not treat this test configuration as an identity directory.

Grant scope types are `server`, `container`, `server_group` and `container_group`.
Container groups can span multiple servers without granting whole-server access.
Organization boundaries apply before membership lookup. No implicit superuser,
wildcard scope or permit-by-default fallback is provided.

## Failure semantics

Authentication errors do not expose native exceptions, rules, inputs or tokens.
Authorization, approval and replay failures occur before queue insertion.
Storage failure fails closed. Replay/deduplication evidence depends on Redis
persistence: losing acknowledged writes can weaken those protections; failover
and crash-loss behavior are not validated by this pilot. If the process stops after claiming a nonce but
before persisting the run, the old token remains consumed: obtain a fresh token
with the same logical deduplication key. The queue's existing durable identity
prevents a second run if the first submission had actually succeeded.

The admission gate validates current injected permissions. Revoking rights does
not yet cancel/recheck already queued steps. Add explicit execution-time policy
checks and revocation semantics before using this for sensitive actions.

## Required before network integration

- Authenticate Atraxis users, derive authoritative organization membership and
  expose controlled administration of grants. Prevent users escalating their own
  permissions or selecting arbitrary provider destinations.
- Integrate with HTTPdis and existing server architecture; do not add an alternate
  unauthenticated listener. Use verified HTTPS, preferably mTLS for service
  identity, with trusted CA/hostname verification and no insecure fallback.
- Reject redirects, bound headers/body/timeouts/concurrency and avoid ambient
  proxy/credential discovery in the sending adapter. Apply peer rate limits.
- Add authenticated receipt/status access, scoped to organization/user rights;
  never return another user's raw execution state merely from a supplied run ID.
- Define key rotation/revocation, policy/inventory freshness, synchronization,
  action-parameter constraints and audit retention without logging token secrets.
- Bind approved workflow revisions to immutable action/decision registry versions;
  the current digest covers workflow bytes, not Python code or separate bindings.
- Exercise real TLS/HTTP failure modes and live authorized integration separately.

No HTTPS or mTLS claim follows from signing a payload. TLS also protects
confidentiality and authenticates the destination; a signed request alone does
not. No HTTP endpoint, real certificate or deployed trust relationship was created.

## Storage providers and rule versions

The internal `RedisRuleRepository` records immutable drafts and optimistic head
updates. An external adapter must preserve the same semantics, not just upload a
mutable YAML file. Keep one explicitly selected source of truth per collection;
backup/export destinations do not silently become independent editable masters.

For S3: plan version-aware reads, conditional writes, checksums, encryption,
bucket/prefix restrictions and recovery behavior. For GitHub: pin repository/path,
use least-privilege credentials, preserve commit identity and reject stale branch
updates. No provider URL, bucket, repository or credentials should come directly
from untrusted rule content. Both adapters remain unimplemented.

Original SDK references used for the pilot:

- [ZEN Python SDK](https://docs.gorules.io/developers/sdks/python)
- [JDM standard](https://docs.gorules.io/developers/jdm/standard)
- [PyJWT API](https://pyjwt.readthedocs.io/en/stable/api.html)
