"""Workspace mode reports a connected GitHub catalog honestly."""
from unittest import TestCase
from opendevengine.platform.workspace import apply_github_status


def summary():
    return {'mode': 'demo', 'catalog': {'source': 'demo_fixtures', 'github': {'status': 'not_available'}}}


class WorkspaceModeTests(TestCase):
    def test_connected_github_switches_mode_and_source(self):
        result = apply_github_status(summary(), {'status': 'connected', 'repository_count': 3})
        self.assertEqual(result['mode'], 'connected')
        self.assertEqual(result['catalog']['source'], 'github')
        self.assertEqual(result['catalog']['github']['repository_count'], 3)

    def test_unconnected_github_keeps_demo_mode(self):
        result = apply_github_status(summary(), {'status': 'configured'})
        self.assertEqual(result['mode'], 'demo')
        self.assertEqual(result['catalog']['source'], 'demo_fixtures')
        self.assertEqual(result['catalog']['github'], {'status': 'configured'})
