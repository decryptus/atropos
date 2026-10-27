# Contributing to Atropos

Atropos owns rule intelligence, persistence, activation and workflow execution.
The separate `client/` distribution is a transport client without engine,
Redis or ZEN dependencies. Atraxis is an independent consumer of that client.

Read [AGENTS.md](../../AGENTS.md) for engineering constraints, the
[public roadmap](../../ROADMAP.md) for planned scope, and the
[user guide](../user/README.md) for supported configuration and operation.
Storage and delivery contracts are described in [backend contracts](backend-contracts.md);
signed authorization exchange is described in [exchange](exchange.md).

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

Keep test collection guards and report executed counts and skips. Never use a
production endpoint by default or weaken assertions, resource limits or deadlines
to obtain a passing result. Synthetic secrets and example domains are fixtures.

Current behavior remains experimental. In particular, signed admission is not
execution authority, an uncertain external outcome is not a confirmed failure,
and durable deduplication does not prove exactly-once external effects. Changes
must preserve current authorization checks and explicit dispatch/activation.

## License

Project-owned code and documentation are licensed under GNU GPL version 3 only
([LICENSE](../../LICENSE), SPDX `GPL-3.0-only`). Dependencies retain their own
licenses. Do not include private infrastructure, credentials or historical
internal reviews in this repository.
