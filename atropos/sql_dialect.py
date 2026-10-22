"""Explicit differences between supported SQL dialects; values stay bound."""


class SQLiteDialect:
    name = 'sqlite'
    record_upsert = '''INSERT INTO records VALUES(?,?,?) ON CONFLICT("key")
        DO UPDATE SET value=excluded.value, expires=excluded.expires'''
    run_upsert = '''INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(namespace,id) DO UPDATE SET state=excluded.state,status=excluded.status,
        available=excluded.available,delivery=excluded.delivery,token=excluded.token,lease_until=excluded.lease_until'''
    journal_insert = 'INSERT INTO journal("key",value) VALUES(?,?)'
    temporary_scopes = '''CREATE TEMP TABLE IF NOT EXISTS authorized_interventions
        (revision CHAR(32) NOT NULL, mode VARCHAR(4) NOT NULL, target VARCHAR(256) NOT NULL,
        PRIMARY KEY(revision,mode,target)) WITHOUT ROWID'''
    temporary_revisions = '''CREATE TEMP TABLE IF NOT EXISTS authorized_revisions
        (revision CHAR(32) NOT NULL, mode VARCHAR(4) NOT NULL, PRIMARY KEY(revision,mode))'''

    def contains(self, column):
        return 'instr(casefold(' + column + '),?)>0'


class PostgreSQLDialect(SQLiteDialect):
    name = 'postgresql'
    journal_insert = SQLiteDialect.journal_insert + ' RETURNING id'
    temporary_scopes = '''CREATE TEMP TABLE IF NOT EXISTS authorized_interventions
        (revision CHAR(32) COLLATE "C" NOT NULL, mode VARCHAR(4) COLLATE "C" NOT NULL,
        target VARCHAR(256) COLLATE "C" NOT NULL, PRIMARY KEY(revision,mode,target))'''
    temporary_revisions = '''CREATE TEMP TABLE IF NOT EXISTS authorized_revisions
        (revision CHAR(32) COLLATE "C" NOT NULL, mode VARCHAR(4) COLLATE "C" NOT NULL,
        PRIMARY KEY(revision,mode))'''

    def contains(self, column):
        # Searchable admission identities use the domain's ASCII grammar.
        return 'strpos(lower(' + column + '),?)>0'


class MySQLDialect(SQLiteDialect):
    name = 'mysql'
    record_upsert = '''INSERT INTO records VALUES(?,?,?) ON DUPLICATE KEY UPDATE
        value=VALUES(value), expires=VALUES(expires)'''
    run_upsert = '''INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE
        state=VALUES(state),status=VALUES(status),available=VALUES(available),
        delivery=VALUES(delivery),token=VALUES(token),lease_until=VALUES(lease_until)'''

    def __init__(self, collation):
        self.temporary_scopes = '''CREATE TEMPORARY TABLE IF NOT EXISTS authorized_interventions
            (revision CHAR(32) NOT NULL, mode VARCHAR(4) NOT NULL, target VARCHAR(256) NOT NULL,
            PRIMARY KEY(revision,mode,target)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=''' + collation
        self.temporary_revisions = '''CREATE TEMPORARY TABLE IF NOT EXISTS authorized_revisions
            (revision CHAR(32) NOT NULL, mode VARCHAR(4) NOT NULL, PRIMARY KEY(revision,mode))
            ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=''' + collation

    def contains(self, column):
        return 'instr(lower(' + column + '),?)>0'
