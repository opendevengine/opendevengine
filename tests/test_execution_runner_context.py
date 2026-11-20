"""Runner file modes and read-only reference context.

Pure/mocked-CLI checks need no Docker. ``RealDockerContextTest`` runs only with
OPENDEVENGINE_DOCKER_TESTS=1 and a locally present image (OPENDEVENGINE_TEST_IMAGE, default
python:3.12-slim); it never pulls and only touches its own labelled containers.
"""
import io
import json
import os
import secrets
import tarfile
import threading
import unittest

from opendevengine.execution import runner as R
from opendevengine.execution.runner import (BoundedResult, DockerRunner, RunnerError, RunnerHandle, parse_workspace_tar,
                                      validate_limits)

DOCKER_TESTS = os.environ.get('OPENDEVENGINE_DOCKER_TESTS') == '1'
TEST_IMAGE = os.environ.get('OPENDEVENGINE_TEST_IMAGE', 'python:3.12-slim')


def ok(stdout=b''):
    return BoundedResult(0, stdout, b'', False, False, False)


class ModesAndArchivesTest(unittest.TestCase):
    def test_upload_archive_preserves_only_regular_modes(self):
        data = R._tar_files({'run.sh': b'#!/bin/sh\n', 'a.txt': b'x'}, {'run.sh': R.EXECUTABLE_MODE})
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            modes = {m.name: m.mode for m in archive.getmembers()}
        self.assertEqual(modes, {'run.sh': 0o755, 'a.txt': 0o644})
        self.assertEqual(R._validate_modes({'run.sh': 0o755, 'a.txt': '100644'}, {'run.sh': b'', 'a.txt': b''}),
                         {'run.sh': '100755', 'a.txt': '100644'})
        for bad in ({'run.sh': 0o777}, {'run.sh': '120000'}, {'missing': 0o755}, {'../x': 0o644}, ['run.sh']):
            with self.subTest(bad=bad), self.assertRaises(RunnerError):
                R._validate_modes(bad, {'run.sh': b''})

    def test_snapshot_parse_reports_modes_without_changing_bytes_api(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as archive:
            for name, mode in (('x.sh', 0o700), ('y.txt', 0o600)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = 1, mode
                archive.addfile(info, io.BytesIO(b'1'))
        limits = validate_limits({})
        self.assertEqual(parse_workspace_tar(buf.getvalue(), limits), {'x.sh': b'1', 'y.txt': b'1'})
        modes = {}
        parse_workspace_tar(buf.getvalue(), limits, modes)
        self.assertEqual(modes, {'x.sh': '100755', 'y.txt': '100644'})

    def test_context_tmpfs_is_separate_root_owned_noexec_and_bounded(self):
        limits = validate_limits({})
        size, files = R._context_size({'repo2/a.py': b'abc'}, limits)
        self.assertEqual(files, {'repo2/a.py': b'abc'})
        args = DockerRunner._create_args(image_id='sha256:' + 'c' * 64, instance_id='i', session_id='s', attempt_id='a',
                                         limits=limits, context_bytes=size)
        tmpfs = [args[i + 1] for i, a in enumerate(args) if a == '--tmpfs']
        context = [t for t in tmpfs if t.startswith('/context:')]
        self.assertEqual(len(context), 1)
        for option in ('noexec', 'nosuid', 'nodev', 'uid=0', 'gid=0', 'mode=0755'):
            self.assertIn(option, context[0])
        self.assertFalse(any(t.startswith('/workspace/') for t in tmpfs))
        self.assertIn('org.opendevengine.context=1', args)
        plain = DockerRunner._create_args(image_id='sha256:' + 'c' * 64, instance_id='i', session_id='s', attempt_id='a',
                                          limits=limits)
        self.assertFalse([a for a in plain if a.startswith('/context')])
        with self.assertRaises(RunnerError):
            R._context_size({'big': b'x' * (2 * 1024 * 1024)}, validate_limits({'workspace_mib': 1, 'max_file_bytes': 1048576}))
        with self.assertRaises(RunnerError):
            R._context_size({'../escape': b'x'}, limits)

    def test_hostconfig_requires_exact_tmpfs_set(self):
        limits = validate_limits({})
        info = {'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                               'SecurityOpt': ['no-new-privileges=true'], 'PidsLimit': 64, 'Memory': 512 * 1024 * 1024,
                               'Tmpfs': {'/workspace': '', '/tmp': '', '/context': ''}},
                'Mounts': [], 'Config': {'User': '65534:65534'}}
        DockerRunner._verify_hostconfig(info, limits, context=True)
        with self.assertRaises(RunnerError):
            DockerRunner._verify_hostconfig(info, limits)  # unexpected extra mount
        info['HostConfig']['Tmpfs'] = {'/workspace': '', '/tmp': ''}
        with self.assertRaises(RunnerError):
            DockerRunner._verify_hostconfig(info, limits, context=True)


class MockedProvisionTest(unittest.TestCase):
    """Drive provision() through a recorded fake Docker CLI to check ordering/identity."""

    def runner(self, probe_rc=0):
        runner = DockerRunner.__new__(DockerRunner)
        runner.instance_id, runner._docker, runner._env = 'inst', '/usr/bin/docker', {}
        runner._limits, runner._lock = {}, threading.Lock()
        cid = 'a' * 64
        image = 'sha256:' + 'b' * 64
        self.calls = []

        def cli(args, *, timeout=30, max_stdout=0, stdin=None):
            self.calls.append((list(args), stdin))
            if args[0] == 'version':
                return ok(json.dumps({'Os': 'linux', 'Version': '27'}).encode())
            if args[:2] == ['image', 'inspect']:
                return ok(json.dumps({'Id': image, 'Os': 'linux', 'Architecture': 'arm64', 'Config': {}}).encode())
            if args[0] == 'ps':
                return ok(b'')
            if args[0] == 'create':
                labels = dict(a.split('=', 1) for a in [args[i + 1] for i, x in enumerate(args) if x == '--label'])
                self.labels = labels
                return ok(cid.encode())
            if args[:2] == ['container', 'inspect']:
                return ok(json.dumps({'Id': cid, 'Image': image, 'Config': {'Labels': self.labels, 'User': '65534:65534'},
                                      'Mounts': [], 'HostConfig': {
                                          'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                                          'SecurityOpt': ['no-new-privileges=true'], 'PidsLimit': 64,
                                          'Memory': 512 * 1024 * 1024,
                                          'Tmpfs': {'/workspace': '', '/tmp': '', '/context': ''}}}).encode())
            if args[0] == 'exec' and _CONTEXT_PROBE in args:
                return BoundedResult(probe_rc, b'', b'', False, False, False)
            return ok()
        runner._cli = cli
        return runner

    def test_context_populated_as_root_then_locked_then_probed_as_actor(self):
        runner = self.runner()
        runner.provision('sess', {'run.sh': b'#!/bin/sh\n'}, image='python:3.12-slim', limits={},
                         file_modes={'run.sh': 0o755}, context={'lib/ref.py': b'X = 1\n'})
        execs = [c for c in self.calls if c[0][0] == 'exec']
        users = [c[0][c[0].index('-u') + 1] for c in execs]
        workspace = next(c for c in execs if c[0][-6:-3] == ['-x', '-f', '-'] or ('-C' in c[0] and c[0][c[0].index('-C') + 1] == '/workspace'))
        with tarfile.open(fileobj=io.BytesIO(workspace[1])) as archive:
            self.assertEqual(archive.getmember('run.sh').mode, 0o755)
        context_upload = next(c for c in execs if '-C' in c[0] and c[0][c[0].index('-C') + 1] == '/context')
        self.assertEqual(context_upload[0][context_upload[0].index('-u') + 1], '0:0')
        with tarfile.open(fileobj=io.BytesIO(context_upload[1])) as archive:
            self.assertEqual(archive.getmember('lib/ref.py').uid, 0)
        order = [('lock' if R._CONTEXT_LOCK_SCRIPT in c[0] else 'probe' if _CONTEXT_PROBE in c[0] else
                  'ctx' if c is context_upload else 'ws' if c is workspace else 'other') for c in execs]
        self.assertEqual([o for o in order if o != 'other'], ['ws', 'ctx', 'lock', 'probe'])
        self.assertEqual(users[order.index('probe')], '65534:65534')
        self.assertEqual(users[order.index('lock')], '0:0')
        create = next(c for c in self.calls if c[0][0] == 'create')[0]
        self.assertEqual(create[create.index('--user') + 1], '65534:65534')

    def test_writable_context_fails_closed_and_removes_container(self):
        runner = self.runner(probe_rc=1)
        with self.assertRaises(RunnerError) as caught:
            runner.provision('sess', {}, image='python:3.12-slim', limits={}, context={'a': b'1'})
        self.assertEqual(caught.exception.code, 'isolation_unverified')
        self.assertTrue(any(c[0][0] == 'rm' for c in self.calls))


_CONTEXT_PROBE = R._CONTEXT_PROBE_SCRIPT


@unittest.skipUnless(DOCKER_TESTS, 'set OPENDEVENGINE_DOCKER_TESTS=1 to run real Docker context checks')
class RealDockerContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = DockerRunner('ctx' + secrets.token_hex(4))
        pre = cls.runner.preflight(TEST_IMAGE)
        if pre['blocking_reasons']:
            raise unittest.SkipTest('preflight blocked: %s' % pre['blocking_reasons'])

    @classmethod
    def tearDownClass(cls):
        for handle in cls.runner.list_owned():
            cls.runner.stop(handle)
            cls.runner.discard(handle)

    def sh(self, handle, script):
        return self.runner.run_command(handle, ['sh', '-c', script], timeout_seconds=30)

    def test_modes_preserved_context_readable_but_immutable(self):
        handle = self.runner.provision('ctxs', {'run.sh': b'#!/bin/sh\necho ran\n', 'a.txt': b'a'}, image=TEST_IMAGE,
                                       limits={}, file_modes={'run.sh': 0o755},
                                       context={'other/ref.txt': b'REFERENCE\n'})
        self.addCleanup(lambda: (self.runner.stop(handle), self.runner.discard(handle)))
        self.assertEqual(self.sh(handle, './run.sh')['stdout'].strip(), 'ran')
        self.assertEqual(self.sh(handle, 'cat /context/other/ref.txt')['stdout'], 'REFERENCE\n')
        for attack in ('echo x > /context/other/ref.txt', 'touch /context/new', 'mv /context/other /context/o2',
                       'chmod 0777 /context/other/ref.txt', 'rm -f /context/other/ref.txt',
                       'ln -s /workspace /context/other/l', 'cp /bin/sh /context/sh'):
            with self.subTest(attack=attack):
                self.assertNotEqual(self.sh(handle, attack)['exit_code'], 0)
        self.assertEqual(self.sh(handle, 'cat /context/other/ref.txt')['stdout'], 'REFERENCE\n')
        self.sh(handle, 'chmod +x a.txt && chmod -x run.sh')
        files, modes = self.runner.snapshot(handle, include_modes=True)
        self.assertEqual((modes['a.txt'], modes['run.sh']), ('100755', '100644'))
        self.assertNotIn('other/ref.txt', files)
        self.assertEqual(self.runner.snapshot(handle), files)


if __name__ == '__main__':
    unittest.main()
