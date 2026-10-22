"""Wire method signatures only; no validation or rule engine."""
class RuleAPI:

    def catalog(self):
        raise NotImplementedError()

    def create_from_templates(self, templates, *, name, rule_kind, selections, match='any', parameters=None, decision='review', catalog_version=1):
        raise NotImplementedError()

    def save_draft(self, document, *, rule_kind, selections, expected_revision, message=''):
        raise NotImplementedError()

    def import_draft(self, content, *, format='yaml', rule_kind, selections, expected_revision, message=''):
        raise NotImplementedError()

    def read(self, name, revision):
        raise NotImplementedError()

    def draft(self, name):
        raise NotImplementedError()

    def export_yaml(self, name, revision):
        raise NotImplementedError()

    def history(self, name, offset=0, limit=50):
        raise NotImplementedError()

    def test(self, name, revision, cases):
        raise NotImplementedError()

    def test_report(self, name, revision, report_id):
        raise NotImplementedError()

    def active(self, name):
        raise NotImplementedError()

    def activate(self, name, revision, *, test_id, expected_generation):
        raise NotImplementedError()

    def rollback(self, name, revision, *, test_id, expected_generation):
        raise NotImplementedError()

    def deactivate(self, name, *, expected_generation):
        raise NotImplementedError()

    def audit(self, name, offset=0, limit=50):
        raise NotImplementedError()

    def evaluate_active(self, name, target, facts):
        raise NotImplementedError()

    def plan_dispatch(self, name, target, facts, binding):
        raise NotImplementedError()

    def dispatch(self, name, target, facts, binding, *, expected_generation, event_id):
        raise NotImplementedError()

    def search_interventions(self, name, *, query='', mode='all', status='', target='', workflow='', since=None, until=None, page=1, limit=50):
        raise NotImplementedError()

    def dispatch_status(self, name, event_id):
        raise NotImplementedError()

    def plan_reconciliation(self, name, event_id, *, mode='real'):
        raise NotImplementedError()

    def reconcile(self, name, event_id, *, expected_state, outcome, evidence, mode='real'):
        raise NotImplementedError()

    def observation_challenge(self, name, target, binding):
        raise NotImplementedError()

    def plan_observed_dispatch(self, proof):
        raise NotImplementedError()

    def dispatch_observed(self, proof, *, expected_generation, event_id):
        raise NotImplementedError()

    def observe_dispatch_plan(self, name, target, binding):
        raise NotImplementedError()

    def observe_dispatch(self, name, target, binding, *, expected_generation, event_id):
        raise NotImplementedError()

    def simulate_dispatch(self, name, target, facts, binding):
        raise NotImplementedError()

    def test_bindings(self, name, target):
        raise NotImplementedError()

    def test_dispatch(self, name, target, facts, binding, *, event_id):
        raise NotImplementedError()

    def test_dispatch_status(self, name, event_id):
        raise NotImplementedError()

    def availability(self, name):
        raise NotImplementedError()

    def archive(self, name, *, expected_revision, expected_generation):
        raise NotImplementedError()

    def unarchive(self, name, *, expected_revision, expected_generation):
        raise NotImplementedError()

    def delete(self, name, *, expected_revision, expected_generation):
        raise NotImplementedError()
