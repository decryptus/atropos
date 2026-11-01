# Promote the existing 1.0.0 release to PyPI

The reviewed engine and client wheels/source archives already exist on the
[GitHub release](https://github.com/decryptus/atropos/releases/tag/v1.0.0).
This workflow promotes those exact bytes. It does not rebuild packages, modify
the stable tag, change runtime code, or deploy a service. Preparing/merging the
workflow is not evidence of PyPI publication.

## One-time account configuration

An owner of the intended PyPI projects must register these two GitHub Trusted
Publishers. If a project does not exist, use the account's
[pending publishers page](https://pypi.org/manage/account/publishing/); otherwise
use that project's Publishing settings. Do not create an unrelated account or
publish under an unreviewed name if either name is unavailable.

| Field | Engine | Client |
| --- | --- | --- |
| PyPI project | `atraxis-atropos` | `atropos-client` |
| GitHub owner | `decryptus` | `decryptus` |
| Repository | `atropos` | `atropos` |
| Workflow filename | `publish-pypi.yml` | `publish-pypi.yml` |
| Environment | `pypi` | `pypi` |

The job uses GitHub environment `pypi` and short-lived OIDC credentials. No PyPI
password or long-lived token belongs in Git, chat, logs or a workflow input.
The publisher job has `id-token: write`; validation jobs do not. The upload
action is pinned to an inspected PyPA revision. See the official
[setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) and
[new-project instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Validate, publish, verify

1. Merge the reviewed PR after its checks pass. `pypi-validation.yml` checks the
   existing GitHub release/tag, six pinned file hashes and all four package
   metadata identities. It runs Twine checks and installed-package acceptance
   outside the checkout in independent engine/client/service environments.
2. Configure both Trusted Publishers. From Actions, explicitly dispatch
   **Publish existing Atropos 1.0.0 to PyPI** on the current `main` branch.
   A push, PR, merge or GitHub release event never uploads automatically.
3. The workflow requires a reviewed merged tree with successful PR promotion
   validation, re-downloads the stable assets, and checks both PyPI versions.
   It stages only missing files. Any existing file with a different digest,
   unexpected filename or yanked state blocks the operation.
4. The separate OIDC job publishes the client before the engine. These are two
   package uploads, not an atomic registry transaction. If interrupted, inspect
   the outcome. An explicit rerun may complete only the remaining files after
   all already published files are verified identical; no blind skip-existing
   or overwrite is used.
5. The verification job compares registry digests, downloads all four actual
   PyPI files and hashes their bytes. It then installs the engine service and
   client from PyPI in a fresh environment, checks dependencies/versions and
   runs CLI help. Inspect a failed verification before reporting success.

The immutable manifest is [pypi-1.0.0.json](../../scripts/pypi-1.0.0.json).
It pins the previously qualified archives, provenance and checksum list.
The original GitHub provenance truthfully records that its publication did not
publish to a registry; do not rewrite it. Registry delivery has its own workflow
record. This version-specific workflow must not be reused for a future release
without reviewing a new manifest and qualification.

The original client package lacks a long description. Twine may report that
metadata warning; it does not justify changing already published 1.0.0 bytes.
Improve metadata in a separately versioned release. User installation documents
must continue to point at the verified GitHub wheels until registry publication
and installation actually succeed.
