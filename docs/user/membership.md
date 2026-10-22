# Account membership and group grants

Atropos can require current account membership independently of its API credentials
and rule grants. Membership is an additional gate, not an authentication method.
Atraxis manages its account hierarchy; Atropos continues to own rule permissions,
rule storage and execution decisions.

## Configure the authority

In the private Atropos API and worker profiles, select the same authoritative file:

```yaml
membership:
  backend: file
  path: /srv/atraxis-accounts/directory.json
```

Relative paths resolve against the engine profile. `file` selects the explicit JSON adapter;
no Redis URL or Atraxis engine-storage knowledge is involved. Missing, unreadable,
malformed, oversized or insecure files fail closed. No cached allow is retained.
Omitting `membership` keeps standalone identities and individual engine grants.
A configured value of null is invalid.

Use the **live snapshot** written by Atraxis account snapshot format 3 (directory format 3). Grant the
engine read access under the supported private-file ownership rules (owner root
or the service uid; no group/world permissions). The file must be a regular file,
not a final-component symlink. For separate containers, mount the containing
private directory read-only into Atropos, not the individual file: atomic
replacement would leave a single-file bind mount pointing to the old inode.
Both services must observe the same local POSIX filesystem. Copied snapshots,
NFS and remote replicas do not provide this revocation guarantee and are unsupported.
Do not mount a backup or manually edit the projection.

## Grant a group explicit permissions

```yaml
grants:
  - subject: operators
    subject_kind: group
    organization: operations
    rule_kind: intervention
    operations: [read, execute]
    scopes: [[container_group, applications]]
    actions: [core.echo]
```

`operators` is an account group from the membership authority. `applications` is a
resource group from Atropos's trusted inventory. These are separate namespaces.
Individual grants use `subject_kind: user` (the default). A group grant requires a
configured authority; a matching username never substitutes for a group or vice
versa. One eligible grant must cover the complete requested target/action set;
partial grants are not combined to expand authority. Tenant administrators gain
no implicit intervention rights.

## Revocation behavior

The API checks membership when resolving the authenticated actor, including calls
made directly with an Atropos credential. Policy evaluation checks current group
membership. Both real and simulation workers recheck identity, membership and
scoped permissions before every attempt/step. Disabling a user, organization,
tenant or relevant group, or removing an organization membership, can therefore
block work already admitted while execution was paused for backup.

Denied work becomes `cancelled` with `execution_authorization_denied`; missing or
invalid authority files have the same fail-closed outcome. Restoring membership
or repairing the file does not revive cancelled work. Review the retained audit
and explicitly admit new work if appropriate. Completed effects remain recorded;
revocation does not undo them or interrupt an external action already started.
There is a check-to-action race window: the check is not a distributed transaction
with an external executor, which still needs its own authorization/idempotency.

## File adapter contract

The adapter consumes only the `membership` member of a bounded JSON envelope;
other envelope data belongs to its writer and is not interpreted by Atropos.

```json
{"membership":{"version":1,"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","members":[
  {"subject":"alice","organization":"operations","groups":["operators"]}
]}}
```

An absent subject/organization pair denies access. An empty groups list permits
organization membership without any group grant. Duplicate pairs, groups or JSON
keys are rejected. Limits: 8 MiB input, 4,096 member pairs and 65,536 total group
references. Revision is an opaque 32-character lowercase hexadecimal snapshot ID;
it is not a timestamp, lease or replay-protection signature. File authenticity
comes from trusted local ownership and deployment, not this ID.
