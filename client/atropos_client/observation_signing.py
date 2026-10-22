"""Bounded signed observation envelopes; no engine, storage or rule semantics."""
import json

MAX_PROOF_BYTES = 131072
MAX_KEY_BYTES = 16384


def sign_observation(private_key, key_id, payload):
    import jwt
    json.dumps(payload, allow_nan=False)
    token = jwt.encode(payload, private_key, algorithm='EdDSA', headers={'kid':key_id,'typ':'JWT'})
    if len(token.encode()) > MAX_PROOF_BYTES:
        raise ValueError('observation_proof_too_large')
    return token


def verify_observation(token, public_keys):
    import jwt
    try:
        if not isinstance(token,str) or not 1 <= len(token) <= MAX_PROOF_BYTES:
            raise ValueError()
        header=jwt.get_unverified_header(token)
        if set(header)!={'alg','kid','typ'} or header['alg']!='EdDSA' or header['typ']!='JWT':
            raise ValueError()
        key=public_keys.get(header['kid'])
        if key is None:raise ValueError()
        # Lifetime is enforced against an engine-issued Redis challenge. No
        # wall-clock assertion from the gateway grants extra validity.
        payload=jwt.decode(token,key,algorithms=['EdDSA'],options={'verify_aud':False})
        json.dumps(payload,allow_nan=False)
        return header['kid'],payload
    except (jwt.PyJWTError,ValueError,TypeError,KeyError,RecursionError):
        raise PermissionError('observation_signature_invalid') from None
