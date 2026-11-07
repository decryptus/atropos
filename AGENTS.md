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

## Coding style and validation

- Keep fixed routes, schemas, enum choices, limits and patterns in named constants. In Python use `UPPER_CASE` for module constants and `_UPPER_CASE` for private ones.
- Centralize each identifier grammar, compile fixed regexes once and validate the entire identifier. Preserve existing selector semantics.
- Write technical error messages in English and expose stable machine-readable error codes. Keep localized user-facing text in the presentation layer.
