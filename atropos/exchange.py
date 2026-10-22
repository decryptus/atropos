"""Signed, scoped, replay-protected admission core for an Atraxis transport.

The caller is a trusted authenticated server, not an end-user form. This module
does not expose an HTTP listener. HTTPS/mTLS transport remains a separate adapter.
"""
import copy
import hashlib
import hmac
import json
import re
import time
import uuid

from .authorization import AccessPolicy, identifier
from .definitions import encode, validate

MAX_BODY_BYTES = 65536
MAX_TOKEN_BYTES = 8192
TOKEN_LIFETIME = 60
METHOD = 'POST'
PATH = '/v1/runs'
REQUIRED_CLAIMS = ('iss', 'aud', 'sub', 'org', 'iat', 'nbf', 'exp', 'jti', 'method', 'path', 'body_sha256')
BODY_FIELDS = frozenset({'workflow', 'revision', 'target', 'input', 'dedup_key'})
HEX_64 = re.compile(r'[0-9a-f]{64}')
HEX_32 = re.compile(r'[0-9a-f]{32}')


class AdmissionError(ValueError):
    pass


class ApprovedWorkflows:
    """Only server-approved definitions can be referenced by an incoming request."""
    def __init__(self, definitions, actions):
        self._records = {}
        for document in definitions:
            definition = validate(document, actions)
            revision = hashlib.sha256(encode(definition).encode()).hexdigest()
            self._records[definition['name'], revision] = definition

    def revision(self, definition):
        return hashlib.sha256(encode(definition).encode()).hexdigest()

    def get(self, name, revision):
        value = self._records.get((name, revision))
        if value is None:
            raise AdmissionError('workflow_not_approved')
        return copy.deepcopy(value)


def _body_document(body):
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_BODY_BYTES:
        raise AdmissionError('invalid_request')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AdmissionError('invalid_request')
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=pairs)
        if not isinstance(value, dict) or set(value) != BODY_FIELDS:
            raise ValueError()
        identifier(value['workflow'])
        if not isinstance(value['revision'], str) or not HEX_64.fullmatch(value['revision']):
            raise ValueError()
        target = value['target']
        if (not isinstance(target, dict) or set(target) != {'kind', 'id'}
                or target['kind'] not in ('server', 'container')):
            raise ValueError()
        identifier(target['id'])
        if not isinstance(value['input'], dict):
            raise ValueError()
        if not isinstance(value['dedup_key'], str) or not 1 <= len(value['dedup_key']) <= 128:
            raise ValueError()
        encode(value, MAX_BODY_BYTES)
        return value
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise AdmissionError('invalid_request') from exc


def _authorize(policy, catalog, subject, organization, request):
    definition = catalog.get(request['workflow'], request['revision'])
    actions = frozenset(step['action'] for step in definition['steps'].values())
    target = request['target']
    if not policy.allows(subject, organization, 'intervention', 'execute',
                         [(target['kind'], target['id'])], actions=actions):
        raise AdmissionError('forbidden')
    resource = policy.resource(organization, target['kind'], target['id'])
    return definition, resource


class AdmissionPublisher:
    """Atraxis-side adapter: authorize the real principal before signing."""
    def __init__(self, policy, catalog, private_key, key_id, issuer, audience):
        self.policy, self.catalog = policy, catalog
        self.private_key, self.key_id = private_key, identifier(key_id)
        self.issuer, self.audience = identifier(issuer), identifier(audience)

    def prepare(self, subject, organization, request):
        # subject/org must be derived from the authenticated server session.
        identifier(subject)
        identifier(organization)
        body = encode(request, MAX_BODY_BYTES).encode()
        document = _body_document(body)
        _authorize(self.policy, self.catalog, subject, organization, document)
        import jwt
        now = int(time.time())
        claims = {'iss': self.issuer, 'aud': self.audience, 'sub': subject, 'org': organization,
                  'iat': now, 'nbf': now, 'exp': now + TOKEN_LIFETIME, 'jti': uuid.uuid4().hex,
                  'method': METHOD, 'path': PATH, 'body_sha256': hashlib.sha256(body).hexdigest()}
        token = jwt.encode(claims, self.private_key, algorithm='EdDSA',
                           headers={'kid': self.key_id, 'typ': 'JWT'})
        return body, token


class AdmissionReceiver:
    """Atropos-side checks before the durable queue, independent of HTTP."""
    def __init__(self, service, policy, catalog, public_keys, issuer, audience):
        if not public_keys:
            raise ValueError('no_trusted_public_keys')
        self.service, self.policy, self.catalog = service, policy, catalog
        self.public_keys = dict(public_keys)
        for key in self.public_keys:
            identifier(key)
        self.issuer, self.audience = identifier(issuer), identifier(audience)

    def submit(self, body, token, method=METHOD, path=PATH):
        import jwt
        if (not isinstance(body, bytes) or not 1 <= len(body) <= MAX_BODY_BYTES
                or not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES
                or method != METHOD or path != PATH):
            raise AdmissionError('invalid_request')
        try:
            header = jwt.get_unverified_header(token)
            # Never fetch a jku/x5u, or trust an algorithm/key supplied by a token.
            if set(header) != {'alg', 'kid', 'typ'} or header['alg'] != 'EdDSA' or header['typ'] != 'JWT':
                raise ValueError()
            public_key = self.public_keys.get(header['kid'])
            if public_key is None:
                raise ValueError()
            claims = jwt.decode(token, public_key, algorithms=['EdDSA'],
                                issuer=self.issuer, audience=self.audience,
                                options={'require': list(REQUIRED_CLAIMS), 'strict_aud': True})
            if any(type(claims[key]) is not int for key in ('iat', 'nbf', 'exp')):
                raise ValueError()
            if (claims['nbf'] != claims['iat'] or not 0 < claims['exp'] - claims['iat'] <= TOKEN_LIFETIME
                    or claims['method'] != METHOD or claims['path'] != PATH
                    or not isinstance(claims['jti'], str) or not HEX_32.fullmatch(claims['jti'])
                    or not isinstance(claims['body_sha256'], str)
                    or not hmac.compare_digest(claims['body_sha256'], hashlib.sha256(body).hexdigest())):
                raise ValueError()
            identifier(claims['sub'])
            identifier(claims['org'])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise AdmissionError('unauthorized') from exc
        request = _body_document(body)
        definition, resource = _authorize(self.policy, self.catalog, claims['sub'], claims['org'], request)
        # Fail closed if Redis is unavailable. Keep nonce for the full maximum
        # lifetime from reception, avoiding clock-rounding gaps around expiry.
        nonce_key = f'{self.service.store.prefix}:admission:{self.issuer}:{claims["jti"]}'
        if not self.service.store.client.set(nonce_key, 'used', nx=True, ex=TOKEN_LIFETIME + 1):
            raise AdmissionError('replay_rejected')
        dedup = hashlib.sha256(encode({'issuer': self.issuer, 'subject': claims['sub'],
                                      'organization': claims['org'], 'key': request['dedup_key']}).encode()).hexdigest()
        state = self.service.submit(definition, request['input'], resource.execution_target, dedup,
                                    attribution={'subject': claims['sub'], 'organization': claims['org'],
                                                 'issuer': self.issuer, 'workflow_revision': request['revision']})
        return {'id': state['id'], 'status': state['status'], 'subject': claims['sub'],
                'organization': claims['org'], 'workflow': request['workflow'],
                'revision': request['revision']}
