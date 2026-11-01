# Run Atropos with Docker

The Docker Hub repository is [decryptus/atropos](https://hub.docker.com/r/decryptus/atropos).
The initial image target is `1.0.0` for **Linux amd64**. Check the repository's
available tags before pulling: creating the repository alone does not publish an image.
Use a version tag or digest in deployments; `latest`, `1` and `1.0` are moving aliases.

The image contains the engine, API client, HTTP rule service, ZEN, Redis client
and Auton adapter dependencies. SQLite and the storage-backed queue remain the
default. PostgreSQL/MySQL drivers are not included in this image. No database,
Auton server, Redis server or worker starts automatically.

```sh
docker pull decryptus/atropos:1.0.0
docker run --rm --network none decryptus/atropos:1.0.0 --help
```

The process runs as UID/GID **10001:10001**. Keep configuration outside the image,
mounted read-only at `/etc/atropos`; persist SQLite and other engine state under
`/var/lib/atropos`. Bind-mounted private directories must be accessible to that UID,
with private files mode `0600` and directories `0700`. Never bake tokens or keys
into an image. Follow the [service configuration guide](rule-service.md).

## Local service on a Linux host

Prepare `engine.yml` using the service guide, with a SQLite URL such as
`sqlite:////var/lib/atropos/engine.db`, a storage-backed queue and explicit identities
and permissions. `docker/compose.yml` mounts the protected configuration directory
specified by `ATROPOS_CONFIG_DIR` and a persistent named data volume:

```sh
export ATROPOS_CONFIG_DIR=/absolute/private/atropos-config
docker compose -f docker/compose.yml up -d engine
```

This example uses Linux host networking. The application itself binds only to
**127.0.0.1:8082**; it is not a public HTTP endpoint. A separately configured TLS
reverse proxy on that host is required for remote clients. Do not use `-p 8082:8082`
as a substitute: bridge port forwarding does not reach the service's loopback
listener. Atraxis's [operator Compose guide](https://github.com/decryptus/atraxis/blob/main/docs/docker-compose.md)
provides the alternative shared-network-namespace TLS proxy layout.

The same image runs an explicitly selected guarded worker:

```sh
docker compose -f docker/compose.yml --profile execution up -d worker
```

Both processes must use the same namespace, configuration and durable storage.
Starting a worker can execute already admitted work if current permissions permit
it. Saving, testing and activating a rule do not start that worker. The default
Compose command starts only the API service.

Use the [backup/restore procedure](storage-backup.md) before upgrades; copying a
live SQLite file or deleting/recreating its volume is not a coordinated backup.
Reconciliation and explicit resume still apply to uncertain or paused work.
