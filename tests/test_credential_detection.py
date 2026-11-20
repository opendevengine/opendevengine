"""Credential guards exercise real indexing/storage with synthetic source/provider I/O."""
from pathlib import Path
import tempfile
import unittest

from opendevengine.ask.common import export_text
from opendevengine.indexing import IndexService
from opendevengine.indexing.safety import decode
from opendevengine.platform.credentials import contains_credential, redact_credentials
from opendevengine.platform.db import Store
from opendevengine.platform.jobs import JobQueue
from opendevengine.platform.worker import Worker
from opendevengine.providers import ProviderService
from opendevengine.sessions.redaction import contains_secret, redact_text
from test_indexing_service import Providers


def credentials():
    values = {}
    for kind in ('', 'RSA ', 'EC ', 'OPENSSH ', 'DSA ', 'ENCRYPTED '):
        header = '-----BEGIN ' + kind + 'PRIVATE KEY-----'
        values['private-key-' + (kind.strip() or 'PEM')] = (
            header + '\nsynthetic material\n-----END ' + kind + 'PRIVATE KEY-----')
        values['incomplete-key-' + (kind.strip() or 'PEM')] = header + '\nsynthetic material'
    for kind in 'pousr':
        values['github-' + kind] = 'gh' + kind + '_' + 'A' * 40
    values['github-pat'] = 'github_' + 'pat_' + 'A' * 40
    values['github-short-pat-export-compatibility'] = 'github_' + 'pat_' + 'abc'
    values['aws'] = 'AK' + 'IA' + 'A' * 16
    for prefix in ('', 'ant-', 'proj-'):
        values['sk-' + (prefix or 'plain')] = 'sk-' + prefix + 'A' * 40
    for kind in 'abprs':
        values['slack-' + kind] = 'xox' + kind + '-' + 'A' * 30
    values['google'] = 'AI' + 'za' + 'A' * 35
    return values


class CredentialDetectionTests(unittest.TestCase):
    def test_supported_formats_across_consumers(self):
        for name, token in credentials().items():
            with self.subTest(format=name):
                source = 'value = "' + token + '"\n'
                self.assertTrue(contains_credential(source))
                self.assertTrue(contains_secret(source.encode()))
                self.assertIsNone(decode(source.encode()))
                for redactor in (redact_text, export_text):
                    result = redactor(source)
                    self.assertNotIn(token, result)
                    self.assertIn('[redacted credential]', result)
                self.assertEqual(redact_credentials(token, r'\literal'), r'\literal')

    def test_ordinary_identifiers_are_preserved(self):
        values = ['task-' + 'A' * 40, 'risk-' + 'A' * 40, 'sk-learn', '--sk-button-background-color-' + 'a' * 20,
                  'sk-ab', 'task-' + 'ant-' + 'A' * 40,
                  'prefixsk-' + 'A' * 40]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(contains_credential(value))
                self.assertFalse(contains_secret(value))
                self.assertEqual(decode(value.encode()), value)
                self.assertEqual(redact_text(value), value)
                self.assertEqual(export_text(value), value)

    def test_keys_on_diff_lines_are_still_detected(self):
        from opendevengine.indexing.safety import decode
        from opendevengine.sessions.redaction import redact_text
        from opendevengine.ask.common import export_text
        key = 'sk-' + 'A' * 40
        for text in ('@@ -1 +0,0 @@\n-' + key + '\n', '@@ -0,0 +1 @@\n+' + key + '\n', '-' + key, ' -' + key):
            self.assertIsNone(decode(text.encode()), text[:20])
            self.assertNotIn(key, redact_text(text))
            self.assertNotIn(key, export_text(text))
        for text in ('--sk-button-background-color-' + 'a' * 20, 'x-sk-' + 'a' * 30):
            self.assertIsNotNone(decode(text.encode()), text[:20])

    def test_caller_specific_rules_are_preserved(self):
        self.assertIn('[redacted credential]', redact_text('passwd=synthetic'))
        self.assertIn('[redacted credential]', redact_text('Bearer ' + 'B' * 20))
        self.assertIn('[redacted credential]', export_text('password=synthetic'))
        self.assertEqual(redact_text('/Users/synthetic/file'), '[private host path]')
        self.assertEqual(export_text('/Users/synthetic/file'), '[private host path]')
        self.assertFalse(contains_secret('password=synthetic'))

    def test_real_index_excludes_every_format_before_storage_and_embedding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store.open(root / 'data')
            jobs = JobQueue(store)
            real = ProviderService(store, None)
            profile = real.create_profile(dict(
                name='synthetic', adapter='ollama', endpoint='http://localhost:11434',
                model='injected', config=dict(capabilities=['embeddings'], embedding_dimensions=3)))
            providers = Providers(real)
            fixtures = credentials()
            files = {'module_' + str(i) + '.py': ('value = "' + token + '"\n').encode()
                     for i, token in enumerate(fixtures.values())}
            files['ordinary.py'] = b'ordinary_identifier = True\n'
            def reader(source, revision, caps):
                return dict(revision='a' * 40, revision_kind='git',
                            files=[dict(path=p, content=v) for p, v in files.items()])
            service = IndexService(store, jobs, None, providers,
                                   data_dir=root / 'data', snapshot_reader=reader)
            try:
                repository = service.register_local('synthetic', root)['repository_id']
                run = service.request_index(repository, embedding_version_id=profile['active_version_id'])
                Worker(jobs, {'index.run': service.run_index}, worker_id='credential-test').run_once()
                result = service.get_run(run['id'])
                self.assertEqual(result['state'], 'completed', result)
                with store.read() as db:
                    for table in ('snapshot_files', 'code_chunks'):
                        paths = [row[0] for row in db.execute('SELECT path FROM ' + table)]
                        self.assertEqual(paths, ['ordinary.py'])
                    bodies = [row[0] for row in db.execute('SELECT text FROM code_chunks_fts')]
                    self.assertEqual(bodies, ['ordinary_identifier = True'])
                coverage = service.get_state(repository)['active_snapshot']['coverage']
                self.assertEqual(coverage['skipped']['binary_or_sensitive'], len(fixtures))
                self.assertTrue(service.search('ordinary_identifier', [repository], semantic=False)['hits'])
                transmitted = '\n'.join(providers.calls)
                for name, token in fixtures.items():
                    with self.subTest(format=name):
                        self.assertNotIn(token, transmitted)
                if service.vectors.available:
                    # Positive control proves embedding was enabled, with only eligible source sent.
                    self.assertEqual(providers.calls, ['ordinary_identifier = True'])
                else:
                    self.assertEqual(providers.calls, [])
            finally:
                service.vectors.close()
                store.close()


if __name__ == '__main__':
    unittest.main()
