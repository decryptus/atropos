"""Workflow admission independent of command-line and transport adapters."""
from .definitions import encode, validate


class WorkflowService:
    def __init__(self, store, actions):
        self.store = store
        self.actions = dict(actions)

    def plan(self, workflow):
        definition = validate(workflow, self.actions)
        return {
            'name': definition['name'], 'start': definition['start'],
            'steps': list(definition['steps']),
            'definition': definition,
            'effects': 'none; structural plan only, actions are not simulated',
        }

    def submit(self, workflow, inputs, target, deduplication_key=None, *, attribution=None, execution=None, conditions=None, audit=None, consume=(), not_after=None, test_context=None):
        definition = validate(workflow, self.actions)
        encode(inputs)
        return self.store.enqueue(definition, inputs, target, deduplication_key, attribution,
                                  execution=execution, conditions=conditions, audit=audit,
                                  consume=consume, not_after=not_after, test_context=test_context)
