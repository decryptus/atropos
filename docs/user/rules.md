# Experimental YAML rules and ZEN

This pilot tests manual authoring, file import/export and real ZEN evaluation.
The YAML schema is deliberately limited and remains experimental. It is separate
from the workflow YAML that describes action execution.

## Contents

- [Ready-made defaults and reusable checks](#ready-made-defaults-and-reusable-checks)
- [Author a rule](#author-a-rule)
- [Import, export and test](#import-export-and-test)
- [One model for forms and YAML](#one-model-for-forms-and-yaml)
- [Store and retrieve versions](#store-and-retrieve-versions)
- [Permissions and scope](#permissions-and-scope)
- [Limits](#limits)

## Author a rule

```yaml
version: 1
name: memory
inputs:
  memory_percent: number
rules:
  - id: high_memory
    when: {field: memory_percent, gte: 90}
    then: diagnose
default: ignore
```

Rules are evaluated in list order: **the first match wins**. If no rule matches,
the result uses `default` and `matched_rule: __default__`. This marker cannot be a
rule ID. `then` selects a decision label; it does not execute an action.

Comparisons: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`. Numeric order
comparisons require a numeric field. Conditions can be composed with `all`, `any`
and `not`. See [memory](../../examples/rules/memory.yml) for maintenance/priority
and [storage](../../examples/rules/storage.yml) for nested conditions.

Declared input types are `number`, `integer`, `boolean` and `string`. Every input
must be provided with its declared type; missing/null values, unknown fields and
implicit coercion are rejected. A missing observation never becomes healthy zero.
Use `true`/`false` for booleans. Strings such as `on` remain strings.

The format has no implicit units or durations. A field named `memory_percent`
must already contain percent values. There are no windows, PromQL queries,
arbitrary expression strings, filesystem calls, HTTP calls or functions in this
pilot. Those semantics need explicit future design rather than guessed conversion.

## Import, export and test

Install ZEN support from the source checkout:

```sh
python -m pip install '.[zen]'
atropos rules check examples/rules/memory.yml
atropos rules evaluate examples/rules/memory.yml --input examples/rules/memory-input.json
atropos rules test examples/rules/memory.yml --cases examples/rules/memory-cases.json
```

`check` validates the authoring model and reports whether the simple form could
represent it. It performs no activation. `evaluate` calls real ZEN. `test` compares
scenario results with explicit expected decisions; it returns 1 for a failed
scenario, 2 for invalid configuration and 0 when all scenarios pass.

Commands that accept a rule file load `.yaml`, `.yml` or `.json`. JSON imports use the **same
authoring schema**, not arbitrary ZEN JDM graphs. Duplicate keys, YAML aliases,
unsupported versions/fields and excess complexity are rejected.

```sh
atropos rules normalize imported-rules.json > exported-rules.yml
atropos rules compile exported-rules.yml > exported-rules.jdm.json
```

Export to a different filename: shell redirection to the input file would
truncate it before the command loads it. Canonical YAML preserves meaning,
descriptions and rule order, but not comments, spacing or original quote style.

Generated JDM includes `atropos.constants`. The wrapper supplies these immutable
values alongside observations, avoiding interpolation of arbitrary strings into
ZEN expressions. To evaluate an exported graph directly with ZEN:

```python
import json
import zen

with open('exported-rules.jdm.json') as stream:
    graph = json.load(stream)
decision = zen.ZenEngine().create_decision(json.dumps(graph))
response = decision.evaluate({
    'facts': {'memory_percent': 94, 'maintenance': False, 'restart_count': 0},
    'rule_constants': graph['atropos']['constants'],
})
```

Use `ZenRules(document).evaluate(facts)` in applications to enforce the declared
input types and select constants from the approved model, rather than accepting
constants from an untrusted request. The generated graph is a build output; the
authoring model remains the source. Arbitrary edits in a native JDM editor cannot
be imported back into this limited YAML schema.

## One model for forms and YAML

The intended editing paths share one validated dictionary model:

- A future web/TUI form edits that model, then serializes it to YAML.
- The advanced editor parses YAML into the same model.
- File import uses the same parser, and export uses the same serializer.
- The compiler converts the model to JDM; ZEN evaluates the compiled decision.

The prototype API exposes `validate_rules`, `parse_rules`, `dump_rules`,
`simple_editor_supported` and `replace_from_simple_editor`. Simple form support
covers individual comparisons and flat `all` groups. Nested `all`/`any`/`not`
stays in YAML mode. Attempting to replace an advanced model through the simple
editor API is rejected rather than silently flattening it. No web/TUI editor is
built yet, and the pilot's advanced YAML still only supports the defined subset.

To use a rule in a workflow, register an approved immutable model:

```python
from atropos.rule_model import load_rules
from atropos.zen_rules import zen_rule_action

actions['memory.decide'] = zen_rule_action(load_rules('examples/rules/memory.yml'))
```

Register before creating the service/worker. A workflow step supplies declared
facts through `with`, then selects its next step through `on_decision`. The CLI
worker does not automatically load rule files into its registry. Publish rule
and action bindings as one approved configuration; do not replace a binding's
meaning while continuing to advertise the same approved workflow version.

## Store and retrieve versions

The internal prototype repository persists immutable snapshots in Redis, scoped
by organization. It does not activate saved revisions or submit jobs:

```python
from atropos.rule_repository import RedisRuleRepository

repository = RedisRuleRepository(redis_client, organization='org')
first = repository.save(document, author='alice', expected_revision=None)
second = repository.save(edited_document, author='alice',
                         expected_revision=first['revision'], message='Adjust threshold')
history = repository.history(document['name'])
snapshot = repository.get(document['name'], first['revision'])
yaml_text = repository.export_yaml(document['name'], first['revision'])
restored = repository.restore(document['name'], first['revision'],
                              author='alice', expected_revision=second['revision'])
```

Each revision retains author, timestamp, parent revision, message and content
digest. A stale `expected_revision` fails, preventing concurrent editors from
silently overwriting each other. Restoration creates another revision; it does
not rewrite history. Redis persistence and backups remain necessary.

The application must derive the author/organization from its authenticated
principal and authorize the operation before calling this low-level repository.
Namespaces are not a substitute for authorization. The scoped [lifecycle API](rule-lifecycle.md) now manages explicit active versions,
scenario evidence and rollback. Authentication, approvals UI and transport integration remain outstanding.

**S3, GitHub and other providers are planned, not implemented.** They must support
retrieval by exact revision, integrity checks, paginated history and optimistic
concurrency. Provider credentials belong to trusted configuration. Importing from
a remote provider must produce a reviewable draft; it must never execute code,
fetch arbitrary imports or activate the downloaded rules automatically.

## Permissions and scope

The pure policy prototype defines independent dimensions:

| Dimension | Supported concepts |
| --- | --- |
| Identity | User and organization |
| Rule type | Supervision, intervention, alerting |
| Operation | Read, create, update, validate, test, import/export, save, activate/deactivate, execute, rollback |
| Resource scope | Server, container, server group, container group |
| Actions | Explicit registered action names |
| Storage | Explicit provider/destination identity |

Server scope includes its containers. Server groups include their member servers
and containers. A container-group grant can span servers but does not grant
access to those entire servers or to unrelated containers. Membership comes
from trusted inventory. Groups are organization-scoped. Export/import/save need
an explicit authorized provider, including the internal destination.

The policy denies by default. Editing supervision rules does not grant intervention
execution or activation. The prototype has no role-management UI, explicit deny
precedence or wildcard scopes. A single grant must cover the requested operation
and all targets/actions; it does not combine partial grants into broader rights.

For the transport-independent signed admission pilot and its remaining integration
requirements, see [the exchange contract](../contributor/exchange.md).

## Limits

At most 64 KiB per rule import, 32 rules, 32 input fields, 32 named checks, 128 condition nodes across definitions and expanded uses in
total, nesting depth 8, 32 membership values and 256 characters per string.
Numbers are finite and bounded to the interoperable integer range ±(2^53−1).
Facts are flat declared fields; nested observations need deliberate projection.

This pilot does not translate existing monit-docker rules or PromQL to ZEN.
monit-docker retains its native language and independent local protections.
No production policy migration or external provider access has been performed.

## Ready-made defaults and reusable checks

The built-in **catalog v1** supplies eight editable starting points. These are
example thresholds, not universal production recommendations:

| Template | Observation | Default match |
| --- | --- | --- |
| `cpu-high` | `cpu_percent` | ≥ 90% |
| `memory-high` | `memory_percent` | ≥ 90% |
| `disk-high` | `disk_percent` | ≥ 90% used |
| `inodes-high` | `inode_percent` | ≥ 90% used |
| `directory-unreadable` | `readable` | false |
| `directory-unwritable` | `writable` | false |
| `container-unhealthy` | `health_status` | exactly `unhealthy` |
| `restarts-high` | `restart_count` | ≥ 3 |

Choose templates and generate a draft without Redis, AI or a running service:

```sh
atropos rules catalog
atropos rules init cpu-high memory-high --name resource-pressure --match all > resource-pressure.yml
atropos rules check resource-pressure.yml
```

`all` means every referenced condition is true; `any` means at least one is true.
They do not mean all/any servers or users. Evaluation uses the supplied facts for
one context; resource selection and authorization remain the application's job.

Generated documents retain concrete named checks, not live catalog imports:

```yaml
version: 1
name: resource-pressure
inputs:
  cpu_percent: number
  memory_percent: number
checks:
  cpu-high: {field: cpu_percent, gte: 90}
  memory-high: {field: memory_percent, gte: 90}
rules:
  - id: combined
    when:
      all:
        - {ref: cpu-high}
        - {ref: memory-high}
    then: review
default: ignore
```

Change `all` to `any` for either condition. Reuse a check in several decisions,
combine groups, or nest `all`, `any` and `not`. A `ref` always names a local
`checks` condition, never a selected `then` result, another file or a remote rule.
References evaluate the condition regardless of the order of check definitions.
The ordered `rules` list still selects **only the first matching decision**.
Unknown references, cycles and excessive expanded complexity are rejected, even
in unused checks. No action runs when a referenced check is evaluated.

For thresholds, pass `--parameters parameters.json`, containing for example:

```json
{"cpu-high": {"threshold": 85}, "memory-high": {"threshold": 92}}
```

Only selected templates may receive parameters. Percentage thresholds accept
0–100; restart thresholds require a positive integer. `--decision LABEL` changes
the decision label, not an executable operation. The default `review` label
requires an explicitly approved workflow binding to have any operational effect.
Use `--catalog-version 1` to pin the current generation contract. The generated
description records that version and the model embeds every actual comparison;
future catalog updates cannot silently alter saved rules. Canonical export and
internal immutable revisions preserve these checks and their parameters.

The Python API is `rule_catalog.list_templates()` and
`rule_catalog.instantiate([...], match='all', parameters={...})`. Catalog entries
are exposed through the Atraxis CLI and TUI catalog view; an editable web/form interface remains future work.
The existing simple-form guard deliberately treats named checks as advanced,
until an editor can preserve their references without flattening them.

These templates evaluate **one observation**, not a duration or persistence
window. The producer must define the restart-count window and percentage units.
They neither poll resources nor fetch metrics. Missing inputs remain errors,
even when another `any` branch matches; an absent observation is not healthy.
Source freshness and broader health-state interpretation belong to an explicit
upstream contract. Filesystem and container checks are observations, not probes.

Instantiation/import/save never activate rules, grant scopes, enqueue work,
restart containers or send notifications. Atraxis delegates explicit activation
and permission checks to the Atropos service. PromQL/native monit-docker templates,
alert routing, sustained thresholds and cooldowns remain future work.

For scoped drafts, scenario evidence, activation, rollback and active evaluation,
see [the lifecycle API](rule-lifecycle.md).
