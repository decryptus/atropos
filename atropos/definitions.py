"""Versioned workflow contract. Conditions belong to decision adapters, not eval()."""
import copy
import json
import math
import re
from pathlib import Path

import yaml

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
WORKFLOW_FIELDS = frozenset({"version", "name", "start", "steps"})
STEP_FIELDS = frozenset({"action", "with", "next", "on_error", "on_decision", "timeout", "retry"})
RETRY_FIELDS = frozenset({"attempts", "delay"})
MAX_DEFINITION_BYTES = 262144
MAX_STEPS = 64


class DefinitionError(ValueError):
    pass


def name(value, field):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise DefinitionError(f"{field}: expected an identifier (1–64 characters)")
    return value


def encode(value, limit=MAX_DEFINITION_BYTES):
    try:
        data = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise DefinitionError("expected finite, acyclic JSON data") from exc
    if len(data.encode()) > limit:
        raise DefinitionError(f"data exceeds {limit} bytes")
    return data


def number(value, field, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise DefinitionError(f"{field}: expected a number between {lower} and {upper}")


def validate(document, actions=None):
    encode(document)
    if not isinstance(document, dict) or set(document) != WORKFLOW_FIELDS:
        raise DefinitionError("workflow requires exactly version, name, start and steps")
    if type(document['version']) is not int or document['version'] != 1:
        raise DefinitionError("unsupported workflow version; expected 1")
    name(document['name'], "name")
    steps = document['steps']
    if not isinstance(steps, dict) or not 1 <= len(steps) <= MAX_STEPS:
        raise DefinitionError("steps must contain 1–64 named steps")
    for key, step in steps.items():
        name(key, "step name")
        if not isinstance(step, dict) or not set(step) <= STEP_FIELDS or 'action' not in step:
            raise DefinitionError(f"{key}: invalid step fields")
        name(step['action'], f"{key}.action")
        if actions is not None and step['action'] not in actions:
            raise DefinitionError(f"{key}: unregistered action {step['action']!r}")
        if not isinstance(step.get('with', {}), dict):
            raise DefinitionError(f"{key}.with must be an object")
        number(step.get('timeout', 30), f"{key}.timeout", .01, 3600)
        retry = step.get('retry', {})
        if not isinstance(retry, dict) or not set(retry) <= RETRY_FIELDS:
            raise DefinitionError(f"{key}.retry: invalid fields")
        attempts = retry.get('attempts', 1)
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise DefinitionError(f"{key}.retry.attempts: expected integer 1–5")
        number(retry.get('delay', 1), f"{key}.retry.delay", .01, 3600)
        if actions is not None and attempts > 1 and not actions[step['action']].retry_safe:
            raise DefinitionError(f"{key}: this action does not allow automatic retries")
        branches = step.get('on_decision', {})
        if not isinstance(branches, dict) or len(branches) > 32:
            raise DefinitionError(f"{key}.on_decision must be a mapping of up to 32 branches")
        for branch in branches:
            name(branch, "decision label")
        for target in [step.get('next'), step.get('on_error'), *branches.values()]:
            if target is not None and (not isinstance(target, str) or target not in steps):
                raise DefinitionError(f"{key}: unknown next step {target!r}")
    if not isinstance(document['start'], str) or document['start'] not in steps:
        raise DefinitionError("start must name an existing step")
    visiting, visited = set(), set()
    def walk(key):
        if key in visiting:
            raise DefinitionError("workflow cycles are not supported; use bounded retries")
        if key in visited:
            return
        visiting.add(key)
        step = steps[key]
        for target in [step.get('next'), step.get('on_error'), *step.get('on_decision', {}).values()]:
            if target is not None:
                walk(target)
        visiting.remove(key)
        visited.add(key)
    walk(document['start'])
    if visited != set(steps):
        raise DefinitionError("workflow contains unreachable steps")
    return copy.deepcopy(document)


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise DefinitionError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise DefinitionError("YAML keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_DEFINITION_BYTES + 1)
    if len(raw) > MAX_DEFINITION_BYTES:
        raise DefinitionError("workflow file exceeds 256 KiB")
    try:
        return validate(yaml.load(raw, Loader=StrictLoader))
    except (yaml.YAMLError, RecursionError) as exc:
        raise DefinitionError("invalid workflow YAML") from exc


def resolve(value, context, depth=0):
    """Resolve explicit JSON Pointers; do not interpolate or execute expressions."""
    if depth > 32:
        raise DefinitionError("input nesting exceeds 32 levels")
    if isinstance(value, dict):
        if '$ref' in value:
            if set(value) != {'$ref'}:
                raise DefinitionError("$ref cannot have sibling keys")
            path = value['$ref']
            if not isinstance(path, str) or not path.startswith('/'):
                raise DefinitionError("$ref must be an absolute JSON Pointer")
            current = context
            try:
                for token in path[1:].split('/'):
                    if re.search(r'~(?:[^01]|$)', token):
                        raise ValueError('invalid JSON Pointer escape')
                    token = token.replace('~1', '/').replace('~0', '~')
                    if isinstance(current, list):
                        if not re.fullmatch(r'0|[1-9][0-9]*', token):
                            raise ValueError('invalid JSON Pointer index')
                        current = current[int(token)]
                    else:
                        current = current[token]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise DefinitionError(f"unresolved reference: {path}") from exc
            return copy.deepcopy(current)
        return {k: resolve(v, context, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context, depth + 1) for v in value]
    return value
