# Atropos

Rules, workflows and orchestration for the Atraxis ecosystem.

**Experimental prototype, licensed under GNU GPL version 3.** Atropos advances small, versioned workflows
through persisted steps using configurable storage and delivery adapters. It complements the supervision and
local interventions already available in monit-docker.

This first version provides a Python service API and explicit CLI commands for
validation, structural planning, submission, workers, inspection and resolution
of uncertain runs. The plain workflow CLI includes harmless demonstration actions. The guarded rule
service can register [approved Auton executors](docs/user/execution-adapters.md)
from its private engine profile. Trusted applications may also register actions.

## Start here

Requires Python 3.11+. SQLite is the default storage; PostgreSQL, MySQL/MariaDB and Redis are optional.
Install the source checkout with `python -m pip install .`.

```sh
atropos validate examples/inspect.yml
atropos plan examples/inspect.yml
export ATROPOS_STORAGE_BACKEND=sqlite
export ATROPOS_STORAGE_URL=sqlite:///atropos.db
atropos submit examples/inspect.yml --target demo-node --dedup-key demo-001
atropos worker
```

Use `atropos status RUN_ID` to inspect a submitted run. Submission means queued,
not completed. See the user guide before configuring a Redis instance or adding
an action with external effects.

## Documentation

- [Storage and queue adapters](docs/user/backends.md): SQLite, PostgreSQL, MySQL/MariaDB, Redis and installed plugins.
- [User guide](docs/user/README.md): configuration, workflow format, Python API,
  operation and failure semantics.
- [Contributor guide](docs/contributor/README.md): architecture, tests and known
  prototype limits.
- [YAML rules, ZEN tests and versioning](docs/user/rules.md).
- [Account membership and group grants](docs/user/membership.md).
- [Rule archive and draft deletion](docs/user/rule-retirement.md).
- [Local storage backup and restore](docs/user/storage-backup.md).
- [Public roadmap](ROADMAP.md).
- [Brand assets](assets/brand/README.md).

Atraxis owns optional AI-assisted authoring and routing. Atropos has no AI runtime
dependency. Optional ZEN support now evaluates a limited experimental YAML rule model.
The final intervention language remains undecided. This source publication is an
experimental development version, not a production-readiness or package-release claim.

## Rule service

Atropos owns the rule lifecycle and persistence. Run its [standalone rule API](docs/user/rule-service.md) for Atraxis or other authorized clients. The separately installable [client package](client/README.md) contains no engine, Redis or ZEN dependency.

Authorized operators can now [preview and explicitly dispatch an approved workflow](docs/user/rule-dispatch.md), with current rights checked before each step. The shipped service registers demonstration actions only.

[Verified observations and administrator tests](docs/user/observations-and-tests.md) add source-bound admission and an isolated workflow-to-alert simulation carrying an immutable test context. An optional [dedicated test receiver](docs/user/test-alert-delivery.md) can acknowledge labelled test alerts; normal notification delivery remains unconfigured.

Inspect existing archives offline with the [retention report](docs/user/retention.md);
no live data is pruned or expired.

## License

Project-owned code and documentation are licensed under GNU GPL version 3 only
([LICENSE](LICENSE), SPDX `GPL-3.0-only`). Dependencies retain their own licenses.
