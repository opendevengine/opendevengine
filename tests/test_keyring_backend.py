import types
import unittest

from opendevengine.platform.keyring_backend import OSKeyringBackend
from opendevengine.platform.errors import SecretStoreUnavailable


class PasswordDeleteError(Exception):
    pass


class Vault:
    priority = 1
    def __init__(self):
        self.values = {}
        self.failure = False
    def set_password(self, service, key, value):
        if self.failure:
            raise RuntimeError('private-secret-value')
        self.values[(service, key)] = value
    def get_password(self, service, key):
        if self.failure:
            raise RuntimeError('private-secret-value')
        return self.values.get((service, key))
    def delete_password(self, service, key):
        if self.failure:
            raise RuntimeError('private-secret-value')
        if self.values.pop((service, key), None) is None:
            raise PasswordDeleteError('missing')


class OSKeyringTests(unittest.TestCase):
    def make_backend(self, platform='darwin'):
        vault = Vault()
        seen = []
        def loader(name):
            seen.append(name)
            if name == 'keyring.errors':
                return types.SimpleNamespace(PasswordDeleteError=PasswordDeleteError)
            return types.SimpleNamespace(Keyring=lambda: vault, WinVaultKeyring=lambda: vault)
        return OSKeyringBackend(platform_name=platform, module_loader=loader), vault, seen

    def test_explicit_secure_backend_and_binary_roundtrip(self):
        backend, vault, seen = self.make_backend()
        self.assertEqual(seen, ['keyring.backends.macOS', 'keyring.errors'])
        backend.put('sec_test', b'private-secret-value\x00\xff')
        self.assertEqual(backend.get('sec_test'), b'private-secret-value\x00\xff')
        self.assertTrue(backend.status().available)
        self.assertNotIn('private-secret-value', repr(vault.values))
        backend.delete('sec_test')
        backend.delete('sec_test')
        with self.assertRaises(KeyError):
            backend.get('sec_test')

    def test_no_discovery_fallback_when_unavailable(self):
        backend = OSKeyringBackend(platform_name='unsupported', module_loader=lambda _: self.fail('Must not discover'))
        self.assertFalse(backend.status().available)
        with self.assertRaises(SecretStoreUnavailable):
            backend.put('sec_test', b'value')
        def unavailable(name):
            raise ImportError('private-secret-value')
        backend = OSKeyringBackend(platform_name='linux', module_loader=unavailable)
        self.assertFalse(backend.status().available)

    def test_backend_errors_are_sanitized_without_secret_context(self):
        backend, vault, _ = self.make_backend()
        vault.failure = True
        for operation in [lambda: backend.put('sec_test', b'value'), lambda: backend.get('sec_test'), lambda: backend.delete('sec_test')]:
            with self.assertRaises(SecretStoreUnavailable) as caught:
                operation()
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn('private-secret-value', str(caught.exception))

    def test_corrupt_or_unbounded_values_fail_closed(self):
        backend, vault, _ = self.make_backend()
        for corrupted in ['!!', '']:
            vault.values[(backend.SERVICE, 'sec_test')] = corrupted
            with self.assertRaises(SecretStoreUnavailable):
                backend.get('sec_test')
        for value in [b'', b'x' * (128 * 1024 + 1), 'plaintext']:
            with self.assertRaises(ValueError):
                backend.put('sec_test', value)


if __name__ == '__main__':
    unittest.main()
