# Atropos engineering requirements

- This source repository is public under GPL-3.0-only by explicit owner authorization. Keep credentials, infrastructure configuration and non-public product intentions private. Publication does not authorize deployment or package releases.
- Separate the domain service from CLI, future transports, storage and execution adapters.
- Preserve monit-docker autonomy and local protections. Atraxis owns AI authoring/configuration; the runtime must work without AI.
- Reuse the Auton client rather than calling its CLI or copying its implementation.
- Only trusted registered actions may execute. Keep unknown external outcomes distinct from confirmed failures; never blindly retry them.
- Preserve target exclusion and deduplication evidence through recovery. Do not claim exactly-once external effects.
- Use unittest and run the collection guard. Test with disposable synthetic infrastructure, never production endpoints by default.
- Maintain separate user and contributor documentation. Keep roadmap, implementation and validation claims honest.
- Preserve GPL-3.0-only licensing. Do not choose the final intervention language implicitly.

- Rule file imports and saved revisions never activate automatically. Keep the authoring model shared by CLI, web/TUI and serializers; reject lossy simple-form edits.
- Enforce user/organization/domain/operation/action/provider scopes server-side, with server/container/group membership from trusted inventory. A container-group grant does not imply access to the whole server.
- Signed admission is not a replacement for verified TLS, authoritative identity, policy freshness or execution-time controls. Keep prototype integration limits explicit.

- Keep draft heads separate from active generations. Activation requires exact-version passing scenario evidence, current scoped permissions and generation compare-and-set; audit state changes atomically. Pure decision evaluation never implies action execution.

- Atropos owns rule intelligence, persistence, activation and workflow runtime. Its service must run without importing Atraxis. Keep the separately packaged API client free of engine, Redis and ZEN dependencies. Authenticate delegated calls independently and check the expected subject/organization before every business operation.
- Public configuration names describe roles (storage/queue/cache with backend and url), not concrete backend prefixes; document the current format and reject unsupported formats explicitly.

- Preserve explicit decision-to-workflow admission: approved immutable binding, exact active generation, scoped action permission, durable event identity, and authorization before every worker attempt. Manual facts are not trusted telemetry; never silently wire observation to intervention or bypass the guarded worker.

- End-to-end tests must carry one immutable test context through events, decisions, runs, action contexts, results and alerts. Keep their queue/locks separate, require scoped test rights, call registered simulators only, and refuse unsupported/missing test mode. Never drop the marker or fall back to a real intervention/notification. Observation-required bindings must reject manual facts for real admission and enforce source-bound signed evidence and freshness.

- Optional test alert delivery must use an engine-owned approved test receiver, preserve the test context and require its matching acknowledgement. Keep destination changes explicit, never follow redirects or retry uncertain delivery, and never claim receiver acceptance proves downstream delivery.

## Backward compatibility

User decision — 7 October 2026: backward compatibility is not required for
Atraxis or Atropos. API, configuration, rule/workflow and storage formats may
change without legacy aliases, compatibility layers or automatic migrations.
Update the current client, service, examples and tests together. Document the
currently supported format and reject unsupported input explicitly. This policy
does not extend to other projects or authorize deleting stored data.

- 1.0.0 scope (8 October): tenant → organization → groups/subgroups → memberships,
  explicit bounded inheritance/delegation, no implicit cross-organization access.
  Rule archive/unarchive/delete require separate rights; preserve audit and linked
  intervention evidence. Never auto-activate on unarchive. Backup pauses execution
  while bounded persistent admission remains available; revalidate queued work at
  execution. Generic queue/storage contracts; Redis first. Hot backup targets 2.0.0.

- Intervention search must remain engine-owned and read-only. Check ownership and
  current revision read/test scope before filtering/counting; recheck authority
  before release. Never mix test stores, return partial capacity-limited totals,
  expose run inputs/proofs, or reinterpret missing evidence as no matches.

- Storage and queue selection must remain technology-independent, including non-SQL
  plugins. SQLite is the default local storage. Configure roles with backend/url/
  options; never add product-prefixed public parameters. Preserve atomic admission
  and durable outbox semantics across separate delivery adapters; a queue receipt
  never grants execution authority.

- SQL adapters reuse AnySQL connection handling with automatic replay disabled.
  Enforce fixed-width keys with CHAR(n) and actual length/alphabet checks; enforce
  nonnegative numbers, non-null keys, status enums and relationships in SQL.
  SQLite auto-increment requires exact INTEGER PRIMARY KEY syntax plus a sign CHECK.
  Test constraint rejection and actual indexed search, not merely round trips.

- Prototype formats may evolve. Do not add migration tooling implicitly. Reject
  unsupported databases without deleting or rewriting their contents; preserve
  existing deployments and stored data.

## Development, qualification and release

- Keep merging, building a release candidate, operational qualification and
  stable publication as separate steps. A merge never authorizes a publication
  or a deployment.
- Merge a development PR when the required CI checks pass and the owner has
  reviewed it. Operational qualification, where the project defines one, protects
  stable publication; it does not gate ordinary merges.
- Publish release candidates (`X.Y.ZrcN` or the platform's pre-release
  equivalent) from the exact tested CI artifacts, never rebuilt, and only to
  pre-release channels. Merging the reviewed PR that sets a candidate version is
  the explicit request to publish that candidate.
- Bind every qualification result to the digest of the exact candidate artifact.
  Never fabricate, hand-edit or reuse evidence produced for a different artifact.
- Publish a stable release only from a reviewed final build that is equivalent to
  the qualified candidate except for its version identity, after the required
  checks pass. Never overwrite tags, releases or registry versions.
- In CI, read release versions from the project metadata; do not hard-code them
  for each release.
- Server operations go through the closed commands of `decryptus/deploy`
  (application deployment) and `decryptus/si` (system maintenance). Application and
  website repositories build and test artifacts; they do not administer servers or
  hold server credentials.

## Complete delivery and release synchronization

- Treat every delivery as a consistency check across the roadmap, PyPI packages,
  Docker Hub images and registry descriptions, repository/user/contributor
  documentation, demos, and existing product websites. Update every affected
  surface; a merged PR alone does not establish a completed delivery.
- Reconcile roadmap items with actual implementation and verified delivery.
  Distinguish planned, implemented, validated and published/deployed work; keep
  remaining limits and next steps explicit. Do not leave completed releases
  listed as pending or mark unverified work complete.
- For a release, publish the validated packages and images through the existing
  release gates, verify their versions/artifacts in the registries, and align
  installation commands, Compose examples, release notes and supported versions.
  Never overwrite an immutable published version to update its contents.
- Update affected documentation in every maintained language, including README,
  guides, examples, screenshots and diagrams. Keep user and contributor material
  separate and regenerate derived assets from the accepted source.
- Update existing demos and websites when affected, including source/image pins,
  visible versions, shared captures and release metadata. Validate the deployed
  result, links and relevant desktop/mobile flows; preserve private/public
  boundaries, credentials, data and the existing approval scope.
- Inspect applicability on every change. A documentation-only change does not
  require an artificial package/image release; a project without a registry,
  demo or website does not require inventing one. Record why a surface is
  unchanged, not applicable, blocked or explicitly deferred.
- Complete the authorized delivery work before reporting success. In the final
  handoff, distinguish merged code from registry publication and deployment,
  report checks performed, and list any required surface still not updated with
  its concrete blocker. Never imply that a planned publication already happened.

## Website synchronization (atraxis.io)

- The public website for Atropos is atraxis.io, built from the private repository `decryptus/atraxis-site` (Atropos sections of index.html, fr.html and demo.html).
- Versions shown on the site are never written by hand: current versions are marked with `data-version` / `data-version-href` and filled by `shared/render_atraxis.py` from `decryptus/auton-site`, which resolves the latest PyPI release and requires the matching Git tag and Docker Hub image. Its CI fails when a displayed version differs from PyPI.
- After every release published to PyPI and Docker Hub, regenerate atraxis-site with that renderer, review the bilingual prose that describes the release, and deploy it in the same delivery. A release is not complete while atraxis.io shows an older version.

## Coding style and validation

- Keep fixed routes, schemas, enum choices, limits and patterns in named constants. In Python use `UPPER_CASE` for module constants and `_UPPER_CASE` for private ones.
- Centralize each identifier grammar, compile fixed regexes once and validate the entire identifier. Preserve existing selector semantics.
- Write technical error messages in English and expose stable machine-readable error codes. Keep localized user-facing text in the presentation layer.

## Shared component placement

- Before adding a dependency, implementing a local component or preparing a
  release, inspect the current capabilities of DWho, HTTPdis and Sonicprobe and
  reuse applicable functionality.
- For a new generic capability, assess whether it belongs in one of these shared
  libraries and record the placement decision in the contribution. Keep
  product-specific business rules in this repository.
- When a shared-library change is needed, validate it in its own repository
  against its contracts, supported versions and existing consumers. Then update
  this project's dependency and verify the integration before delivery. Follow
  each repository's release procedures and existing authorization scope.

## Test discovery and execution

- Verify the actual runner and every discovery root before adding or changing
  tests. A green command does not prove that all test declarations were loaded.
  Do not put standalone pytest functions into a suite run only by unittest.
- Check that every test declaration is actually collected by the declared runner,
  with the same interpreter/environment as the tests, before relying on a green
  run.
- Reject empty suites, import/collection errors, duplicate definitions and
  declarations omitted by the runner. Keep test helpers out of the `test*`
  namespace. Explain intentional skips and separate integration prerequisites;
  never hide failures with `continue-on-error` or `|| true`.
- Run the normal test command after the collection check. Report
  collected/executed/skipped counts and investigate unexpected changes;
  collection alone is not a passing test run.
- Check test paths, naming patterns, selection filters and CI commands together.
  A new test directory or non-Python test harness needs an explicit CI entry.
- Preserve supported interpreter matrices. Changing runners requires an explicit
  decision and collection parity.

## Separate user and contributor documentation

- Maintain two distinct entry points and tables of contents: user documentation
  for installation, configuration, operation, public APIs and troubleshooting;
  contributor documentation for architecture, internals, tests, benchmarks,
  release engineering and development plans.
- Keep README and package descriptions focused on users. Link to the contributor
  guide instead of embedding maintainer procedures. Library API examples belong
  in the user guide when they are needed to integrate the library.
- Keep registry publishing, CI setup, repository secrets and maintainer account
  configuration out of user guides, website manuals and package descriptions.
  Never include credential values or private infrastructure evidence in either guide.
- Put implementation reviews and acceptance records under the contributor
  navigation. Preserve user-facing compatibility limits, migration instructions,
  security requirements and failure semantics in the user documentation.
- Apply this separation to generated documentation and FR/EN website content.
  Update source content and generators together; do not patch only generated HTML.
- Before delivery, inspect both entry points, check links and build documentation
  with warnings treated as errors where supported. Review README/package text and
  the deployed manual for accidental maintainer content. Preserve private-project
  visibility and existing review/publication approval requirements.

## Deliverable and deployment contents

- Every deliverable and deployment ships only what the product needs to install,
  run and meet its legal obligations. This covers source tarballs and sdists,
  wheels, Docker images, Debian packages, GitHub release assets, browser
  extensions, mobile packages (APK/AAB/IPA), Claude skills and plugins, ChatGPT
  apps and instructions, and published websites.
- Never ship repository or maintainer material: `AGENTS.md`, `CLAUDE.md` and
  other agent instructions, roadmaps, contribution guides, reviews and acceptance
  records, CI configuration (`.github/`), tests and fixtures, development
  scripts, Dockerfiles and build tooling, editor/VCS files, local configuration,
  secrets or credentials.
- Keep what is required: runtime code and assets, the default configuration and
  examples the product loads or documents for installation, `LICENSE` and
  required notices, and the README or metadata a registry needs to describe the
  package. Ship user documentation only in deliverables meant to carry it.
- Select contents with explicit allow-lists: a `.dockerignore` starting with `*`,
  a `MANIFEST.in` without broad `graft`, explicit package data, a dedicated build
  directory for sites and plugins, and `export-ignore` in `.gitattributes` for
  GitHub source archives. Never publish a repository root or `COPY . .` into a
  final image.
- Before each release or deployment, list the actual artifact contents (sdist and
  wheel listings, image filesystem, archive listing, deployed URLs) and remove
  any non-essential file before publishing. Report this check in the handoff.

## Configuration file format

- Prefer YAML (`.yaml` / `.yml`) over JSON for project configuration files: it is
  easier to read, supports comments and produces clearer diffs.
- JSON remains acceptable when a tool or ecosystem requires it or does not support
  YAML (for example `package.json`, `tsconfig.json`, `composer.json`). Converting
  existing JSON files is not required.
- When a tool accepts both formats, use YAML for new configuration.
- In YAML, quote ambiguous scalars (`"yes"`, `"no"`, `"on"`, `"off"`, versions such
  as `"1.10"`) to avoid implicit type conversion.

## Brand assets placement

- Keep brand assets under `assets/brand/`: `svg/` for vector sources, `png/`
  for raster exports, `favicon/` for favicons and app icons, and `social/` for
  the GitHub social preview and Open Graph images.
- Name new files without the project prefix and, for raster exports, with the
  pixel width: `logo-horizontal.svg`, `logo-horizontal-1024.png`,
  `logo-horizontal-dark-1024.png`.
- Keep the GitHub social preview at
  `assets/brand/social/social-preview-1280x640.png` (1280×640, under 1 MB),
  derived from the existing logo. Do not redesign the brand.
- Do not move or rename an existing asset without updating every reference
  (README, HTML, packaging, build scripts) in the same change.

## Commit authorship

Write every commit as `decryptus <decryptus@users.noreply.github.com>`, as both author and committer (run `git config user.name decryptus` and `git config user.email decryptus@users.noreply.github.com` before committing, including when rebasing). Do not add `Co-Authored-By` lines, session links or any mention of an assistant or tool in commit messages. Do not rewrite history already published on the default branch to fix older commits.
