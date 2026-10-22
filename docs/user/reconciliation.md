# Reconcile an uncertain outcome

Use the rule service's `plan_reconciliation(name, event_id, mode="real")`
and `reconcile(name, event_id, expected_state=..., outcome=..., evidence=...,
mode="real")` methods after independently checking the external result.
Client 0.1.0a10 and engine 0.1.0a20 implement this contract. `mode="test"`
uses the separate test store and requires current test rights as well.

Only the admitting subject in the same organization can use these methods.
Current revision read access, a separate `reconcile` grant covering the target
and every workflow action, and an active identity are required at review and
confirmation. Existing execute permissions do not grant reconciliation rights.
An inactive rule or expired admission is not reactivated by this operation.

The plan returns a receipt and `expected_state`, a digest of the complete stored
state. Confirmation must supply that exact digest. Only an uncertain run without
an active worker lease and still owning its target lock can be closed. A changed
state or target owner is a conflict; review again before making a new decision.

`outcome` is `succeeded` or `failed`. `evidence` is a nonempty verification
reference or note, at most 2048 UTF-8 bytes, without leading/trailing whitespace
or control characters. Do not include secrets or credentials. Atropos stores
the author, organization, timestamp, outcome, evidence and reviewed digest in the
run and in its scoped lifecycle audit, in the same transaction as lock release.

This is an **operator attestation**, not an automated verification. It terminates
the entire run. It never retries an action, executes remaining steps, creates
action results, changes the original history, or proves that all steps ran.
The receipt's `reconciliation` field distinguishes it from ordinary completion.
Real and test run IDs, locks, results and test markers remain separate.

If the confirmation response is lost, read the original run's status and audit.
Do not automatically repeat confirmation or submit new work: the transaction
may already have committed. An unchanged uncertain state remains blocked until
an authorized operator completes verification and reconciliation.

The trusted local workflow CLI's `resolve` command remains a separate host
administration path; it does not supply this scoped API attestation or audit.
