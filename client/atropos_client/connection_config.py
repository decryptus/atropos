"""Explicit client connection layers. Server-owned identities never use ambient settings."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import ssl

import yaml

from .config import ConfigurationError, StrictLoader, origin, read_file, MAX_CA_BYTES
from .files import read_private
from .protocol import TOKEN_PATTERN

MAX_PROFILE_BYTES = 65536
DEFAULT_TIMEOUT = 15
MAX_TIMEOUT = 60
MAX_PATH = 4096
PREFIXES = frozenset(('ATRAXIS', 'ATROPOS'))
ENV_FIELDS = {'SERVER_URL': 'origin', 'TOKEN': 'token', 'TOKEN_FILE': 'token_file',
              'CA_FILE': 'ca_file', 'TIMEOUT': 'timeout'}
PROFILE_FIELDS = frozenset(('server', 'origin', 'token_file', 'ca_file',
                            'allow_loopback_http', 'timeout'))
OVERRIDE_FIELDS = frozenset(('origin', 'token_file', 'ca_file', 'timeout'))
CREDENTIAL_FIELDS = frozenset(('token', 'token_file'))


@dataclass(frozen=True)
class RemoteConfig:
    origin: str
    token_file: str | None
    ca_file: str | None
    timeout: int
    service: str = 'atropos'
    token: str | None = field(default=None, repr=False)


def validate_token(token):
    if not isinstance(token, str) or re.fullmatch(TOKEN_PATTERN, token) is None:
        raise ValueError('invalid_rule_token')
    return token


def read_token(path):
    return validate_token(read_private(path, 129).decode('ascii').rstrip('\n'))


def _path(value, base):
    if not isinstance(value, str) or not value or len(value) > MAX_PATH or '\x00' in value:
        raise ValueError('invalid_client_path')
    path = Path(value)
    return str(path if path.is_absolute() else base / path)


def _layer(document, base, *, environment=False):
    """Reject invalid explicit values before merging, including shadowed ones."""
    result = dict(document)
    if CREDENTIAL_FIELDS <= result.keys():
        raise ValueError('ambiguous_credentials')
    for key in ('token_file', 'ca_file'):
        if key in result:
            result[key] = _path(result[key], base)
    if 'token' in result:
        validate_token(result['token'])
    if 'timeout' in result:
        value = result['timeout']
        if environment:
            if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,2}', value) is None:
                raise ValueError('invalid_timeout')
            value = int(value)
        if type(value) is not int or not 1 <= value <= MAX_TIMEOUT:
            raise ValueError('invalid_timeout')
        result['timeout'] = value
    if 'origin' in result:
        result['origin'] = origin(result['origin'], result.get('allow_loopback_http', False))
    return result


def load_remote_config(path=None, *, environ=None, prefix='ATROPOS', overrides=None):
    """CLI overrides > explicitly supplied environment > private YAML > defaults.

    Environment is opt-in. Relative environment/CLI paths use cwd; YAML paths
    use the profile directory. Credentials replace each other as a single group.
    Environment and CLI destinations require HTTPS; loopback HTTP is file-only.
    """
    try:
        if prefix not in PREFIXES:
            raise ValueError('invalid_environment_prefix')
        result = {'timeout': DEFAULT_TIMEOUT}
        if path is not None:
            path = Path(path).absolute()
            document = yaml.load(read_private(path, MAX_PROFILE_BYTES), Loader=StrictLoader)
            if (type(document) is not dict or not set(document) <= PROFILE_FIELDS
                    or type(document.get('allow_loopback_http', False)) is not bool
                    or ('origin' in document and 'server' in document)):
                raise ValueError('invalid_rule_client')
            document = dict(document)
            if 'server' in document:
                server = document.pop('server')
                if type(server) is not dict or set(server) != {'url'}:
                    raise ValueError('invalid_server_configuration')
                document['origin'] = server['url']
            result.update(_layer(document, path.parent))
        environment = {} if environ is None else {
            key: environ[prefix + '_' + name] for name, key in ENV_FIELDS.items()
            if prefix + '_' + name in environ}
        explicit = {} if overrides is None else dict(overrides)
        if not set(explicit) <= OVERRIDE_FIELDS:
            raise ValueError('invalid_client_override')
        for layer in (_layer(environment, Path.cwd(), environment=True),
                      _layer(explicit, Path.cwd())):
            if CREDENTIAL_FIELDS & layer.keys():
                for key in CREDENTIAL_FIELDS:
                    result.pop(key, None)
            if 'origin' in layer:
                result.pop('allow_loopback_http', None)
            result.update(layer)
        destination = origin(result['origin'], result.get('allow_loopback_http', False))
        if not CREDENTIAL_FIELDS & result.keys():
            raise ValueError('missing_credentials')
        if 'token_file' in result:
            read_token(result['token_file'])
        ca_file = result.get('ca_file')
        if ca_file is not None:
            if not destination.startswith('https://'):
                raise ValueError('invalid_ca')
            ca_file, content = read_file(ca_file, '.', MAX_CA_BYTES)
            ssl.create_default_context(cadata=content.decode('ascii'))
        return RemoteConfig(destination, result.get('token_file'), ca_file,
                            result['timeout'], token=result.get('token'))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, yaml.YAMLError):
        raise ConfigurationError('invalid_rule_client_configuration') from None
