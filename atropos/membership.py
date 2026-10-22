"""Independent, fail-closed membership authority contract and private-file adapter."""
import json
from pathlib import Path
import re
from typing import Protocol

from atropos_client.files import read_private
from .authorization import identifier

MAX_MEMBERSHIP_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_GROUP_REFERENCES = 65536
REVISION = re.compile(r'[0-9a-f]{32}')


class MembershipAuthority(Protocol):
    def groups(self, subject: str, organization: str) -> frozenset[str] | None:
        """None denies membership; an empty set permits membership without groups."""
        ...


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_membership_snapshot')
        result[key] = value
    return result


def decode_membership(raw):
    """The envelope may carry other owner data; only membership is interpreted."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MEMBERSHIP_BYTES:
        raise ValueError('invalid_membership_snapshot')
    envelope = json.loads(raw, object_pairs_hook=unique_object)
    if type(envelope) is not dict or 'membership' not in envelope:
        raise ValueError('invalid_membership_snapshot')
    data = envelope['membership']
    if (type(data) is not dict or set(data) != {'version','revision','members'}
            or type(data['version']) is not int or data['version'] != 1
            or type(data['revision']) is not str or not REVISION.fullmatch(data['revision'])
            or type(data['members']) is not list or len(data['members']) > MAX_MEMBERS):
        raise ValueError('invalid_membership_snapshot')
    result, references = {}, 0
    for member in data['members']:
        if type(member) is not dict or set(member) != {'subject','organization','groups'}:
            raise ValueError('invalid_membership_snapshot')
        key = identifier(member['subject']), identifier(member['organization'])
        if key in result or type(member['groups']) is not list:
            raise ValueError('invalid_membership_snapshot')
        references += len(member['groups'])
        if references > MAX_GROUP_REFERENCES:
            raise ValueError('membership_capacity_exceeded')
        groups = frozenset(identifier(group) for group in member['groups'])
        if len(groups) != len(member['groups']):
            raise ValueError('invalid_membership_snapshot')
        result[key] = groups
    return data['revision'], result


class FileMembershipAuthority:
    def __init__(self, path):
        self.path = Path(path)

    def groups(self, subject, organization):
        # Never retain a cached allow across calls or fall back after a read failure.
        try:
            _, members = decode_membership(read_private(self.path, MAX_MEMBERSHIP_BYTES))
            return members.get((subject, organization))
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise PermissionError('membership_authority_unavailable') from None



class SQLiteMembershipAuthority:
    """Read-only v2 account projection; no import of the Atraxis application."""
    def __init__(self,path):
        self.path = Path(path).absolute()

    def groups(self,subject,organization):
        import os
        import sqlite3
        import stat
        from sonicprobe.libs import anysql
        from sonicprobe.libs.urisup import uri_help_unsplit
        owner = None
        try:
            metadata = self.path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid()
                    or stat.S_IMODE(metadata.st_mode)&0o077 or metadata.st_nlink!=1):
                raise ValueError('membership_store_not_private')
            uri=uri_help_unsplit(('sqlite3',None,str(self.path),[('mode','ro')],None))
            owner=anysql.connect_by_uri(uri,auto_reconnect=False)
            c=owner.driver_connection
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
            if (c.execute('PRAGMA user_version').fetchone()[0]!=3
                    or c.execute('PRAGMA application_id').fetchone()[0]!=1096045144
                    or c.execute('SELECT revision FROM account_meta WHERE id=1').fetchone() is None):
                raise ValueError('unsupported_membership_schema')
            if c.execute('SELECT 1 FROM membership_members WHERE subject=? AND organization=?',(subject,organization)).fetchone() is None:
                return None
            return frozenset(row[0] for row in c.execute('SELECT group_id FROM membership_groups WHERE subject=? AND organization=?',(subject,organization)))
        except (OSError,ValueError,TypeError,sqlite3.Error):
            raise PermissionError('membership_authority_unavailable') from None
        finally:
            if owner is not None: owner.close()

def configured_membership(spec, config_path):
    if spec is None:
        return None
    if (type(spec) is not dict or set(spec) != {'backend','path'} or spec['backend'] not in ('file','sqlite')
            or type(spec['path']) is not str or not 0 < len(spec['path']) <= 4096
            or not spec['path'].isprintable()):
        raise ValueError('invalid_membership_configuration')
    path = Path(spec['path'])
    if not path.is_absolute():
        path = Path(config_path).parent / path
    return (FileMembershipAuthority if spec['backend']=='file' else SQLiteMembershipAuthority)(path)
