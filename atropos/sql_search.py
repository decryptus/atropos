"""Indexed SQL read model; callers supply freshly authorized revision/target scopes."""
from contextlib import contextmanager


class SQLInterventionIndex:
    def __init__(self, real, test):
        if real.client is not test.client: raise ValueError('search_requires_shared_transaction_domain')
        self.backend = real.client
        self.namespaces = {'real':real.prefix,'test':test.prefix}

    @contextmanager
    def snapshot(self):
        # SQLite WAL readers share a stable view without reserving the writer.
        # Other adapters retain their existing transaction-domain contract.
        snapshot = getattr(self.backend, 'read_snapshot', self.backend.transaction)
        with snapshot(): yield self

    def scopes(self,subject,organization,rule):
        return list(self.backend.connection.execute(
            'SELECT DISTINCT a.revision,a.mode FROM admissions a WHERE a.organization=? AND a.subject=? AND a.rule=? AND ((a.mode=\'real\' AND a.namespace=?) OR (a.mode=\'test\' AND a.namespace=?))',
            (organization,subject,rule,self.namespaces['real'],self.namespaces['test'])))

    def page(self,subject,organization,rule,query,scopes):
        c = self.backend.connection
        c.execute(self.backend.dialect.temporary_scopes)
        c.execute(self.backend.dialect.temporary_revisions)
        c.execute('DELETE FROM authorized_interventions')
        c.execute('DELETE FROM authorized_revisions')
        c.executemany('INSERT INTO authorized_interventions VALUES(?,?,?)',scopes)
        c.executemany('INSERT INTO authorized_revisions VALUES(?,?)', sorted({(r,m) for r,m,_ in scopes}))
        clauses = ['a.organization=?','a.subject=?','a.rule=?',"((a.mode='real' AND a.namespace=?) OR (a.mode='test' AND a.namespace=?))"]
        values = [organization,subject,rule,self.namespaces['real'],self.namespaces['test']]
        # Missing authorized evidence must fail even when a filter would hide it.
        invalid = c.execute("SELECT 1 FROM admissions a JOIN authorized_revisions v ON v.revision=a.revision AND v.mode=a.mode "
            "LEFT JOIN runs r ON r.namespace=a.namespace AND r.id=a.run_id "
            "LEFT JOIN authorized_interventions s ON s.revision=a.revision AND s.mode=a.mode AND s.target=r.target WHERE "+' AND '.join(clauses)+
            " AND (r.id IS NULL OR s.revision IS NULL) LIMIT 1",values).fetchone()
        if invalid is not None: raise ValueError('invalid_intervention_evidence')
        for value,column in ((query.mode if query.mode!='all' else '', 'a.mode'),(query.status,'r.status')):
            if value: clauses.append(column+'=?');values.append(value)
        for value,column in ((query.target,'r.target'),(query.workflow,'r.workflow')):
            if value: clauses.append(self.backend.dialect.contains(column));values.append(value.casefold())
        for value,operator in ((query.since,'>='),(query.until,'<=')):
            if value is not None:clauses.append('r.created'+operator+'?');values.append(value)
        if query.query:
            clauses.append('('+' OR '.join(self.backend.dialect.contains(column) for column in ('r.id','a.rule','a.event_id','r.target','r.workflow'))+')')
            values.extend([query.query.casefold()]*5)
        source = ' FROM admissions a JOIN runs r ON r.namespace=a.namespace AND r.id=a.run_id JOIN authorized_interventions s ON s.revision=a.revision AND s.mode=a.mode AND s.target=r.target WHERE '+' AND '.join(clauses)
        total = c.execute('SELECT COUNT(*)'+source,values).fetchone()[0]
        rows = c.execute('SELECT r.id,a.rule,a.revision,a.event_id,a.mode,r.status,r.target,r.workflow,r.created'+source+' ORDER BY r.created DESC,a.mode,r.id LIMIT ? OFFSET ?',values+[query.limit,(query.page-1)*query.limit])
        items=[]
        for row in rows:
            item=dict(zip(('id','rule','revision','event_id','mode','status','target','workflow','created_at'),row))
            item['test_context']={'test':True,'id':item['id']} if item['mode']=='test' else None
            items.append(item)
        return total,items
