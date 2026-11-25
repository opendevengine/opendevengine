"""Migration 16: execution profiles accept an API-key auth mode and a managed flag."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from opendevengine.platform import Store
from opendevengine.platform import migrations


class MigrationSixteenTests(TestCase):
    def open_v15_with_profile(self, directory):
        with patch.object(migrations, 'MIGRATIONS', tuple(m for m in migrations.MIGRATIONS if m.version <= 15)):
            store = Store.open(Path(directory))
        with store.transaction() as c:
            c.execute("INSERT INTO execution_profiles(id,name,active_version_id,created_at,updated_at) VALUES('ep','Personal CLI','epv',1,1)")
            c.execute("INSERT INTO execution_profile_versions VALUES('epv','ep',1,'claude_code','cli_login','{\"image\":\"x\"}',1)")
        store.close()
        return Store.open(Path(directory))

    def test_migration_16_keeps_existing_versions(self):
        with TemporaryDirectory() as directory:
            store = self.open_v15_with_profile(directory)
            try:
                with store.read() as c:
                    row = c.execute("SELECT * FROM execution_profile_versions WHERE id='epv'").fetchone()
                    self.assertEqual((row['auth_mode'], row['config_json'], row['secret_ref']), ('cli_login', '{"image":"x"}', None))
                    self.assertEqual(c.execute("SELECT managed FROM execution_profiles WHERE id='ep'").fetchone()[0], 0)
                    self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(store.schema_version(), migrations.LATEST_VERSION)
            finally:
                store.close()

    def test_migration_16_allows_api_key_auth(self):
        with TemporaryDirectory() as directory:
            store = self.open_v15_with_profile(directory)
            try:
                with store.transaction() as c:
                    c.execute("INSERT INTO secret_refs VALUES('ref','memory','provider_api_key','coding',1,NULL)")
                    c.execute("INSERT INTO execution_profile_versions(id,profile_id,version,adapter,auth_mode,config_json,created_at,secret_ref) "
                              "VALUES('epv2','ep',2,'claude_code','api_key','{}',2,'ref')")
                with self.assertRaises(sqlite3.IntegrityError):  # an API-key version must name its key
                    with store.transaction() as c:
                        c.execute("INSERT INTO execution_profile_versions(id,profile_id,version,adapter,auth_mode,config_json,created_at) "
                                  "VALUES('epv4','ep',4,'claude_code','api_key','{}',4)")
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as c:
                        c.execute("INSERT INTO execution_profile_versions(id,profile_id,version,adapter,auth_mode,config_json,created_at) "
                                  "VALUES('epv3','ep',3,'claude_code','token','{}',3)")
                with store.transaction() as c:
                    c.execute("INSERT INTO ai_providers(id,kind,name,endpoint,auth,secret_ref,created_at,updated_at) "
                              "VALUES('p','local','Local','http://127.0.0.1:11434','none',NULL,1,1)")
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as c:
                        c.execute("INSERT INTO ai_providers(id,kind,name,endpoint,auth,secret_ref,created_at,updated_at) "
                                  "VALUES('q','gemini','G',NULL,'none',NULL,1,1)")
            finally:
                store.close()
