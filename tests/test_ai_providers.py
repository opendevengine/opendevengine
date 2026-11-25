"""AI providers façade: providers by kind, one choice per job, a managed coding profile."""
import json
import tempfile
import unittest
from pathlib import Path

from opendevengine.execution.profiles import ExecutionProfileService
from opendevengine.platform import Platform, Worker
from opendevengine.platform.secrets import SecretService
from opendevengine.providers import ProviderService
from opendevengine.providers import validation
from opendevengine.providers.catalog import AIProviderError, AIProviders
from test_providers_service import FakeTransport, MemoryVault

KEY = 'sk-ant-private-fixture-key'


class AIProvidersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = Platform.open(Path(self.temp.name))
        self.addCleanup(self.platform.close)
        self.addCleanup(validation.set_trusted_endpoints, [])
        self.vault = MemoryVault()
        self.secrets = SecretService(self.platform.store, self.vault)
        self.providers = ProviderService(self.platform.store, self.secrets, transport=FakeTransport())
        self.profiles = ExecutionProfileService(self.platform, self.providers)
        self.catalog = AIProviders(self.platform.store, self.secrets, self.providers, self.profiles)

    def code(self, expected, call):
        with self.assertRaises(AIProviderError) as caught:
            call()
        self.assertEqual(caught.exception.code, expected)

    def claude(self, **kw):
        return self.catalog.add('claude', api_key=KEY, consent=True, **kw)

    def local(self, endpoint='http://100.64.0.9:11434'):
        return self.catalog.add('local', endpoint=endpoint, adapter='ollama')

    def managed(self):
        with self.platform.store.read() as db:
            return db.execute('SELECT * FROM execution_profiles WHERE managed=1 AND deleted_at IS NULL').fetchall()

    def pass_coding_test(self):
        Worker(self.platform.jobs, {'execution.test': lambda *_: {}}, worker_id='fixture').run_once()
        with self.platform.store.transaction() as db:
            db.execute("UPDATE execution_profile_checks SET status='passed',result_json=? ",
                       (json.dumps({'image_id': 'sha256:' + 'a' * 64, 'capabilities': {}}),))

    def test_failed_add_cleans_up_secret(self):
        from unittest.mock import patch

        before = dict(self.vault.values)

        with patch.object(
            self.providers,
            '_update_settings_in_transaction',
            side_effect=RuntimeError('Injected failure'),
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'Injected failure'
            ):
                self.catalog.add(
                    'claude',
                    api_key=KEY,
                    consent=True,
                )

        self.assertEqual(self.catalog.list(), [])
        self.assertEqual(self.vault.values, before)

    def _assert_failed_add_preserves_privacy(self, local_only):
        import sqlite3

        self.providers.update_settings(local_only=local_only)
        settings_before = self.providers.settings()
        vault_before = dict(self.vault.values)

        with self.platform.store.transaction() as db:
            db.execute("""
                CREATE TRIGGER reject_provider_insert
                BEFORE INSERT ON ai_providers
                BEGIN
                    SELECT RAISE(ABORT, 'Injected insert failure');
                END
            """)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, 'Injected insert failure'
        ):
            self.catalog.add(
                'claude', api_key=KEY, consent=True)

        self.assertEqual(self.catalog.list(), [])
        self.assertEqual(
            self.providers.settings(), settings_before)
        self.assertEqual(self.vault.values, vault_before)

    def test_failed_add_preserves_local_only_mode(self):
        self._assert_failed_add_preserves_privacy(True)

    def test_failed_add_preserves_existing_remote_consent(self):
        self._assert_failed_add_preserves_privacy(False)

    def test_remote_provider_requires_consent(self):
        self.code('consent_required', lambda: self.catalog.add('claude', api_key=KEY))
        self.code('consent_required', lambda: self.catalog.add('codex', api_key=KEY, consent=False))
        self.assertEqual(self.catalog.list(), [])
        self.assertTrue(self.providers.settings()['local_only'])
        self.claude()
        self.assertIs(self.providers.settings()['local_only'], False)

    def test_key_never_returned(self):
        provider = self.claude()
        self.catalog.choose('plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose('coding', provider['id'], 'sonnet')
        with self.platform.store.read() as db:
            stored = json.dumps([list(r) for t in ('jobs', 'ai_providers', 'execution_profile_versions',
                                                    'provider_profile_versions')
                                 for r in db.execute('SELECT * FROM ' + t)])
        public = json.dumps([provider, self.catalog.list(), self.catalog.jobs(),
                             self.providers.list_profiles(), self.profiles.list_profiles()])
        self.assertNotIn(KEY, stored + public)
        self.assertIs(self.catalog.list()[0]['has_key'], True)

    def test_public_endpoints_rejected_as_local(self):
        for endpoint in (
            'http://api.openai.com/v1',
            'https://api.openai.com/v1',
            'http://8.8.8.8/v1',
            'https://8.8.8.8/v1',
            'http://studio.lan/v1',
            'http://0.0.0.0/v1',
            'http://224.0.0.1/v1',
            'http://192.0.2.1/v1',
            'http://[2001:4860:4860::8888]/v1',
        ):
            with self.subTest(endpoint=endpoint):
                self.code(
                    'local_endpoint_required',
                    lambda: self.catalog.add(
                        'local',
                        endpoint=endpoint,
                        adapter='openai_compatible',
                        api_key=KEY))

        self.assertEqual(self.catalog.list(), [])
        self.assertEqual(self.vault.values, {})
        self.assertTrue(
            self.providers.settings()['local_only'])

    def test_private_network_endpoints_remain_supported(self):
        for host in (
            'localhost',
            '127.0.0.1',
            '10.0.0.10',
            '172.16.0.10',
            '192.168.1.10',
            '100.64.0.10',
            '169.254.1.10',
            '[::1]',
            '[fd00::10]',
        ):
            with self.subTest(host=host):
                endpoint = 'http://' + host + ':11434'
                provider = self.local(endpoint=endpoint)

                self.assertEqual(
                    validation.destination_for(endpoint)['kind'],
                    'local')

                self.catalog.remove(provider['id'])

    def test_public_endpoint_update_preserves_configuration(self):
        provider = self.local()
        self.catalog.choose(
            'plans', provider['id'], 'qwen3:8b')

        before = self._deletion_snapshot()

        self.code(
            'local_endpoint_required',
            lambda: self.catalog.update(
                provider['id'],
                endpoint='https://8.8.8.8'))

        self.assertEqual(self._deletion_snapshot(), before)

    def test_legacy_public_endpoint_is_not_trusted_on_restart(self):
        provider = self.local()
        endpoint = 'https://api.openai.com/v1'

        with self.platform.store.transaction() as db:
            db.execute(
                'UPDATE ai_providers SET endpoint=? WHERE id=?',
                (endpoint, provider['id']))

        AIProviders(
            self.platform.store,
            self.secrets,
            self.providers,
            self.profiles)

        self.assertEqual(
            validation.destination_for(endpoint)['kind'],
            'remote')

        self.assertEqual(
            self.catalog.list()[0]['endpoint'], endpoint)

        self.assertTrue(
            self.providers.settings()['local_only'])

    def test_explicit_trust_cannot_override_public_address(self):
        endpoint = 'http://8.8.8.8/v1'

        validation.set_trusted_endpoints([endpoint])

        self.assertFalse(
            validation.is_trusted_endpoint(
                endpoint, extra=[endpoint]))

        with self.assertRaises(Exception):
            validation.validate_endpoint(
                endpoint, trusted=[endpoint])

        self.assertEqual(
            validation.destination_for(endpoint)['kind'],
            'remote')

    def test_local_provider_is_trusted(self):
        provider = self.local()
        self.assertTrue(self.providers.settings()['local_only'])  # no consent needed or asked
        self.catalog.choose('plans', provider['id'], 'qwen3:8b')
        role = self.providers.roles()['planning']
        self.assertEqual((role['destination']['kind'], role['model']), ('local', 'qwen3:8b'))

    def test_remove_local_keeps_shared_host(self):
        first, second = self.local(), self.local()
        self.catalog.remove(first['id'])
        self.assertEqual(validation.destination_for('http://100.64.0.9:11434')['kind'], 'local')
        self.catalog.remove(second['id'])
        self.assertEqual(validation.destination_for('http://100.64.0.9:11434')['kind'], 'remote')

    def test_trust_survives_restart(self):
        self.local()
        validation.set_trusted_endpoints([])
        AIProviders(self.platform.store, self.secrets, self.providers, self.profiles).refresh_trust()
        self.assertEqual(validation.destination_for('http://100.64.0.9:11434')['kind'], 'local')

    def test_local_provider_trusts_only_its_own_address(self):
        handmade = self.providers.create_profile({'name': 'hosted', 'adapter': 'openai_compatible',
                                                  'endpoint': 'https://192.168.1.20/v1', 'model': 'm',
                                                  'config': {'capabilities': ['chat']}})
        self.catalog.add('local', endpoint='http://192.168.1.20/v1', adapter='openai_compatible')
        self.assertEqual(validation.destination_for('https://192.168.1.20/v1')['kind'], 'remote')
        self.assertEqual(validation.destination_for('http://192.168.1.20:8080/v1')['kind'], 'remote')
        self.assertEqual(validation.destination_for('http://192.168.1.20/v1')['kind'], 'local')
        self.assertTrue(self.providers.settings()['local_only'])
        with self.assertRaises(Exception):
            self.providers.assign_role('planning', handmade['id'])

    def test_bad_coding_model_keeps_working_setup(self):
        login = self.catalog.add('claude', use_login=True, consent=True)
        self.catalog.choose('coding', login['id'], 'opus')
        before = self.managed()[0]['id']
        keyed = self.claude()
        vault_before = dict(self.vault.values)
        self.code('validation', lambda: self.catalog.choose('coding', keyed['id'], 'sonnet 5'))
        self.assertEqual([r['id'] for r in self.managed()], [before])
        self.assertEqual(self.vault.values, vault_before)
        self.assertEqual(self.catalog.jobs()['coding']['provider_id'], login['id'])

    def test_clearing_summary_follows_plans_again(self):
        provider = self.local()
        self.catalog.choose('plans', provider['id'], 'qwen3:8b')
        self.catalog.choose('summary', provider['id'], 'qwen3:4b')
        self.catalog.choose('summary', None)
        roles = self.providers.roles()
        self.assertEqual(roles['summary']['profile_id'], roles['planning']['profile_id'])
        self.assertEqual(self.catalog.jobs()['summary']['model'], 'qwen3:8b')

    def test_plans_sets_ask_planning_summary(self):
        provider = self.local()
        self.catalog.choose('plans', provider['id'], 'qwen3:8b')
        roles = self.providers.roles()
        self.assertEqual(len({roles[r]['profile_id'] for r in ('ask', 'planning', 'summary')}), 1)
        self.assertEqual(self.catalog.jobs()['plans']['model'], 'qwen3:8b')

    def test_summary_choice_survives_plans_change(self):
        provider = self.local()
        self.catalog.choose('summary', provider['id'], 'qwen3:4b')
        self.catalog.choose('plans', provider['id'], 'qwen3:8b')
        roles = self.providers.roles()
        self.assertEqual((roles['summary']['model'], roles['planning']['model']), ('qwen3:4b', 'qwen3:8b'))

    def test_same_model_reuses_profile(self):
        provider = self.local()
        self.catalog.choose('plans', provider['id'], 'qwen3:8b')
        self.catalog.choose('summary', provider['id'], 'qwen3:8b')
        self.assertEqual(len(self.providers.list_profiles()), 1)

    def test_coding_creates_managed_profile_and_tests(self):
        provider = self.claude()
        jobs = self.catalog.choose('coding', provider['id'], 'sonnet')
        self.assertEqual(jobs['coding']['status'], 'queued')
        rows = self.managed()
        self.assertEqual(len(rows), 1)
        version = self.profiles.get(rows[0]['id'])['version']
        self.assertEqual((version['auth_mode'], version['config']['agent_model'], version['config']['image']),
                         ('api_key', 'sonnet', 'python:3.12-slim'))
        self.assertIs(version['config']['allow_external_source'], True)
        self.assertIsNone(self.profiles.default_profile_id())
        self.pass_coding_test()
        self.assertEqual(self.profiles.default_profile_id(), rows[0]['id'])
        self.assertEqual(self.catalog.jobs()['coding']['status'], 'passed')
        self.assertEqual(self.profiles.version_api_key(version['id']), KEY)

    def test_coding_with_login_uses_cli_login(self):
        provider = self.catalog.add('claude', use_login=True, consent=True)
        self.catalog.choose('coding', provider['id'], 'opus')
        version = self.profiles.get(self.managed()[0]['id'])['version']
        self.assertEqual((version['auth_mode'], version['has_secret']), ('cli_login', False))

    def test_coding_model_change_versions_the_same_profile(self):
        provider = self.claude()
        self.catalog.choose('coding', provider['id'], 'sonnet')
        self.catalog.choose('coding', provider['id'], 'opus')
        rows = self.managed()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.profiles.get(rows[0]['id'])['version']['config']['agent_model'], 'opus')

    def test_coding_rejects_codex_and_local(self):
        codex = self.catalog.add('codex', api_key=KEY, consent=True)
        self.code('coding_unsupported', lambda: self.catalog.choose('coding', codex['id'], 'gpt-5-codex'))
        self.code('coding_unsupported', lambda: self.catalog.choose('coding', self.local()['id'], 'qwen3:8b'))
        self.assertEqual(self.managed(), [])

    def test_claude_needs_exactly_one_credential(self):
        self.code('validation', lambda: self.catalog.add('claude', consent=True))
        self.code('validation', lambda: self.catalog.add('claude', api_key=KEY, use_login=True, consent=True))
        self.code('validation', lambda: self.catalog.add('codex', consent=True))
        self.code('validation', lambda: self.catalog.add('local'))

    def test_failed_rotation_preserves_original_key(self):
        import sqlite3

        provider = self.claude()
        before = dict(self.vault.values)

        with self.platform.store.transaction() as db:
            db.execute("""
                CREATE TRIGGER reject_provider_update
                BEFORE UPDATE ON ai_providers
                BEGIN
                    SELECT RAISE(ABORT, 'Injected rotation failure');
                END
            """)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, 'Injected rotation failure'
        ):
            self.catalog.update(
                provider['id'],
                api_key='synthetic-rotated-key',
            )

        self.assertEqual(self.catalog.list(), [provider])

        with self.platform.store.read() as db:
            ref = db.execute(
                'SELECT secret_ref FROM ai_providers WHERE id=?',
                (provider['id'],),
            ).fetchone()[0]

        self.assertEqual(self.secrets.resolve(ref).reveal(), KEY)
        self.assertEqual(self.vault.values, before)

    def _rotation_snapshot(self):
        tables = (
            'ai_providers',
            'provider_profiles',
            'provider_profile_versions',
            'execution_profiles',
            'execution_profile_versions',
            'execution_profile_checks',
            'jobs',
        )

        with self.platform.store.read() as db:
            result = {
                table: sorted(
                    repr(tuple(r))
                    for r in db.execute('SELECT * FROM ' + table)
                )
                for table in tables
            }
            result['live_credentials'] = sorted(
                r[0] for r in db.execute(
                    'SELECT id FROM secret_refs WHERE revoked_at IS NULL')
            )

        return result

    def _assert_rotation_rollback(self, service, method):
        from unittest.mock import patch

        provider = self.claude()
        self.catalog.choose(
            'plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose(
            'coding', provider['id'], 'sonnet')

        before = self._rotation_snapshot()
        vault_before = dict(self.vault.values)

        with patch.object(
            service, method,
            side_effect=RuntimeError('Injected rotation failure')
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'Injected rotation failure'
            ):
                self.catalog.update(
                    provider['id'],
                    api_key='synthetic-rotated-key')

        self.assertEqual(self._rotation_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_rotation_rolls_back_on_chat_failure(self):
        self._assert_rotation_rollback(
            self.providers, '_update_profile_in_transaction')

    def test_rotation_rolls_back_on_execution_failure(self):
        self._assert_rotation_rollback(
            self.profiles, '_update_in_transaction')

    def test_rotation_rolls_back_on_test_scheduling_failure(self):
        self._assert_rotation_rollback(
            self.profiles, '_request_test_in_transaction')

    def test_rotation_rolls_back_on_staging_failure(self):
        self._assert_rotation_rollback(
            self.providers, '_create_secret')

    def test_rotation_rolls_back_on_check_insert_failure(self):
        import sqlite3

        provider = self.claude()
        self.catalog.choose(
            'plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose(
            'coding', provider['id'], 'sonnet')

        before = self._rotation_snapshot()
        vault_before = dict(self.vault.values)

        with self.platform.store.transaction() as db:
            db.execute("""
                CREATE TRIGGER reject_check_insert
                BEFORE INSERT ON execution_profile_checks
                BEGIN
                    SELECT RAISE(ABORT, 'Injected check failure');
                END
            """)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, 'Injected check failure'
        ):
            self.catalog.update(
                provider['id'],
                api_key='synthetic-rotated-key')

        self.assertEqual(self._rotation_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_local_endpoint_update_preserves_trust(self):
        provider = self.local()
        self.catalog.choose(
            'plans', provider['id'], 'qwen3:8b')

        endpoint = 'http://100.64.0.10:11434'

        updated = self.catalog.update(
            provider['id'], endpoint=endpoint)

        self.assertEqual(updated['endpoint'], endpoint)

        role = self.providers.roles()['planning']
        version = self.providers.get_version(role['version_id'])

        self.assertEqual(version['endpoint'], endpoint)
        self.assertEqual(
            validation.destination_for(endpoint)['kind'],
            'local')
        self.assertTrue(
            self.providers.settings()['local_only'])

    def test_rotation_rejects_running_execution_check(self):
        provider = self.claude()
        self.catalog.choose(
            'plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose(
            'coding', provider['id'], 'sonnet')

        lease = self.platform.jobs.claim(
            'fixture-worker', kinds=['execution.test'])
        self.assertIsNotNone(lease)

        before = self._rotation_snapshot()
        vault_before = dict(self.vault.values)

        self.code(
            'coding_check_in_progress',
            lambda: self.catalog.update(
                provider['id'],
                api_key='synthetic-rotated-key'))

        self.assertEqual(self._rotation_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_rotate_key_updates_chat_and_coding(self):
        provider = self.claude()
        self.catalog.choose('plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose('coding', provider['id'], 'sonnet')
        chat_before = self.providers.roles()['planning']['version_id']
        coding_before = self.profiles.get(self.managed()[0]['id'])['active_version_id']
        self.catalog.update(provider['id'], api_key='sk-ant-rotated')
        chat_after = self.providers.roles()['planning']['version_id']
        coding_after = self.profiles.get(self.managed()[0]['id'])['active_version_id']
        self.assertNotEqual(chat_before, chat_after)
        self.assertNotEqual(coding_before, coding_after)
        self.assertEqual(self.profiles.version_api_key(coding_after), 'sk-ant-rotated')
        self.assertEqual(self.catalog.jobs()['coding']['status'], 'queued')
        with self.assertRaises(Exception):
            self.profiles.version_api_key(coding_before)  # old ref revoked
        self.assertNotIn(KEY.encode(), list(self.vault.values.values()))

    def _deletion_snapshot(self):
        result = self._rotation_snapshot()

        with self.platform.store.read() as db:
            for table in (
                'ai_provider_profiles',
                'ai_job_choices',
                'model_role_assignments',
                'provider_settings',
            ):
                result[table] = sorted(
                    repr(tuple(r))
                    for r in db.execute('SELECT * FROM ' + table))

        return result

    def _deletion_fixture(self):
        provider = self.claude()
        self.catalog.choose(
            'plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose(
            'coding', provider['id'], 'sonnet')
        return provider

    def _assert_deletion_rollback(self, service, method):
        from unittest.mock import patch

        provider = self._deletion_fixture()
        before = self._deletion_snapshot()
        vault_before = dict(self.vault.values)

        with patch.object(
            service, method,
            side_effect=RuntimeError('Injected deletion failure')
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'Injected deletion failure'
            ):
                self.catalog.remove(provider['id'])

        self.assertEqual(self._deletion_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_deletion_rolls_back_on_chat_failure(self):
        self._assert_deletion_rollback(
            self.providers, '_delete_profile_in_transaction')

    def test_deletion_rolls_back_on_execution_failure(self):
        self._assert_deletion_rollback(
            self.profiles, '_delete_in_transaction')

    def test_deletion_rolls_back_on_database_failure(self):
        import sqlite3

        provider = self._deletion_fixture()
        before = self._deletion_snapshot()
        vault_before = dict(self.vault.values)

        with self.platform.store.transaction() as db:
            db.execute("""
                CREATE TRIGGER reject_provider_delete
                BEFORE DELETE ON ai_providers
                BEGIN
                    SELECT RAISE(ABORT, 'Injected delete failure');
                END
            """)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, 'Injected delete failure'
        ):
            self.catalog.remove(provider['id'])

        self.assertEqual(self._deletion_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_delete_summary_preserves_privacy(self):
        remote = self.claude()
        local = self.local()

        self.catalog.choose(
            'plans', remote['id'], 'claude-sonnet-5-5')
        self.catalog.choose(
            'summary', local['id'], 'qwen3:8b')

        self.providers.update_settings(local_only=True)

        self.catalog.remove(local['id'])

        self.assertEqual(self.catalog.list(), [remote])
        self.assertTrue(
            self.providers.settings()['local_only'])

        roles = self.providers.roles()

        self.assertEqual(
            roles['summary']['profile_id'],
            roles['planning']['profile_id'])
        self.assertEqual(
            roles['summary']['status'], 'blocked')
        self.assertEqual(
            roles['planning']['status'], 'blocked')

        # Inheriting an assignment must not authorize inference.
        from opendevengine.providers.errors import DestinationNotAllowed

        with self.assertRaises(DestinationNotAllowed):
            self.providers.generate(
                'summary',
                [{'role': 'user', 'content': 'Synthetic fixture'}])

    def test_delete_rejects_unverified_containment(self):
        provider = self._deletion_fixture()

        with self.platform.store.read() as db:
            check = db.execute(
                'SELECT version_id, job_id '
                'FROM execution_profile_checks'
            ).fetchone()

        self.platform.jobs.cancel(check['job_id'])

        self.profiles.reconcile_test(
            check['version_id'],
            check['job_id'],
            containment_confirmed=False)

        before = self._deletion_snapshot()
        vault_before = dict(self.vault.values)

        self.code(
            'execution_test_reconciliation_required',
            lambda: self.catalog.remove(provider['id']))

        self.assertEqual(self._deletion_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

        # Deletion is permitted after verified reconciliation.
        self.profiles.reconcile_test(
            check['version_id'],
            check['job_id'],
            containment_confirmed=True)

        self.catalog.remove(provider['id'])
        self.assertEqual(self.catalog.list(), [])
        self.assertEqual(self.vault.values, {})

    def test_delete_rejects_expired_execution_lease(self):
        provider = self._deletion_fixture()

        lease = self.platform.jobs.claim(
            'fixture-worker', kinds=['execution.test'])
        self.assertIsNotNone(lease)

        with self.platform.store.transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_expires_at=? "
                "WHERE kind='execution.test' AND state='leased'",
                (self.profiles.clock() - 1,))

        before = self._deletion_snapshot()
        vault_before = dict(self.vault.values)

        self.code(
            'execution_test_reconciliation_required',
            lambda: self.catalog.remove(provider['id']))

        self.assertEqual(self._deletion_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_deletion_rejects_running_execution_check(self):
        provider = self._deletion_fixture()

        lease = self.platform.jobs.claim(
            'fixture-worker', kinds=['execution.test'])
        self.assertIsNotNone(lease)

        before = self._deletion_snapshot()
        vault_before = dict(self.vault.values)

        self.code(
            'coding_check_in_progress',
            lambda: self.catalog.remove(provider['id']))

        self.assertEqual(self._deletion_snapshot(), before)
        self.assertEqual(self.vault.values, vault_before)

    def test_deletion_cancels_check_and_restores_summary(self):
        local = self.local()
        self.catalog.choose('plans', local['id'], 'qwen3:8b')

        provider = self.claude()
        self.catalog.choose(
            'summary', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose('coding', provider['id'], 'sonnet')

        with self.platform.store.read() as db:
            job_id = db.execute(
                "SELECT id FROM jobs WHERE kind='execution.test' "
                "AND state='queued'").fetchone()[0]

        self.catalog.remove(provider['id'])

        self.assertEqual(
            self.platform.jobs.get(job_id).state, 'cancelled')

        roles = self.providers.roles()

        self.assertEqual(
            roles['summary']['profile_id'],
            roles['planning']['profile_id'])

        self.assertEqual(
            self.catalog.jobs()['summary']['provider_id'],
            local['id'])

        self.assertEqual(self.vault.values, {})

    def test_remove_provider_clears_jobs(self):
        provider = self.claude()
        self.catalog.choose('plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose('coding', provider['id'], 'sonnet')
        self.catalog.remove(provider['id'])
        jobs = self.catalog.jobs()
        self.assertEqual((jobs['coding']['status'], jobs['plans']['status']), ('unassigned', 'unassigned'))
        self.assertEqual(self.managed(), [])
        self.assertIsNone(self.profiles.default_profile_id())
        self.assertEqual(self.providers.list_profiles(), [])
        self.assertEqual(self.catalog.list(), [])
        self.assertEqual(self.vault.values, {})
        self.code('not_found', lambda: self.catalog.remove(provider['id']))

    def test_privacy_back_on_blocks_remote_jobs(self):
        provider = self.claude()
        self.catalog.choose('plans', provider['id'], 'claude-sonnet-5-5')
        self.catalog.choose('coding', provider['id'], 'sonnet')
        self.providers.update_settings(local_only=True)
        jobs = self.catalog.jobs()
        self.assertEqual((jobs['plans']['status'], jobs['coding']['status']), ('blocked', 'blocked'))

    def test_codex_chat_profile_targets_openai(self):
        codex = self.catalog.add('codex', api_key=KEY, consent=True)
        self.catalog.choose('plans', codex['id'], 'gpt-5-codex')
        version = self.providers.get_version(self.providers.roles()['planning']['version_id'])
        self.assertEqual((version['adapter'], version['endpoint']), ('openai_compatible', 'https://api.openai.com/v1'))

    def test_search_requires_ollama_and_probes_dimensions(self):
        from test_providers_service import response
        self.providers.transport.queue.append(response({'embeddings': [[0.1, 0.2, 0.3]]}))
        provider = self.local()
        self.catalog.choose('search', provider['id'], 'nomic-embed-text')
        version = self.providers.get_version(self.providers.roles()['embedding']['version_id'])
        self.assertEqual((version['config']['capabilities'], version['config']['embedding_dimensions']), (['embeddings'], 3))
        codex = self.catalog.add('codex', api_key=KEY, consent=True)
        self.code('search_unsupported', lambda: self.catalog.choose('search', codex['id'], 'text-embedding-3-small'))


class AIProvidersAPITests(unittest.TestCase):
    def setUp(self):
        from opendevengine.services import ApplicationServices
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.platform = Platform.open(Path(self.temp.name))
        self.addCleanup(self.platform.close)
        self.addCleanup(validation.set_trusted_endpoints, [])
        self.services = ApplicationServices(self.platform, provider_transport=FakeTransport())

    def call(self, method, path, body=None):
        return self.services.api.dispatch(method, path, body=body)

    def test_routes_round_trip(self):
        created = self.call('POST', '/api/ai-providers', {'kind': 'local', 'endpoint': 'http://127.0.0.1:11434',
                                                          'adapter': 'ollama'})
        self.assertEqual(created.status, 201)
        pid = created.body['id']
        self.assertEqual([p['id'] for p in self.call('GET', '/api/ai-providers').body['providers']], [pid])
        chosen = self.call('PATCH', '/api/ai-jobs/plans', {'provider_id': pid, 'model': 'qwen3:8b'})
        self.assertEqual(chosen.body['plans']['model'], 'qwen3:8b')
        self.assertEqual(self.call('GET', '/api/ai-jobs').body['plans']['provider_id'], pid)
        self.call('PATCH', '/api/ai-jobs/plans', {'provider_id': None})
        self.assertEqual(self.call('GET', '/api/ai-jobs').body['plans']['status'], 'unassigned')
        self.assertEqual(self.call('DELETE', '/api/ai-providers/' + pid).status, 200)

    def test_consent_error_has_code(self):
        with self.assertRaises(AIProviderError) as caught:
            self.call('POST', '/api/ai-providers', {'kind': 'codex', 'api_key': KEY})
        self.assertEqual((caught.exception.code, caught.exception.http_status), ('consent_required', 400))


if __name__ == '__main__':
    unittest.main()
