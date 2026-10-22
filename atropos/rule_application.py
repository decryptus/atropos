"""Atropos-owned rule application, independent of HTTP and presentation.

The trusted application supplies a principal resolver. Request data never
chooses the actor, organization, policy or backend destination.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuleActor:
    subject: str
    organization: str


class RuleApplication:
    def __init__(self, actor_loader, backend_factory, catalog, instantiate, dispatcher_factory=None):
        self._actor_loader = actor_loader
        self._backend_factory = backend_factory
        self._catalog = catalog
        self._instantiate = instantiate
        self._dispatcher_factory = dispatcher_factory

    def _session(self):
        actor = self._actor_loader()
        if not isinstance(actor, RuleActor):
            raise PermissionError('authenticated_rule_actor_required')
        return self._backend_factory(actor)

    def catalog(self):
        self._session()
        return self._catalog()

    def create_from_templates(self, templates, *, name, rule_kind, selections,
                              match='any', parameters=None, decision='review', catalog_version=1):
        backend, actor = self._session()
        document = self._instantiate(templates, name=name, match=match, parameters=parameters,
                                     decision=decision, catalog_version=catalog_version)
        return backend.save_draft(actor, document, rule_kind=rule_kind, selections=selections,
                                  expected_revision=None, message='Created from catalog')

    def save_draft(self, document, *, rule_kind, selections, expected_revision, message=''):
        backend, actor = self._session()
        return backend.save_draft(actor, document, rule_kind=rule_kind, selections=selections,
                                  expected_revision=expected_revision, message=message)

    def import_draft(self, content, *, format='yaml', rule_kind, selections, expected_revision, message=''):
        backend, actor = self._session()
        return backend.import_draft(actor, content, format=format, rule_kind=rule_kind,
                                    selections=selections, expected_revision=expected_revision, message=message)

    def read(self, name, revision):
        backend, actor = self._session()
        return backend.read(actor, name, revision)

    def draft(self, name):
        backend, actor = self._session()
        return backend.draft(actor, name)

    def export_yaml(self, name, revision):
        backend, actor = self._session()
        return backend.export_yaml(actor, name, revision)

    def history(self, name, offset=0, limit=50):
        backend, actor = self._session()
        return backend.history(actor, name, offset, limit)

    def test(self, name, revision, cases):
        backend, actor = self._session()
        return backend.test(actor, name, revision, cases)

    def test_report(self, name, revision, report_id):
        backend, actor = self._session()
        return backend.test_report(actor, name, revision, report_id)

    def active(self, name):
        backend, actor = self._session()
        return backend.active(actor, name)

    def activate(self, name, revision, *, test_id, expected_generation):
        backend, actor = self._session()
        return backend.activate(actor, name, revision, test_id=test_id, expected_generation=expected_generation)

    def rollback(self, name, revision, *, test_id, expected_generation):
        backend, actor = self._session()
        return backend.rollback(actor, name, revision, test_id=test_id, expected_generation=expected_generation)

    def deactivate(self, name, *, expected_generation):
        backend, actor = self._session()
        return backend.deactivate(actor, name, expected_generation=expected_generation)

    def audit(self, name, offset=0, limit=50):
        backend, actor = self._session()
        return backend.audit(actor, name, offset, limit)

    def evaluate_active(self, name, target, facts):
        backend, actor = self._session()
        return backend.evaluate_active(actor, name, target, facts)


    def _dispatcher(self):
        backend, actor = self._session()
        if self._dispatcher_factory is None:
            raise PermissionError('dispatch_not_configured')
        return self._dispatcher_factory(backend), actor

    def plan_dispatch(self, name, target, facts, binding):
        dispatcher, actor = self._dispatcher()
        return dispatcher.plan(actor, name, target, facts, binding)

    def dispatch(self, name, target, facts, binding, *, expected_generation, event_id):
        dispatcher, actor = self._dispatcher()
        return dispatcher.dispatch(actor, name, target, facts, binding, expected_generation, event_id)

    def search_interventions(self, name, *, query='', mode='all', status='', target='', workflow='', since=None, until=None, page=1, limit=50):
        from .intervention_search import InterventionQuery,search
        filters=InterventionQuery(query,mode,status,target,workflow,since,until,page,limit)
        dispatcher,actor=self._dispatcher()
        return search(dispatcher,actor,name,filters)

    def dispatch_status(self, name, event_id):
        dispatcher, actor = self._dispatcher()
        return dispatcher.status(actor, name, event_id)

    def plan_reconciliation(self, name, event_id, *, mode='real'):
        from .reconciliation import Reconciler
        dispatcher, actor = self._dispatcher()
        return Reconciler(dispatcher).plan(actor, name, event_id, mode)

    def reconcile(self, name, event_id, *, expected_state, outcome, evidence, mode='real'):
        from .reconciliation import Reconciler
        dispatcher, actor = self._dispatcher()
        return Reconciler(dispatcher).confirm(actor, name, event_id, expected_state=expected_state,
                                             outcome=outcome, evidence=evidence, mode=mode)


    def test_bindings(self, name, target):
        dispatcher, actor = self._dispatcher()
        return dispatcher.tests.bindings(actor, name, target)

    def test_dispatch(self, name, target, facts, binding, *, event_id):
        dispatcher, actor = self._dispatcher()
        return dispatcher.tests.submit(actor, name, target, facts, binding, event_id)

    def test_dispatch_status(self, name, event_id):
        dispatcher, actor = self._dispatcher()
        return dispatcher.tests.status(actor, name, event_id)

    def simulate_dispatch(self, name, target, facts, binding):
        dispatcher, actor = self._dispatcher()
        return dispatcher.simulate(actor, name, target, facts, binding)

    def observation_challenge(self, name, target, binding):
        dispatcher, actor = self._dispatcher()
        return dispatcher.observations.challenge(actor, name, target, binding)

    def plan_observed_dispatch(self, proof):
        dispatcher, actor = self._dispatcher()
        return dispatcher.observations.plan(actor, proof)

    def dispatch_observed(self, proof, *, expected_generation, event_id):
        dispatcher, actor = self._dispatcher()
        return dispatcher.observations.dispatch(actor, proof, expected_generation, event_id)


    def availability(self, name):
        backend, actor = self._session()
        return backend.availability(actor, name)

    def archive(self, name, *, expected_revision, expected_generation):
        backend, actor = self._session()
        return backend.archive(actor, name, expected_revision=expected_revision, expected_generation=expected_generation)

    def unarchive(self, name, *, expected_revision, expected_generation):
        backend, actor = self._session()
        return backend.unarchive(actor, name, expected_revision=expected_revision, expected_generation=expected_generation)

    def delete(self, name, *, expected_revision, expected_generation):
        backend, actor = self._session()
        return backend.delete(actor, name, expected_revision=expected_revision, expected_generation=expected_generation)


def build_rule_application(*, actor_loader, policy_loader, namespace='atropos', dispatcher_factory=None, storage_backend=None, redis_client=None):
    """Engine-owned composition; the caller supplies storage and authoritative policy.

    This factory is never imported or called by the Atraxis runtime.
    """
    from atropos.rule_catalog import instantiate, list_templates
    from atropos.rule_lifecycle import Principal, RuleLifecycle
    if storage_backend is None:
        from .backend_registry import RedisBackend
        storage_backend = RedisBackend(redis_client)

    def backend(actor):
        principal = Principal(actor.subject, actor.organization)
        repository = storage_backend.repository(principal.organization, namespace)
        service = RuleLifecycle(repository, storage_backend.lifecycle_store(repository), policy_loader)
        return service, principal

    return RuleApplication(actor_loader, backend, list_templates, instantiate, dispatcher_factory)
