import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pypi_release', ROOT / 'scripts/prepare-pypi.py')
pypi = importlib.util.module_from_spec(spec); spec.loader.exec_module(pypi)


class PyPIPromotionTests(unittest.TestCase):
    def setUp(self):
        self.expected = {'package-1.whl': 'a' * 64, 'package-1.tar.gz': 'b' * 64}
        self.document = {'info': {'name': 'package', 'version': '1'}, 'urls': [
            {'filename': name, 'digests': {'sha256': sha}, 'yanked': False}
            for name, sha in self.expected.items()]}

    def test_absent_release_stages_both_files(self):
        self.assertEqual(pypi.missing_files(self.expected, None, 'package', '1'), sorted(self.expected))

    def test_identical_existing_release_is_not_uploaded_again(self):
        self.assertEqual(pypi.missing_files(self.expected, self.document, 'package', '1'), [])

    def test_partial_upload_resumes_only_the_matching_missing_file(self):
        self.document['urls'].pop()
        self.assertEqual(pypi.missing_files(self.expected, self.document, 'package', '1'), ['package-1.tar.gz'])

    def test_conflict_yank_extra_or_duplicate_file_fails_closed(self):
        variants = []
        changed = copy.deepcopy(self.document); changed['urls'][0]['digests']['sha256'] = 'c' * 64; variants.append(changed)
        changed = copy.deepcopy(self.document); changed['urls'][0]['yanked'] = True; variants.append(changed)
        changed = copy.deepcopy(self.document); changed['urls'][0]['filename'] = 'other.whl'; variants.append(changed)
        changed = copy.deepcopy(self.document); changed['urls'].append(changed['urls'][0]); variants.append(changed)
        for document in variants:
            with self.subTest(document=document), self.assertRaises(ValueError):
                pypi.missing_files(self.expected, document, 'package', '1')

    def test_wrong_package_or_version_is_rejected(self):
        for key in ('name', 'version'):
            changed = copy.deepcopy(self.document); changed['info'][key] = 'wrong'
            with self.assertRaises(ValueError): pypi.missing_files(self.expected, changed, 'package', '1')

    def test_manifest_has_exactly_four_distributions_and_two_evidence_files(self):
        manifest = json.loads(pypi.MANIFEST.read_bytes())
        self.assertEqual(len(manifest['files']), 6)
        self.assertEqual(len(pypi.archives(manifest, 'engine')), 2)
        self.assertEqual(len(pypi.archives(manifest, 'client')), 2)
        self.assertEqual(manifest['release_commit'], '613856430e87cd5abac0ea346d8d60a63b73cc57')

    def test_tampered_or_symlinked_assets_are_rejected_before_staging(self):
        manifest = json.loads(pypi.MANIFEST.read_bytes())
        release = {'tag_name': manifest['tag'], 'draft': False, 'prerelease': False}
        tag = {'object': {'type': 'commit', 'sha': manifest['release_commit'],
                         'url': 'https://api.github.com/repos/decryptus/atropos/git/commits/' + manifest['release_commit']}}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in manifest['files']: (source / name).write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'asset_digest_mismatch'):
                pypi.validate(source, manifest, release, tag)
            name = next(iter(manifest['files'])); (source / name).unlink()
            (source / name).symlink_to(source / 'SHA256SUMS')
            with self.assertRaises(ValueError): pypi.validate(source, manifest, release, tag)
