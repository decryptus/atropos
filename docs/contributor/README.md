# Contributing to Atropos

Atropos owns rule intelligence, persistence, activation and workflow execution.
The separate `client/` distribution is a transport client without engine,
Redis or ZEN dependencies. Atraxis is an independent consumer of that client.

Read [AGENTS.md](../../AGENTS.md) for engineering constraints, the
[public roadmap](../../ROADMAP.md) for planned scope, and the
[user guide](../user/README.md) for supported configuration and operation.
Storage and delivery contracts are described in [backend contracts](backend-contracts.md);
signed authorization exchange is described in [exchange](exchange.md).
The [1.0 delivery baseline](v1-delivery.md) fixes included/deferred functionality,
the four exit gates and when existing evidence needs to be repeated.

The [stable publication procedure](release-stable.md) verifies exact tested archives
and the reviewed operational qualification manifest.

## Development and validation

Use Python 3.11 or 3.12 and disposable synthetic infrastructure. Install the
matching client before the service extra:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install './client[observations]' '.[redis,service,exchange]'
python scripts/check-test-collection.py tests
python -m unittest discover -s tests -v
python scripts/check-test-collection.py client_tests
python -m unittest discover -s client_tests -v
```

Core tests require a local `redis-server` executable. The GitHub workflows are
the reproducible reference for optional dependencies, environment variables,
collection guards and disposable fixtures:

- Engine and client: Python 3.11/3.12, collected and executed unittest suites.
- [Installed distributions](installed-packages.md): source archives, wheels and
  three isolated environments on Python 3.11/3.12; package provenance, dependency
  isolation, API authentication and SQLite backup/restore outside the checkout.
- Auton recovery: service and integration tests with isolated Auton and
  Alertmanager fixtures, including uncertain-outcome reconciliation.
- SQL servers: PostgreSQL, MySQL and MariaDB adapters and constraints.
- SQL TLS: verified TLS and negative certificate/hostname checks.

The SQL server and TLS workflows share [immutable image references](../../.github/sql-images.json)
for PostgreSQL 16, MySQL 8.4 and MariaDB 11.4 on Linux amd64. These are the
same registry digests used by the preceding successful qualification, not an
implicit server upgrade. Both workflows check the requested reference and actual
image configuration ID of every running container before database tests. They
retain `sql-images-servers` (three containers) and `sql-images-tls` (nine containers)
for 14 days, including the source commit and lock-file hash. Image identity does
not imply that tests passed; read the workflow conclusion and executed counts.

TLS setup still comes from the pinned Sonicprobe checkout. The
[preparation helper](../../scripts/sql-fixture-images.py) verifies the original
script's full SHA-256 before replacing exactly its three image assignments in the
disposable checkout. Changed upstream input fails before writing. Certificate
generation, ports, cleanup, readiness deadlines and negative TLS cases remain
unchanged. Updating an image requires updating the shared lock and service
references together, then passing both workflows; updating the upstream fixture
also requires reviewing its source and checksum. The image configuration IDs are
specific to Linux amd64. No other architecture is qualified by these runs.

This pins the SQL server images only. Runner images, installed apt/Python test
dependencies and other integration fixtures remain separate inputs; these locks
do not claim a completely frozen environment or byte-identical rebuilds.

Keep test collection guards and report executed counts and skips. Never use a
production endpoint by default or weaken assertions, resource limits or deadlines
to obtain a passing result. Synthetic secrets and example domains are fixtures.

The [1.0 support matrix](../user/v1.0.md) bounds the stable reference. In particular, signed admission is not
execution authority, an uncertain external outcome is not a confirmed failure,
and durable deduplication does not prove exactly-once external effects. Changes
must preserve current authorization checks and explicit dispatch/activation.

## License

Project-owned code and documentation are licensed under GNU GPL version 3 only
([LICENSE](../../LICENSE), SPDX `GPL-3.0-only`). Dependencies retain their own
licenses. Do not include private infrastructure, credentials or historical
internal reviews in this repository.
