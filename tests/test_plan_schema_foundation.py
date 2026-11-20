"""Exercise deferred plan/version insertion and rollback on a real Store."""
import sqlite3
from unittest import TestCase
from opendevengine.platform import Platform


class AskSchemaTests(TestCase):
    def test_plan_version_cycle_is_atomic_and_scope_generation_is_pinned(self):
        p = Platform.open(None)
        try:
            with p.store.transaction() as c:
                c.execute("INSERT INTO conversations VALUES('c','task','[]',2,1,1,NULL)")
                c.execute("INSERT INTO plans VALUES('p','c','task','v',1,1,NULL)")
                c.execute("INSERT INTO plan_versions(id,plan_id,version,scope_revision,content_json,content_hash,source,created_at) VALUES('v','p',1,2,'{}','hash','manual',1)")
            with self.assertRaises(sqlite3.IntegrityError):
                with p.store.transaction() as c:
                    c.execute("INSERT INTO plans VALUES('broken',NULL,'task','absent',1,1,NULL)")
            with p.store.read() as c:
                self.assertEqual(c.execute("SELECT scope_revision FROM plan_versions WHERE id='v'").fetchone()[0], 2)
                self.assertIsNone(c.execute("SELECT id FROM plans WHERE id='broken'").fetchone())
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            p.close()
