# Configure an Auton executor

The rule service and guarded worker can register approved Auton actions from the
same private engine profile. Atropos owns their destinations and credentials;
Atraxis remains an API client and never needs this configuration or storage access.

## Install and declare an action

Install `./client` and `.[service,auton]` from the Atropos source checkout. The
optional executor uses Auton's existing `OperationService` client, version 1.3.1.
If supplying Auton from source, install its reviewed client with
`AUTON_PACKAGE=auton python -m pip install /path/to/auton` before installing the
Atropos extras. No Auton daemon is embedded in the engine.

Add this mapping to the engine profile, replacing all example values:

```yaml
execution_adapters:
  ops.inspect:
    backend: auton
    target: demo/server/lab
    origin: https://executor.example
    endpoint: inspect
    username: operator
    password_file: secrets/executor.password
    # Optional private CA; normal verified TLS is the default:
    # ca_file: secrets/executor-ca.pem
    simulation: noop
```

`target` must identify a server/container in the engine's trusted inventory.
The URL must be a bare HTTPS origin, without credentials, path, query or fragment.
Only an explicit `allow_loopback_http: true` permits HTTP to a numeric loopback
address for isolated testing. TLS verification cannot be disabled. No destination,
credentials, arbitrary imports or transport settings come from workflow inputs.
The configured client ignores ambient proxy, netrc and CA environment settings;
use the approved origin and optional CA file explicitly.

Protect the profile and secret files with mode 0600 in an administrator-owned
private directory. Relative paths resolve beside the engine profile. Passwords
are read from private regular files at execution, never emitted by listing commands.
Changing the password file's contents supports rotation without changing the action
identity. Changing its configured path, username, origin, endpoint, target or
simulation policy changes the adapter revision and requires new binding approval.

The configured Auton endpoint still controls what programs may execute. Review
its fixed workflow parameters; a generic shell endpoint grants broad power and is
not a substitute for narrow daemon-side endpoint authorization.

## Pin the reviewed binding

Obtain the adapter's configuration revision without contacting Auton:

```sh
atropos execution-adapters --config /private/engine.yml
```

Copy the returned revision into the matching [approved binding](rule-dispatch.md):

```yaml
adapter_revisions:
  ops.inspect: REPLACE_WITH_THE_REVIEWED_64_CHARACTER_REVISION
```

The binding's workflow selects `ops.inspect`; its existing action grant must also
permit that action for the concrete target. All configured actions used by the
workflow must have an exact entry. Core demonstration actions need no entry.
`rules-bindings` returns the resulting immutable binding ID. Do not copy a stale
binding ID after changing adapter settings.

The API and worker validate this mapping. A changed adapter cannot silently redirect
an already admitted run. Restart a running worker after changing adapter definitions;
its old registry fails closed. Updating the reviewed binding creates a new ID, so
old queued admissions cannot use it and fail authorization. No old intervention
is automatically resubmitted. Current actor, policy, generation, binding and
resource authorization are still checked before every step.

## Execute and test

Use the existing explicit dispatch API and guarded worker:

```sh
atropos rules-serve --config /private/engine.yml --port 8082
atropos rules-worker --config /private/engine.yml
```

A queued receipt confirms admission only. A remote timeout or lost response remains
`uncertain`; remote effects are never automatically retried. Changing configuration
cannot cancel an effect that was already admitted by Auton.

`simulation: noop` explicitly registers a local simulator. It retains the immutable
test context and returns `simulated: true` with `effects: none`. It reads no Auton
password and contacts no daemon. Run it through the test-dispatch API and
`rules-worker --test`; its queue and locks remain separate. A simulated success
validates orchestration, **not** the remote program's behavior or actual recovery.
Omit the setting, or use `simulation: unsupported`, to refuse test admission for
that action. Test mode never falls back to the real executor.
