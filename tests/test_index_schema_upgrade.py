"""Exercise real migration4 data through the corrected namespace identity."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from opendevengine.platform import Store
from opendevengine.platform import migrations


class IndexSchemaUpgradeTests(TestCase):
    def test_upgrade_preserves_cited_namespace_and_accepts_rotated_version(self):
        with TemporaryDirectory() as directory:
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:4]):
                store = Store.open(Path(directory))
            with store.transaction() as c:
                c.execute("INSERT INTO provider_profiles VALUES('p','embedding','v1',NULL,1,1)")
                for version in (1, 2):
                    c.execute("INSERT INTO provider_profile_versions VALUES(?, 'p', ?, 'ollama', 'http://127.0.0.1:11434','nomic',NULL,'{}',1)", (f'v{version}', version))
                c.execute("INSERT INTO embedding_namespaces VALUES('n1','same-model','v1',3,'unit','c1',1)")
                c.execute("INSERT INTO index_sources(id,kind,name,created_at,updated_at) VALUES('demo:x','demo','x',1,1)")
                c.execute("INSERT INTO repository_snapshots(id,repository_id,revision,revision_kind,state,namespace_id,chunker_version,parser_version,created_at) VALUES('s1','demo:x','hash','content_hash','ready','n1','c1','p1',1)")
                c.execute("UPDATE index_sources SET active_snapshot_id='s1' WHERE id='demo:x'")
            store.close()
            store = Store.open(Path(directory))
            try:
                with store.transaction() as c:
                    c.execute("INSERT INTO embedding_namespaces VALUES('n2','same-model','v2',3,'unit','c1',2)")
                    self.assertEqual(c.execute("SELECT namespace_id FROM repository_snapshots WHERE id='s1'").fetchone()[0], 'n1')
                    self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction() as c:
                        c.execute("INSERT INTO embedding_namespaces VALUES('n3','same-model','v2',3,'unit','c1',3)")
                self.assertEqual(store.schema_version(), migrations.LATEST_VERSION)
            finally:
                store.close()
