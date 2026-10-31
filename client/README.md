# Atropos HTTP client

Independently installable GPLv3 client for the Atropos rule API. It contains
wire signatures, strict configuration and bounded HTTPS transport. It has no
Redis, ZEN, HTTPdis, DWho or `atropos` engine dependency. Requires Python 3.11+
and PyYAML. Install from this directory with `python -m pip install .`.

```python
from atropos_client.client import RuleClient
client = RuleClient('/private/atropos-client.yml',
                    expected_actor={'subject': 'alice', 'organization': 'example'})
print(client.catalog())
```

The private YAML profile contains `server: {url: https://atropos.example.invalid}`,
`token_file: atropos-access.token`, optional `ca_file`, and `timeout: 15`.
Both profile and token file must be mode 0600, owned by root/current UID, and not
symlinks. `origin` is a legacy alias for `server.url`; combining both is rejected.
Verified HTTPS is required, except explicit literal-loopback HTTP test profiles.

`expected_actor` is an identity precondition, not an authority claim. The engine
compares it to the account authenticated by its token before processing a command.
This prevents a misconfigured gateway credential from changing user or organization.
The client can also target the Atraxis gateway with `service='atraxis'`; such
requests must not include an expected-actor claim, as Atraxis owns that mapping.

The target service identifies successful responses with `X-Rule-Service`.
Errors use fixed codes; timeouts/lost mutation responses report an unknown outcome.
No redirect, ambient proxy/netrc lookup or retry is performed. See the engine's
[user API guide](../docs/user/rule-service.md) for lifecycle and recovery semantics.

The 0.1.0a2 contract adds `plan_dispatch`, `dispatch` and `dispatch_status`. Dispatch is a mutation with unknown-outcome recovery; it is never automatically retried. See the [dispatch guide](../docs/user/rule-dispatch.md).

Client 0.1.0a3 adds observation and test commands. The optional `observations` extra provides bounded EdDSA envelopes; it introduces no rule engine or storage dependency. Gateway and engine observation methods have separate allowlists. Test commands cannot accept an override that disables the server-issued test context.

## Explicit environment configuration

Client **0.1.0a4** accepts environment configuration when explicitly
requested. It does not read environment settings by default:

```python
from atropos_client.client import RuleClient
client = RuleClient(env_prefix='ATROPOS')
print(client.catalog())
```

Set `ATROPOS_SERVER_URL` to the engine HTTPS origin, and either
`ATROPOS_TOKEN_FILE` to a private token file or `ATROPOS_TOKEN` to a bearer token.
Optional settings are `ATROPOS_CA_FILE` and `ATROPOS_TIMEOUT` (integer 1–60 seconds,
default 15). For a direct Atraxis connection, select `service='atraxis'` and
`env_prefix='ATRAXIS'`; the corresponding variables start with `ATRAXIS_`.
The service marker and all TLS checks remain mandatory.

Precedence is explicit `overrides` > selected environment prefix > optional
private YAML profile > defaults. Accepted override keys are `origin`, `token_file`,
`ca_file` and `timeout`; no token-value command argument is needed. The two
credential choices form one group: a higher-priority token file replaces a lower
inline token, and vice versa. Declaring both at the same level is an error, even
when a higher level would replace them. Empty or malformed explicit values are
errors. A partial YAML profile may supply defaults, but the resolved connection
must contain a destination and exactly one credential source.

Relative YAML paths use the profile directory. Relative environment and override
paths use the current working directory. File permission, owner, symlink and size
checks still apply. Environment and explicit destination overrides require HTTPS;
loopback HTTP remains an explicit YAML-only facility for disposable tests.

Token values are excluded from configuration repr, command arguments and returned
errors. They are passed to the bounded transport worker through stdin, not its
inherited token environment. The environment token is a snapshot at client
creation; recreate the client to change it. Token files are reread for each
request, preserving revocation through file removal or rotation. Prefer token
files for mounted secrets: process environments can be visible to local operators.

Atraxis's server-owned per-user connection to Atropos continues to use its pinned
private profile with environment loading disabled. Environment settings do not
supply identity, organization, grants, rule contents or storage configuration.
This release adds no engine dependency, implicit local fallback or mutation retry.


## Discover authorized test workflows

`client.test_bindings('cpu', ['container', 'web'])` returns metadata for approved
bindings visible to the authenticated caller: `id`, `rule`, `revision`,
`workflow`, `actions` and `test_supported`. The engine filters by organization,
rule, concrete target, revision read/test access and test rights for every action.
It returns at most 64 rows; workflow inputs, policy and credentials are omitted.
A missing simulator sets `test_supported` to false without executing anything.
No matching access returns an empty list, including inaccessible revisions.

Listing does not reserve permission or enqueue work. Submission and workers
continue to enforce current rights. Use the matched 1.0 engine/client packages;
unsupported older services may reject this method. Atraxis forwards it through its
independently authenticated gateway, preserving engine-owned authorization.

## Admission capacity refusal

Client 0.1.0a6 recognizes `rule_queue_full` only from a matching rule service's
HTTP 429 and exact bounded error envelope. It means the new run was not admitted;
no automatic retry occurs. An unrecognized 429 or malformed/mismatched response
remains `rule_outcome_unknown` for mutations. Existing receipt recovery still
uses the stable event ID. Capacity belongs to the Atropos namespace and is
configured by its trusted administrator, never by rule inputs or Atraxis clients.

Client 0.1.0a7 adds `availability`, `archive`, `unarchive` and `delete`. Mutations
pin the exact draft revision and availability generation, and never retry unknown
outcomes. Atropos remains responsible for scoped permission and lifecycle checks.

Client 0.1.0a8 adds the read-only `search_interventions(name, **filters)` command.
See the [intervention search contract](../docs/user/intervention-search.md) for
scope, filters, pagination, permission checks and availability limits. Install the
matching client and engine sources; the [1.0 support matrix](../docs/user/v1.0.md) describes the published pair.

## Atraxis organization selection

Client 0.1.0a10 accepts `RuleClient(..., service="atraxis", organization="operations")`.
This selects an existing authenticated gateway mapping through
`X-Atraxis-Organization`; it does not set a subject or grant permissions. Direct
Atropos connections reject this option and keep their credential-bound identity.

## Version 1.0

The versioned features above remain available in client 1.0.0. Functional modules
match 0.1.0a10; only the runtime/package version changes. Use separate consumer
and engine environments and retain a consumer application's explicit dependency
pins. See the [1.0 installation and support guide](../docs/user/v1.0.md).

## License

GNU GPL version 3 only ([LICENSE](LICENSE), SPDX `GPL-3.0-only`).
