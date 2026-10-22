"""Versioned SQLite schema; immutable payloads have indexed, checked metadata."""
SCHEMA_VERSION = 2
APPLICATION_ID = 1096045135
STATUSES = ('queued','retrying','running','uncertain','succeeded','failed','cancelled')


def fixed(column,length,nullable=False):
    return f"{column} CHAR({length}) {'NULL' if nullable else 'NOT NULL'} CHECK((typeof({column})='text' AND length({column})={length} AND {column} NOT GLOB '*[^0-9a-f]*'){(' OR '+column+' IS NULL') if nullable else ''})"


def text(column,length):
    return f"{column} VARCHAR({length}) NOT NULL CHECK(typeof({column})='text' AND length({column}) BETWEEN 1 AND {length})"


def timestamp(column,nullable=False):
    return f"{column} REAL {'NULL' if nullable else 'NOT NULL'} CHECK((typeof({column}) IN ('integer','real') AND {column} BETWEEN 0 AND 253402300799){(' OR '+column+' IS NULL') if nullable else ''})"


SCHEMA = [
    f"CREATE TABLE records ({text('key',512)} PRIMARY KEY, value TEXT NOT NULL CHECK(typeof(value)='text'), {timestamp('expires',True)})",
    f"CREATE TABLE journal (id INTEGER PRIMARY KEY AUTOINCREMENT CHECK(id>0), {text('key',512)}, value TEXT NOT NULL CHECK(typeof(value)='text'))",
    'CREATE INDEX journal_key ON journal(key,id)',
    f'''CREATE TABLE runs (
        {text('namespace',64)}, {fixed('id',64)}, state TEXT NOT NULL CHECK(json_valid(state) AND length(CAST(state AS BLOB))<=1048576),
        status VARCHAR(9) NOT NULL CHECK(status IN {STATUSES!r}), {text('target',256)}, {text('workflow',64)},
        {timestamp('created')}, {timestamp('available')}, {fixed('delivery',32)}, {fixed('token',32,True)}, {timestamp('lease_until',True)},
        PRIMARY KEY(namespace,id), CHECK((token IS NULL)=(lease_until IS NULL)),
        CHECK(status='running' OR token IS NULL),
        CHECK(json_extract(state,'$.id') IS id), CHECK(json_extract(state,'$.status') IS status),
        CHECK(json_extract(state,'$.target') IS target), CHECK(json_extract(state,'$.workflow.name') IS workflow),
        CHECK(json_extract(state,'$.created_at') IS created))''',
    'CREATE INDEX runs_delivery ON runs(namespace,status,available,created,id)',
    'CREATE INDEX runs_expired ON runs(namespace,status,lease_until,id)',
    'CREATE INDEX runs_search ON runs(namespace,status,created,id)',
    'CREATE INDEX runs_target ON runs(namespace,target,created,id)',
    f'''CREATE TABLE admissions (
        {text('namespace',64)}, {fixed('run_id',64)}, journal_id INTEGER UNSIGNED NOT NULL UNIQUE CHECK(typeof(journal_id)='integer' AND journal_id>0),
        {text('organization',128)}, {text('subject',128)}, {text('rule',64)}, {fixed('revision',32)}, {text('event_id',128)},
        mode VARCHAR(4) NOT NULL CHECK(mode IN ('real','test')), PRIMARY KEY(namespace,run_id),
        FOREIGN KEY(namespace,run_id) REFERENCES runs(namespace,id), FOREIGN KEY(journal_id) REFERENCES journal(id))''',
    'CREATE INDEX admissions_owner ON admissions(organization,subject,rule,namespace,revision,run_id)',
]

# Admission metadata cannot disagree with its durable run or journal evidence.
SCHEMA += [
    """CREATE TRIGGER admissions_evidence BEFORE INSERT ON admissions BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM runs r JOIN journal j ON j.id=NEW.journal_id
            WHERE r.namespace=NEW.namespace AND r.id=NEW.run_id
            AND json_extract(r.state,'$.attribution.subject')=NEW.subject
            AND json_extract(r.state,'$.attribution.organization')=NEW.organization
            AND json_extract(r.state,'$.execution.rule')=NEW.rule
            AND json_extract(r.state,'$.execution.revision')=NEW.revision
            AND json_extract(r.state,'$.execution.event_id')=NEW.event_id
            AND json_extract(j.value,'$.run_id')=NEW.run_id
            AND json_extract(j.value,'$.author')=NEW.subject
            AND json_extract(j.value,'$.organization')=NEW.organization
            AND json_extract(j.value,'$.revision')=NEW.revision
            AND ((NEW.mode='real' AND json_extract(r.state,'$.attribution.issuer')='atropos.rules'
                AND json_extract(j.value,'$.operation')='dispatch' AND json_type(r.state,'$.test_context') IS NULL)
              OR (NEW.mode='test' AND json_extract(r.state,'$.attribution.issuer')='atropos.tests'
                AND json_extract(j.value,'$.operation')='test_run'
                AND json_type(r.state,'$.test_context.test')='true' AND json_extract(r.state,'$.test_context.id')=NEW.run_id
                AND json_type(j.value,'$.test_context.test')='true' AND json_extract(j.value,'$.test_context.id')=NEW.run_id))
        ) THEN RAISE(ABORT,'invalid_intervention_evidence') END;
    END""",
    """CREATE TRIGGER admissions_immutable BEFORE UPDATE ON admissions BEGIN
        SELECT RAISE(ABORT,'immutable_admission');
    END""",
    """CREATE TRIGGER run_identity_immutable BEFORE UPDATE ON runs
        WHEN NEW.namespace<>OLD.namespace OR NEW.id<>OLD.id OR NEW.target<>OLD.target OR NEW.workflow<>OLD.workflow OR NEW.created<>OLD.created
        OR json_extract(NEW.state,'$.attribution') IS NOT json_extract(OLD.state,'$.attribution')
        OR json_extract(NEW.state,'$.execution') IS NOT json_extract(OLD.state,'$.execution')
        OR json_extract(NEW.state,'$.test_context') IS NOT json_extract(OLD.state,'$.test_context')
        BEGIN SELECT RAISE(ABORT,'immutable_run_identity'); END""",
]
