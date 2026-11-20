"""Physical workspace usage includes uncheckpointed SQLite sidecar files."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from opendevengine.platform import Platform,DB_FILENAME


class StorageUsageTests(TestCase):
    def test_uncheckpointed_wal_counts_toward_reported_usage(self):
        with TemporaryDirectory() as directory:
            platform=Platform.open(Path(directory))
            try:
                with platform.store.read() as conn:
                    conn.execute('PRAGMA wal_autocheckpoint=0')
                for n in range(8):
                    platform.jobs.enqueue('test.disk',{'number':n,'synthetic':'x'*12000})
                filename=Path(directory)/DB_FILENAME
                wal=Path(str(filename)+'-wal')
                self.assertGreater(wal.stat().st_size,0)
                actual=sum(Path(str(filename)+suffix).stat().st_size for suffix in ('','-wal','-shm'))
                self.assertEqual(platform.store.size_bytes(),actual)
                with platform.store.read() as conn:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                self.assertLess(platform.store.size_bytes(),actual)
            finally:
                platform.close()
