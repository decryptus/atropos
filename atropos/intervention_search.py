"""Read-only search of a complete bounded rule admission history."""
from dataclasses import dataclass,asdict
import heapq
import math
from .definitions import name

MAX_EVENTS = 10000
MAX_QUERY = 128
MAX_PAGE = 100000
MAX_LIMIT = 200
STATUSES = frozenset({'queued','running','retrying','succeeded','failed','cancelled','uncertain'})
MODES = frozenset({'all','real','test'})

@dataclass(frozen=True)
class InterventionQuery:
    query: str = ''
    mode: str = 'all'
    status: str = ''
    target: str = ''
    workflow: str = ''
    since: int | None = None
    until: int | None = None
    page: int = 1
    limit: int = 50

    def __post_init__(self):
        if (any(type(v) is not str or len(v)>MAX_QUERY or v and not v.isprintable()
                for v in (self.query,self.target,self.workflow))
            or type(self.mode) is not str or self.mode not in MODES
            or type(self.status) is not str or self.status and self.status not in STATUSES
            or any(v is not None and (type(v) is not int or not 0<=v<=253402300799) for v in (self.since,self.until))
            or self.since is not None and self.until is not None and self.since>self.until
            or type(self.page) is not int or not 1<=self.page<=MAX_PAGE
            or type(self.limit) is not int or not 1<=self.limit<=MAX_LIMIT):
            raise ValueError('invalid_intervention_search')

    def parameters(self):return asdict(self)


def search(dispatcher,actor,rule,query):
    name(rule,'rule')
    lifecycle=dispatcher.lifecycle
    credential=dispatcher.identity_loader(actor)
    lifecycle.draft(actor,rule)  # Authorize the named rule before history/capacity reads.
    index_factory = getattr(dispatcher.workflows.store,'intervention_index',None)
    if index_factory is not None:
        index = index_factory(dispatcher.tests.workflows.store)
        contexts, allowed = [], []
        with index.snapshot():
            for revision,mode in index.scopes(actor.subject,actor.organization,rule):
                try:
                    record=lifecycle.read(actor,rule,revision)
                    if mode=='test': lifecycle._authorize(lifecycle._policy(actor),actor,record['context'],'test')
                except PermissionError:
                    continue
                contexts.append((record['context'],mode))
                allowed.extend((revision,mode,actor.organization+'/'+kind+'/'+identity) for kind,identity in record['context']['targets'])
            total,items=index.page(actor.subject,actor.organization,rule,query,allowed)
        # End the historical snapshot before reloading current authority. A
        # revocation committed during the read must prevent releasing its rows.
        lifecycle.draft(actor,rule)
        policy=lifecycle._policy(actor)
        if not policy.member(actor.subject,actor.organization): raise PermissionError('rule_access_denied')
        for context,mode in contexts:
            lifecycle._authorize(policy,actor,context,'read')
            if mode=='test': lifecycle._authorize(policy,actor,context,'test')
        if dispatcher.identity_loader(actor)!=credential: raise PermissionError('dispatch_identity_changed')
        return dict(schema_version=1,subject=actor.subject,organization=actor.organization,rule=rule,
                    query=query.parameters(),total=total,items=items,has_more=query.page*query.limit<total)
    iterator = getattr(lifecycle.store,'iter_admissions',None)
    events = iterator(rule,MAX_EVENTS) if iterator is not None else lifecycle.store.admission_history(rule,MAX_EVENTS)
    rows=[];contexts={};seen=set();total=0
    keep=query.page*query.limit
    for event in events:
        operation=event.get('operation')
        if operation not in ('dispatch','test_run'):continue
        if event.get('author')!=actor.subject or event.get('organization')!=actor.organization:continue
        revision=event['revision'];is_test=operation=='test_run'
        try:
            record=lifecycle.read(actor,rule,revision)
            if is_test:lifecycle._authorize(lifecycle._policy(actor),actor,record['context'],'test')
        except PermissionError:continue
        # An authorized missing/corrupt run makes the search unavailable, never
        # silently complete. Test and real storage remain strictly separate.
        store=dispatcher.tests.workflows.store if is_test else dispatcher.workflows.store
        state=store.get(event['run_id'])
        attribution=state.get('attribution',{});proof=state.get('execution',{})
        expected_issuer='atropos.tests' if is_test else 'atropos.rules'
        if (state.get('id')!=event['run_id'] or attribution.get('subject')!=actor.subject
            or attribution.get('organization')!=actor.organization or attribution.get('issuer')!=expected_issuer
            or proof.get('rule')!=rule or proof.get('revision')!=revision
            or is_test and any(type(marker) is not dict or set(marker)!={'test','id'}
                or marker.get('test') is not True or marker.get('id')!=state['id']
                for marker in (state.get('test_context'),event.get('test_context')))
            or not is_test and state.get('test_context') is not None):
            raise ValueError('invalid_intervention_evidence')
        key=(is_test,state['id'])
        if key in seen:continue
        seen.add(key);contexts[(revision,is_test)]=(record['context'],is_test)
        row=dict(id=state['id'],rule=rule,revision=revision,event_id=proof['event_id'],
                 mode='test' if is_test else 'real',status=state['status'],target=state['target'],
                 workflow=state['workflow']['name'],created_at=state['created_at'],test_context=state.get('test_context'))
        if (row['status'] not in STATUSES or type(row['created_at']) not in (int,float)
            or not math.isfinite(row['created_at']) or not 0<=row['created_at']<=253402300799
            or row['target'] not in {actor.organization+'/'+kind+'/'+identity for kind,identity in record['context']['targets']}):
            raise ValueError('invalid_intervention_state')
        if query.mode!='all' and row['mode']!=query.mode:continue
        if query.status and row['status']!=query.status:continue
        if query.target and query.target.casefold() not in row['target'].casefold():continue
        if query.workflow and query.workflow.casefold() not in row['workflow'].casefold():continue
        if query.since is not None and row['created_at']<query.since:continue
        if query.until is not None and row['created_at']>query.until:continue
        if query.query and not any(query.query.casefold() in row[k].casefold() for k in ('id','rule','event_id','target','workflow')):continue
        total+=1
        item=_PageRow(row)
        if len(rows)<keep:heapq.heappush(rows,item)
        elif item.key<rows[0].key:heapq.heapreplace(rows,item)
    # Recheck every included scope before returning counts as well as rows.
    lifecycle.draft(actor,rule)
    policy=lifecycle._policy(actor)
    if not policy.member(actor.subject,actor.organization):raise PermissionError('rule_access_denied')
    for context,is_test in contexts.values():
        lifecycle._authorize(policy,actor,context,'read')
        if is_test:lifecycle._authorize(policy,actor,context,'test')
    if dispatcher.identity_loader(actor)!=credential:raise PermissionError('dispatch_identity_changed')
    rows=[item.row for item in sorted(rows,key=lambda item:item.key)]
    start=(query.page-1)*query.limit
    return dict(schema_version=1,subject=actor.subject,organization=actor.organization,rule=rule,
                query=query.parameters(),total=total,items=rows[start:start+query.limit],has_more=start+query.limit<total)


class _PageRow:
    """Max-heap of only the prefix needed for this requested page."""
    def __init__(self,row):
        self.row=row
        self.key=(-row['created_at'],row['mode'],row['id'])

    def __lt__(self,other):
        return self.key>other.key
