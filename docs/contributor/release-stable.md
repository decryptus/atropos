# Reviewed stable publication

The publisher creates a GitHub release for the bounded [1.0 matrix](../user/v1.0.md).
It does not publish to a registry or deploy services. Review private operational
evidence and its public hash-only manifest before merging final sources.

Six PR workflows must pass on the exact reviewed head: engine, client, installed
packages, Auton recovery, SQL servers and SQL TLS. The package job builds both
engine/client wheels from their source archives and verifies installed identities
in independent environments on Python 3.11 and 3.12. The publisher downloads the
exact Python 3.12 artifacts; it never rebuilds them for publication.

Publication is explicit: dispatch `Publish reviewed stable release` on main with
the tested PR head, or move `release/publish-1.0.0` from a commit preceding that
workflow to the exact reviewed main merge. Ordinary PRs/main pushes do not publish.
The publisher checks current main, the merged PR's exact tree, successful workflows,
package versions, matching service/client dependency, operational gates, all runtime
module hashes, four exact archive paths and their sizes/hashes. An existing tag or
release aborts publication; never overwrite one to make a retry succeed.

Only after attaching all archives, provenance and checksums does the draft become
stable/latest. A failed draft remains for inspection. Verify the published tag,
flags, downloaded attachment hashes and installed package identities afterward.
Runtime equivalence is based on the reviewed version-only changes from the
qualified engine/client sources; it is not permission to reuse evidence for future
functional changes. Keep private infrastructure, recipients and credentials out
of the public record.
