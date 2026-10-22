"""Single bounded POST, killed by parent deadline including DNS. Never retries."""
import http.client
import json
import re
import ssl
import sys
from urllib.parse import urlsplit

from .protocol import (ROUTE, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
                                  MUTATIONS, request, decode)
from .connection_config import read_token, validate_token
from .config import origin as _origin, read_file as _read_file, MAX_CA_BYTES

WORKER_REQUEST_LIMIT = MAX_REQUEST_BYTES + 65536
MAX_ERROR_BYTES = 1024
STATUS_CODES = {400: 'invalid_rule_request', 401: 'rule_access_denied',
                403: 'rule_access_denied', 404: 'rule_not_found',
                409: 'rule_conflict', 413: 'invalid_rule_request'}


def fetch(document):
    connection = None
    uncertain = 'rule_service_unavailable'
    try:
        raw = json.dumps(document['request'], ensure_ascii=True, allow_nan=False).encode()
        method, _, _ = request(raw, allow_expected_actor=True)
        uncertain = 'rule_outcome_unknown' if method in MUTATIONS else 'rule_service_unavailable'
        origin = _origin(document['origin'], document['origin'].startswith('http://'))
        parsed = urlsplit(origin)
        timeout = document['timeout']
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError('invalid_timeout')
        if (document.get('token') is None) == (document.get('token_file') is None):
            raise ValueError('ambiguous_credentials')
        token = (read_token(document['token_file']) if document.get('token_file') is not None
                 else validate_token(document['token']))
        if parsed.scheme == 'https':
            context = ssl.create_default_context()
            if document['ca_file'] is not None:
                _, content = _read_file(document['ca_file'], '.', MAX_CA_BYTES)
                context = ssl.create_default_context(cadata=content.decode('ascii'))
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=timeout, context=context)
        else:
            if document['ca_file'] is not None:
                raise ValueError('invalid_ca')
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        context_headers={}
        organization=document.get('organization')
        if organization is not None:
            if document['service']!='atraxis' or type(organization) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',organization) is None:
                raise ValueError('invalid_organization_context')
            context_headers['X-Atraxis-Organization']=organization
        connection.request('POST', ROUTE, body=raw, headers={**context_headers,
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status not in (200, 429):
            # No redirect, proxy, netrc, credential fallback or automatic retry.
            return {'error': STATUS_CODES.get(response.status, uncertain)}
        if response.getheader('X-Rule-Service') != document['service']:
            raise ValueError('wrong_rule_service')
        if (response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
                or response.getheader('Content-Encoding', 'identity').lower() != 'identity'):
            raise ValueError('invalid_response')
        lengths = response.headers.get_all('Content-Length', [])
        transfers = response.headers.get_all('Transfer-Encoding', [])
        if (len(lengths) > 1 or len(transfers) > 1 or (lengths and transfers)
                or (transfers and transfers[0].lower() != 'chunked')):
            raise ValueError('invalid_response')
        response_limit = MAX_ERROR_BYTES if response.status == 429 else MAX_RESPONSE_BYTES
        if lengths and (not lengths[0].isascii() or not lengths[0].isdigit()
                        or int(lengths[0]) > response_limit):
            raise ValueError('invalid_response')
        body = response.read(response_limit + 1)
        if len(body) > response_limit or (lengths and len(body) != int(lengths[0])):
            raise ValueError('invalid_response')
        envelope = decode(body)
        if response.status == 429:
            return {'error': 'rule_queue_full'} if envelope == {'error': 'rule_queue_full'} else {'error': uncertain}
        if type(envelope) is not dict or set(envelope) != {'result'}:
            raise ValueError('invalid_response')
        return envelope
    except Exception:
        return {'error': uncertain}
    finally:
        if connection is not None:
            connection.close()


def main():
    try:
        raw = sys.stdin.buffer.read(WORKER_REQUEST_LIMIT + 1)
        if len(raw) > WORKER_REQUEST_LIMIT:
            raise ValueError('invalid_worker_request')
        response = fetch(decode(raw))
    except Exception:
        response = {'error': 'rule_outcome_unknown'}
    output = json.dumps(response, ensure_ascii=True, allow_nan=False)
    if len(output.encode()) > MAX_RESPONSE_BYTES:
        output = '{"error":"rule_outcome_unknown"}'
    sys.stdout.write(output)


if __name__ == '__main__':
    main()
