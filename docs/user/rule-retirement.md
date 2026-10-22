# Archive, unarchive and delete rules

These operations are enforced by Atropos and require distinct scoped permissions:
`archive`, `unarchive` and `delete`. Permission is checked for **every historical
revision's target scope**, not just the current draft. Read access alone never
authorizes a change. Current operations support at most 128 historical revisions;
larger rules require a future bounded administration procedure.

- **Archive:** first deactivate the rule using the separate `deactivate` right.
  Archive preserves revisions, scenario reports, audit and linked run receipts.
  Saving, activation, new scenario tests and new simulated interventions are refused
  while archived. Queued simulated work fails execution authorization; already
  started external work is not cancelled by archiving.
- **Unarchive:** makes the rule available without activating it. Activation remains
  an explicit operation with current passing evidence and permissions.
- **Delete:** permanently removes contents and scenario reports only for a draft
  that has never been activated and has no linked real **or simulated** intervention.
  Previously used rules must be archived. Deletion preserves audit and the scope
  metadata required to protect it. The name remains reserved to avoid conflating
  future rules with retained references. There is no restore of deleted content.

The independent client exposes `availability(name)`, `archive`, `unarchive` and
`delete`. Each mutation requires the exact draft `expected_revision` and the exact
**availability generation**, returned by `availability`. This is separate from the
active generation used for activation/deactivation. Use `None` for the initial
availability generation. A concurrent edit or lifecycle change causes a conflict;
read again and review instead of retrying automatically.

```python
head = client.draft('cpu')
state = client.availability('cpu')
client.archive('cpu', expected_revision=head['revision'],
               expected_generation=state['generation'])
```

Archived content remains explicitly readable with normal scope permissions.
After deletion, `availability` and `audit` remain accessible to authorized readers;
content/history pages no longer provide the deleted material. Atraxis provides
explicit review/confirmation flows in its web interface, CLI and terminal workbench.

Archive purge is not implemented. Any future purge requires separate administrator
permission, retention/dependency checks and explicit confirmation; it must never
silently erase intervention evidence.
