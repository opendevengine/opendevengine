"""Every web module the UI imports is served; a missing entry breaks the whole app at load."""
import re
import unittest
from pathlib import Path
from opendevengine.__main__ import ASSETS

WEB = Path(__file__).resolve().parent.parent / 'opendevengine' / 'web'
IMPORT = re.compile(r'''(?:from\s+|import\s*\(\s*)["']\./([A-Za-z0-9_.-]+\.js)["']''')


class WebAssetsTest(unittest.TestCase):
    def test_every_imported_module_is_served(self):
        imported = {name for path in WEB.glob('*.js') for name in IMPORT.findall(path.read_text())}
        self.assertTrue(imported, 'expected at least one module import')
        missing = sorted(name for name in imported if '/' + name not in ASSETS)
        self.assertEqual(missing, [])

    def test_every_served_file_exists(self):
        for route, (name, _) in ASSETS.items():
            self.assertTrue((WEB / name).is_file(), route)


if __name__ == '__main__':
    unittest.main()
