# Container build and publication

`Dockerfile` builds Linux amd64 with the reviewed Python image, Debian snapshot
and hashed Python archives in `docker/locks`. The dependency inventory is retained
inside the image at `/usr/local/share/atraxis/environment.json`. These locks are
copied from the qualified Atraxis Compose engine profile, with the stable 1.0.0
engine/client identities. They do not make a bit-reproducible-build claim.

The Docker Hub workflow builds on PRs and main without pushing. It verifies
non-root execution, installed source/license identity, service authentication,
actor refusal, paused backup/restore and explicit resume through the existing
installed service tests. A separate named-volume case recreates the container
between admission, each worker step and inspection, and checks persistent
results and deduplication. Fixtures have no external effects. Architecture
coverage is Linux amd64 only. Image evidence is retained for 14 days.

## Publish

Configure repository secret **DOCKERHUB_TOKEN** for Docker Hub user `decryptus`,
with write access to `decryptus/atropos`. Do not put its value in source or logs.
Use **Docker Hub image → Run workflow**, branch `main`, and select `publish`,
or explicitly create/update the `release/dockerhub` branch at the reviewed commit.
Ordinary main pushes and PRs never publish.

Publication requires an existing stable `vX.Y.Z` source tag. Runtime file sets,
all Python runtime bytes and engine/client package metadata must match that tag.
Development versions are refused. The exact image tested earlier in the job is
pushed, without a second build. Existing version tags cannot be overwritten with
a different image; a matching image permits a retry after partial publication.
Aliases `X.Y`, `X` and `latest` advance only if no newer matching stable version
already exists in Docker Hub. Registry errors fail closed.

Do not retag the existing package release merely to add its Docker recipe. The
OCI revision records the image recipe commit; the stable tag check independently
proves the packaged runtime belongs to the published version. After publishing,
pull the exact tag, verify its digest and metadata, and run the image acceptance
again before announcing availability. Publishing an image does not deploy a site.
