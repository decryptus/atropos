# Installed distribution acceptance

The `package.yml` workflow builds the engine and independent client on Python
3.11 and 3.12. Each wheel is built from its source archive by `python -m build`,
then installed into a fresh environment. No registry publication occurs.

| Environment | Installed candidate | Checks |
| --- | --- | --- |
| Engine | Base engine wheel | Runtime/metadata version, every Python source file, GPLv3 license, CLI, explicit synthetic execution, durable deduplication, unknown database refusal |
| Client | Base client wheel | Same package checks; no engine, Redis, ZEN, HTTPdis, DWho, JWT or Auton; actual subprocess transport against a loopback fixture, redirect and wrong-service refusal |
| Service | Engine `[service]` and matching client `[observations]` wheels | Same package checks; authenticated installed API, delegated actor/token refusal, SQLite backup/restore, retained queue/pause, explicit reconciled resume, duplicate/overwrite refusal |

The engine's base dependencies include Sonicprobe and its transitive dependencies.
The base engine does not install the independent client, Redis, ZEN or Auton.
Profile-based service/maintenance commands require the documented optional service
installation; the base CLI reports a missing-extra error instead of opening storage.
This is distinct from the base local workflow CLI and its integrated SQLite queue.

Tests run outside the checkout and check imports against the environment's actual
`site-packages`. They compare installed Python bytes with the candidate sources;
the source checkout is read as evidence and never inserted as a package import
fallback. The collection guard's `--installed` mode preserves that boundary:

```sh
/path/to/fresh/environment/bin/python /path/to/source/scripts/check-test-collection.py --installed installed_tests/engine
/path/to/fresh/environment/bin/python -m unittest discover -s /path/to/source/installed_tests/engine -v
```

Use the corresponding client or service environment and discovery directory for
the other suites. Each suite must collect and execute all declarations with zero
skips. Run `pip check` in every environment. The workflow records dependency
versions, test logs, candidate source commit and SHA-256/size for all four built
archives. Review those alongside the retained wheels and source archives.

The client transport fixture is not an engine. The service case starts the actual
installed API on loopback with disposable credentials and a synthetic `core.echo`
workflow. These checks do not qualify remote TLS, other Python versions, SQL server
backends, live Auton targets, notification delivery or site recovery. Their separate
acceptance remains necessary. No service, release or deployed data is modified.
