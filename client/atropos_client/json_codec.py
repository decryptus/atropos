"""Strict bounded-protocol JSON scalar handling."""
import math


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('invalid_json_number')


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('invalid_json_number')
    return result
