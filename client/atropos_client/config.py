"""Private client configuration primitives; no framework, engine or Redis import."""
from ipaddress import ip_address
from pathlib import Path
import re
from urllib.parse import urlsplit
import yaml

MAX_CA_BYTES = 1048576


class ConfigurationError(ValueError):
    pass


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ConfigurationError('yaml_alias_not_allowed')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ConfigurationError('invalid_or_duplicate_key')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def origin(value, allow_http):
    if not isinstance(value, str) or len(value) > 2048 or any(
            c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '%\\?#' for c in value):
        raise ValueError('invalid_origin')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.netloc.endswith(':')):
        raise ValueError('invalid_origin')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid_origin')
    try:
        address = ip_address(parsed.hostname)
    except ValueError:
        address = None
    if parsed.scheme == 'http':
        if not allow_http or address is None or not address.is_loopback:
            raise ValueError('loopback_http_only')
    elif allow_http:
        raise ValueError('invalid_origin')
    if address is not None:
        host = '[' + address.compressed + ']' if address.version == 6 else address.compressed
    else:
        host = parsed.hostname.encode('idna').decode('ascii')
        if len(host) > 253 or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', part)
                                  for part in host.split('.')):
            raise ValueError('invalid_origin')
    return parsed.scheme + '://' + host + (':' + str(port) if port else '')


def read_file(value, config_path, limit):
    path = Path(value)
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError('invalid_file')
    return str(path), data
