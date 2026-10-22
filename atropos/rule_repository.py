"""Internal immutable rule revisions; saving never activates a revision.

External providers must implement this contract with their own CAS/versioning.
This Redis implementation is storage only: an authenticated application enforces
user/domain/resource/provider permissions before calling it.
"""
import hashlib
import json
import uuid
from typing import Protocol

from redis.exceptions import WatchError

from .definitions import encode, name
from .authorization import identifier
from .storage_contract import Conflict
from .rule_model import dump_rules, validate_rules

MAX_REVISION_BYTES = 131072


class RuleRepository(Protocol):
    def head(self, rule_name): ...
    def save(self, document, author, expected_revision, message='', context=None): ...
    def get(self, rule_name, revision): ...
    def history(self, rule_name, offset=0, limit=50): ...
    def restore(self, rule_name, revision, author, expected_revision): ...


class RedisRuleRepository:
    def __init__(self, client, organization, namespace='atropos'):
        name(namespace, 'namespace')
        self.client = client
        self.organization = identifier(organization)
        self.prefix = f'{namespace}:rules:{identifier(organization)}'

    def _key(self, rule_name, suffix):
        name(rule_name, 'rule name')
        return f'{self.prefix}:{rule_name}:{suffix}'

    def availability_key(self, rule_name):
        return self._key(rule_name, 'availability')

    def availability(self, rule_name, client=None):
        raw = (client if client is not None else self.client).get(self.availability_key(rule_name))
        return json.loads(raw) if raw else {'status': 'available', 'generation': None}

    def require_available(self, rule_name, client=None):
        if self.availability(rule_name, client)['status'] != 'available':
            raise Conflict('rule_unavailable')

    def head(self, rule_name):
        return self.client.get(self._key(rule_name, 'head'))

    def save(self, document, author, expected_revision, message='', context=None):
        document = validate_rules(document)
        identifier(author)
        if not isinstance(message, str) or len(message) > 256:
            raise ValueError('invalid_revision_message')
        head = self._key(document['name'], 'head')
        history = self._key(document['name'], 'history')
        revision = uuid.uuid4().hex
        seconds, micros = self.client.time()
        record = {'revision': revision, 'parent': expected_revision, 'author': author,
                  'message': message, 'created_at': seconds + micros / 1000000,
                  'content_sha256': hashlib.sha256(encode(document).encode()).hexdigest(),
                  'document': document}
        if context is not None:
            # Generic immutable metadata. The application validates its schema.
            record['context'] = json.loads(encode(context, 32768))
            record['context_sha256'] = hashlib.sha256(encode(record['context']).encode()).hexdigest()
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(head, self.availability_key(document['name']))
                self.require_available(document['name'], pipe)
                if pipe.get(head) != expected_revision:
                    raise Conflict('rule_head_changed')
                pipe.multi()
                pipe.set(self._key(document['name'], revision), encode(record, MAX_REVISION_BYTES), nx=True)
                pipe.set(head, revision)
                pipe.rpush(history, revision)
                pipe.execute()
                return record
            except WatchError as exc:
                raise Conflict('rule_head_changed') from exc

    def get(self, rule_name, revision):
        if not isinstance(revision, str) or len(revision) != 32 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('invalid_revision')
        raw = self.client.get(self._key(rule_name, revision))
        if raw is None:
            raise KeyError('rule_revision_not_found')
        record = json.loads(raw)
        document = validate_rules(record['document'])
        if record['content_sha256'] != hashlib.sha256(encode(document).encode()).hexdigest():
            raise ValueError('rule_revision_integrity_error')
        if record['revision'] != revision or document['name'] != rule_name:
            raise ValueError('rule_revision_identity_error')
        if 'context' in record and record.get('context_sha256') != hashlib.sha256(encode(record['context']).encode()).hexdigest():
            raise ValueError('rule_context_integrity_error')
        return record

    def history(self, rule_name, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_history_page')
        revisions = self.client.lrange(self._key(rule_name, 'history'), offset, offset + limit - 1)
        return [{k: v for k, v in self.get(rule_name, revision).items() if k != 'document'}
                for revision in revisions]

    def restore(self, rule_name, revision, author, expected_revision):
        record = self.get(rule_name, revision)
        return self.save(record['document'], author, expected_revision,
                         message=f'Restored from {revision}', context=record.get('context'))

    def export_yaml(self, rule_name, revision):
        return dump_rules(self.get(rule_name, revision)['document'])
