"""Independent consumer contract for the relational account projection."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from atropos.membership import configured_membership
from sonicprobe.libs import anysql


class SQLiteMembershipTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'accounts.db'
        with sqlite3.connect(self.path) as c:
            c.executescript("""PRAGMA application_id=1096045144; PRAGMA user_version=3;
                CREATE TABLE account_meta(id INTEGER PRIMARY KEY,revision CHAR(32));
                INSERT INTO account_meta VALUES(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
                CREATE TABLE membership_members(subject VARCHAR(128),organization VARCHAR(128),PRIMARY KEY(subject,organization));
                CREATE TABLE membership_groups(subject VARCHAR(128),organization VARCHAR(128),group_id VARCHAR(128),PRIMARY KEY(subject,organization,group_id));
                INSERT INTO membership_members VALUES('alice','org');
                INSERT INTO membership_groups VALUES('alice','org','ops');""")
        self.path.chmod(0o600)
        self.authority=configured_membership({'backend':'sqlite','path':str(self.path)},self.path.parent/'service.yml')

    def test_current_membership_revocation_and_missing_file_fail_closed(self):
        self.assertEqual(self.authority.groups('alice','org'),frozenset({'ops'}))
        self.assertIsNone(self.authority.groups('alice','elsewhere'))
        with sqlite3.connect(self.path) as c:c.execute('DELETE FROM membership_members')
        self.assertIsNone(self.authority.groups('alice','org'))
        self.path.unlink()
        with self.assertRaises(PermissionError):self.authority.groups('alice','org')
        self.assertFalse(self.path.exists())

    def test_actual_connection_refuses_writes_and_unsupported_schema(self):
        connect=anysql.connect_by_uri
        def checked(uri,**kwargs):
            owner=connect(uri,**kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                owner.driver_connection.execute("INSERT INTO membership_members VALUES('bob','org')")
            owner.rollback()
            return owner
        with patch.object(anysql,'connect_by_uri',side_effect=checked):
            self.assertEqual(self.authority.groups('alice','org'),frozenset({'ops'}))
        with sqlite3.connect(self.path) as c:c.execute('PRAGMA user_version=1')
        with self.assertRaises(PermissionError):self.authority.groups('alice','org')
