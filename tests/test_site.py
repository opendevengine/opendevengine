"""Static publishing contract: no server or optional dependencies required."""
from html.parser import HTMLParser
from pathlib import Path
import unittest
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1] / 'site'


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.ids = []
        self.title_parts = []
        self.in_title = False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'title':
            self.in_title = True

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)


class SiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(ROOT.rglob('*.html'))
        cls.documents = {p: Document(p.read_text(encoding='utf-8')) for p in cls.files}

    def test_pages_and_landmarks(self):
        self.assertTrue(self.files, 'The site must contain HTML pages')
        for path, doc in self.documents.items():
            with self.subTest(page=path.relative_to(ROOT)):
                self.assertTrue(any(t == 'html' and a.get('lang') for t, a in doc.tags))
                self.assertEqual(sum(t == 'title' for t, _ in doc.tags), 1)
                self.assertTrue(''.join(doc.title_parts).strip())
                self.assertTrue(any(t == 'meta' and a.get('name') == 'viewport'
                                    and 'width=device-width' in a.get('content', '') for t, a in doc.tags))
                self.assertEqual(sum(t == 'h1' for t, _ in doc.tags), 1)
                self.assertEqual(sum(t == 'main' for t, _ in doc.tags), 1)
                self.assertEqual(len(doc.ids), len(set(doc.ids)), 'Duplicate fragment IDs')
                styles = [a.get('href') for t, a in doc.tags
                          if t == 'link' and a.get('rel') == 'stylesheet']
                self.assertEqual(len(styles), 1, 'Every page shares one stylesheet')
                self.assertEqual((path.parent / styles[0]).resolve(), ROOT / 'assets/styles.css')
                icons = [a for t, a in doc.tags
                         if t == 'link' and 'icon' in a.get('rel', '').split()]
                self.assertEqual(len(icons), 1, 'Every page declares a local favicon')
                self.assertEqual(icons[0].get('type'), 'image/svg+xml')
                self.assertEqual((path.parent / icons[0].get('href', '')).resolve(),
                                 ROOT / 'assets/favicon.svg')
                self.assertTrue(any(t == 'a' and a.get('href') == '#main' for t, a in doc.tags))
                self.assertTrue(any(a.get('aria-current') == 'page' for _, a in doc.tags))

    def test_links_and_fragments(self):
        for path, doc in self.documents.items():
            for tag, attrs in doc.tags:
                for key in ('href', 'src'):
                    if key not in attrs:
                        continue
                    value = attrs[key]
                    with self.subTest(page=path.relative_to(ROOT), tag=tag, link=value):
                        parsed = urlsplit(value)
                        if parsed.scheme or parsed.netloc:
                            # GitHub is an intentional external navigation destination.
                            self.assertEqual(key, 'href', 'Off-site resource')
                            self.assertEqual(tag, 'a', 'Off-site resource')
                            self.assertEqual(parsed.scheme, 'https')
                            self.assertEqual(parsed.netloc, 'github.com')
                            continue
                        self.assertFalse(parsed.path.startswith('/'), 'Root-relative URL breaks Pages subpaths')
                        target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                        self.assertTrue(target.is_relative_to(ROOT.resolve()), 'Link escapes published site')
                        self.assertTrue(target.is_file(), f'Missing file: {target.name}')
                        if parsed.fragment:
                            self.assertIn(target, self.documents, 'Fragment target must be HTML')
                            self.assertIn(unquote(parsed.fragment), self.documents[target].ids)

    def test_resources_and_privacy(self):
        for path, doc in self.documents.items():
            with self.subTest(page=path.relative_to(ROOT)):
                text = path.read_text(encoding='utf-8')
                self.assertNotRegex(text, r'/(?:Users|home)/')
                self.assertFalse(any(t in {'base', 'iframe'} for t, _ in doc.tags))
                for tag, attrs in doc.tags:
                    self.assertFalse(any(k.startswith('on') for k in attrs), 'Inline event handler')
                    if tag == 'img':
                        self.assertIn('alt', attrs)
                        self.assertTrue(attrs['alt'].strip())
                    if tag == 'script' or (tag == 'link' and 'stylesheet' in attrs.get('rel', '').split()):
                        parsed = urlsplit(attrs.get('src' if tag == 'script' else 'href', ''))
                        self.assertFalse(parsed.scheme or parsed.netloc, 'External script/stylesheet')
        styles = list(ROOT.rglob('*.css'))
        self.assertEqual(len(styles), 1, 'Exactly one shared stylesheet')
        css = styles[0].read_text(encoding='utf-8')
        self.assertNotRegex(css, r'@import|https?://|transition\s*:\s*all')
        self.assertLessEqual(len(list(ROOT.rglob('*.js'))), 1)


if __name__ == '__main__':
    unittest.main()
