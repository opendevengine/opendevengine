"""Subprocess smoke check of offline benchmark; no latency assertions."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class BenchmarkLatencyTests(unittest.TestCase):
    def test_reduced_run(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, str(root / 'scripts/benchmark_latency.py'),
             '--catalog-reads', '2', '--routing-repeats', '2', '--prompt-limit', '2'],
            cwd=root, capture_output=True, text=True, timeout=60, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result['fixture']['repositories'], 250)
        self.assertEqual(result['fixture']['indexed_repositories'], 25)
        self.assertEqual(result['fixture']['labelled_prompts'], 2)
        self.assertGreater(result['fixture']['chunks'], 0)
        self.assertEqual(result['routing']['cold']['samples'], 2)
        self.assertEqual(result['routing']['warm']['samples'], 4)
        pages = result['catalog']['warm_by_page']
        self.assertEqual(sum(p['variant'] == 'all' for p in pages), 3)
        self.assertEqual({p['variant'] for p in pages},
                         {'all', 'broad', 'selective', 'no_match'})
        self.assertTrue(all(p['samples'] == 2 for p in pages))
        self.assertEqual(result['catalog']['warm']['samples'], 2 * len(pages))
        for gate in ('catalog', 'routing'):
            for phase in ('cold', 'warm'):
                stats = result[gate][phase]
                self.assertLessEqual(stats['p50_ms'], stats['p95_ms'])
                self.assertLessEqual(stats['p95_ms'], stats['max_ms'])
        self.assertIn('sqlite', result['environment'])
        self.assertIn('cpu_count', result['environment'])

    def test_source_chunks_reach_exact_target(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, str(root / 'scripts/benchmark_latency.py'), '--catalog-reads', '1',
             '--routing-repeats', '1', '--prompt-limit', '1', '--source-chunks', '250'],
            cwd=root, capture_output=True, text=True, timeout=60, check=True)
        fixture = json.loads(completed.stdout)['fixture']
        self.assertEqual(fixture['chunks'], 250)
        self.assertGreater(fixture['files'], 25)
        self.assertGreater(fixture['database_bytes'], 0)
