# Search admitted interventions

With client 0.1.0a8 and engine 0.1.0a16:

```python
result = client.search_interventions(
    'high-cpu', query='web', mode='all', status='queued',
    target='container/web', workflow='inspect', page=1, limit=50)
```

Search covers your own admitted runs for the named rule and authenticated
organization. It does not reveal another operator's runs. Reading the current
draft is required before the history is inspected; this prevents denied callers
from probing history capacity. Current read rights
on each admitted rule revision are required; test runs additionally require test
rights. Revoked permissions remove results, and a revocation during the search
discards the response. Search never executes, retries, activates or mutates a run.

Text (`query`) matches run/event IDs, rule, target or workflow by literal
case-insensitive substring. `target` and `workflow` are additional substring
filters. `mode` is `all`, `real` or `test`; `status` is empty for all or one of
`queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled`, `uncertain`.
`since` and `until` are optional inclusive UTC Unix seconds for creation time.
They are integers, must be ordered, and range from 0 through 253402300799.
Text filters have at most 128 printable characters. Pages start at 1, with a
maximum of 100000; limits range from 1 to 200, default 50.

The result includes `subject`, `organization`, `rule`, the canonical `query`,
`total`, `items` and `has_more`. Items expose only run identity, revision, event ID,
mode, status, canonical target, workflow name, creation time and the immutable
`test_context` (null for real runs). Inputs, outputs,
credentials and execution proofs are not returned. Sorting is newest first,
then mode and run ID. Tests use their separate store and remain labelled `test`.

SQLite uses a transactional admission index: the engine authorizes revision and
target scopes before applying SQL filters, counting and selecting the page. Search
does not stop at 10,000 lifecycle events. Redis reads retained history in batches;
a concurrent history change makes that request fail instead of returning a partial
total. Missing/corrupt authorized evidence remains an error. Redis statuses may
evolve during the scan. Neither backend freezes pagination across requests.

Substring filters remain literal and case-insensitive; an index narrows the owner
and rule scope, but arbitrary substrings are not full-text indexed. Cross-rule
search and cross-operator oversight are not provided. Existing audit browsing
remains independent.
