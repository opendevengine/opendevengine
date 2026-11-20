"""Runner boundary checks.

Pure checks need no Docker. ``RealDockerIsolationTest`` runs only when
OPENDEVENGINE_DOCKER_TESTS=1 and uses a locally present image
(OPENDEVENGINE_TEST_IMAGE, default debian:stable-slim); it never pulls and only
touches containers labelled with its own random instance id.
"""
import io
import json
import os
import unittest.mock
from pathlib import Path
import secrets
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest

from opendevengine.execution import runner as R
from opendevengine.execution.runner import (
    DEFAULT_LIMITS, DockerRunner, RunnerError, RunnerHandle, compare_snapshots,
    parse_workspace_tar, run_bounded, validate_limits, validate_relative_path,
)


def _tar(entries, fmt=tarfile.GNU_FORMAT):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=fmt) as tf:
        for info, data in entries:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _reg(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name, kind, linkname=''):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


class PathAndLimitValidationTest(unittest.TestCase):
    def test_rejects_traversal_absolute_and_control_paths(self):
        for bad in ['', '/etc/passwd', '../x', 'a/../b', 'a//b', 'a\\b', 'a\nb', 'nul\x00', '.', 'a/.',
                    'x' * 300, None, 7]:
            with self.subTest(bad=bad), self.assertRaises(RunnerError) as cm:
                validate_relative_path(bad)
            self.assertEqual(cm.exception.code, 'unsafe_path')

    def test_normalizes_safe_relative_paths(self):
        self.assertEqual(validate_relative_path('./src/app.py'), 'src/app.py')
        self.assertEqual(validate_relative_path('dir with space/ü.txt'), 'dir with space/ü.txt')

    def test_limits_are_finite_known_and_bounded(self):
        self.assertEqual(validate_limits(None), DEFAULT_LIMITS)
        for bad in [{'docker_args': 1}, {'cpus': True}, {'pids': float('inf')}, {'pids': float('nan')},
                    {'pids': 10_000}, {'memory_mib': 128, 'workspace_mib': 100}, {'wall_seconds': '60'},
                    {'max_file_bytes': 2 * 1024 * 1024, 'workspace_mib': 1}]:
            with self.subTest(bad=bad), self.assertRaises(RunnerError):
                validate_limits(bad)
        self.assertEqual(validate_limits({'pids': 32.0})['pids'], 32)

    def test_provision_inputs_rejected_before_any_docker_use(self):
        limits = validate_limits({})
        with self.assertRaises(RunnerError):
            R._validate_files({'../escape': b'x'}, limits)
        with self.assertRaises(RunnerError):
            R._validate_files({'a': b'x', 'a/b': b'y'}, limits)
        with self.assertRaises(RunnerError) as cm:
            R._validate_files({'big': b'x' * (limits['max_file_bytes'] + 1)}, limits)
        self.assertEqual(cm.exception.code, 'limit_exceeded')
        for bad in ['', '-rm', 'a/b', 'x' * 49, 'semi;colon']:
            with self.subTest(bad=bad), self.assertRaises(RunnerError):
                R._validate_id(bad, 'session_id')

    def test_handle_repr_hides_container_id(self):
        cid = 'a' * 64
        handle = RunnerHandle('inst', 'sess', cid, 'sha256:' + 'b' * 64)
        self.assertNotIn(cid, repr(handle))
        with self.assertRaises(Exception):
            handle.session_id = 'other'


class TarParsingTest(unittest.TestCase):
    """Snapshots are parsed in memory; nothing lands on the host filesystem."""

    def setUp(self):
        self.limits = validate_limits({})
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        leftovers = os.listdir(self.tmp)
        os.rmdir(self.tmp)
        self.assertEqual(leftovers, [], 'tar parsing must never extract onto the host')

    def test_regular_files_and_dirs_parse(self):
        d = tarfile.TarInfo('./src')
        d.type = tarfile.DIRTYPE
        root = tarfile.TarInfo('.')
        root.type = tarfile.DIRTYPE
        data = _tar([(root, None), (d, None), _reg('./src/a.py', b'print(1)\n'),
                     _reg('./' + 'L' * 120 + '/n.txt', b'long')])
        files = parse_workspace_tar(data, self.limits)
        self.assertEqual(files['src/a.py'], b'print(1)\n')
        self.assertEqual(files['L' * 120 + '/n.txt'], b'long')

    def test_links_devices_and_traversal_rejected(self):
        cases = [
            (_special('./link', tarfile.SYMTYPE, '/etc/passwd'), 'snapshot_rejected'),
            (_special('./hard', tarfile.LNKTYPE, './a'), 'snapshot_rejected'),
            (_special('./dev', tarfile.CHRTYPE), 'snapshot_rejected'),
            (_special('./blk', tarfile.BLKTYPE), 'snapshot_rejected'),
            (_special('./fifo', tarfile.FIFOTYPE), 'snapshot_rejected'),
            (_reg('../escape', b'x'), 'unsafe_path'),
            (_reg('/etc/cron.d/x', b'x'), 'unsafe_path'),
            (_reg('./a/../../b', b'x'), 'unsafe_path'),
        ]
        for entry, code in cases:
            with self.subTest(name=entry[0].name):
                with self.assertRaises(RunnerError) as cm:
                    parse_workspace_tar(_tar([_reg('./a', b'ok'), entry]), self.limits)
                self.assertEqual(cm.exception.code, code)

    def test_pax_path_override_is_validated(self):
        info, data = _reg('./innocent', b'x')
        info.pax_headers = {'path': '../../etc/evil'}
        with self.assertRaises(RunnerError) as cm:
            parse_workspace_tar(_tar([(info, data)], fmt=tarfile.PAX_FORMAT), self.limits)
        self.assertEqual(cm.exception.code, 'unsafe_path')

    def test_over_limit_artifacts_rejected_explicitly(self):
        small = validate_limits({'max_files': 2, 'max_file_bytes': 10})
        with self.assertRaises(RunnerError) as cm:
            parse_workspace_tar(_tar([_reg('./big', b'x' * 11)]), small)
        self.assertEqual(cm.exception.code, 'limit_exceeded')
        with self.assertRaises(RunnerError) as cm:
            parse_workspace_tar(_tar([_reg(f'./f{i}', b'x') for i in range(3)]), small)
        self.assertEqual(cm.exception.code, 'limit_exceeded')
        with self.assertRaises(RunnerError) as cm:
            parse_workspace_tar(b'not a tar archive' * 40, self.limits)
        self.assertEqual(cm.exception.code, 'snapshot_rejected')

    def test_manifest_comparison_reports_added_modified_deleted(self):
        base = {'a': b'1', 'b': b'2', 'c': b'3'}
        cur = {'a': b'1', 'b': b'changed', 'd': b'new'}
        self.assertEqual(compare_snapshots(base, cur), {'added': ['d'], 'modified': ['b'], 'deleted': ['c']})

    def test_upload_archive_round_trips_regular_files_only(self):
        files = {'a.txt': b'A', 'pkg/m.py': b'M'}
        self.assertEqual(parse_workspace_tar(R._tar_files(files), self.limits), files)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class BoundedSubprocessTest(unittest.TestCase):
    def test_output_capped_without_buffering_everything(self):
        res = run_bounded(['/bin/sh', '-c', 'head -c 3000000 /dev/zero; echo err >&2'],
                          timeout=20, max_stdout=1000, max_stderr=100)
        self.assertEqual(len(res.stdout), 1000)
        self.assertTrue(res.stdout_truncated)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)

    def test_deadline_kills_whole_process_tree(self):
        start = time.monotonic()
        res = run_bounded(['/bin/sh', '-c', 'sleep 60 & echo $!; wait'], timeout=1, max_stdout=100)
        self.assertTrue(res.timed_out)
        self.assertLess(time.monotonic() - start, 10)
        child = int(res.stdout.split()[0])
        self.assertTrue(_wait_dead(child), 'grandchild survived deadline kill')

    def test_descendants_cannot_outlive_normal_exit(self):
        start = time.monotonic()
        res = run_bounded(['/bin/sh', '-c', 'sleep 60 & echo $!'], timeout=20, max_stdout=100)
        self.assertLess(time.monotonic() - start, 10, 'straggler held output pipe open')
        self.assertTrue(_wait_dead(int(res.stdout.split()[0])))

    def test_cancel_event_stops_command(self):
        import threading
        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        res = run_bounded(['/bin/sh', '-c', 'sleep 60'], timeout=30, max_stdout=10, cancel=ev)
        self.assertTrue(res.timed_out)

    def test_detached_descendant_with_all_pipes_closed_is_killed(self):
        # Review major 2 repro: parent exits 0, child keeps running with no pipes held.
        res = run_bounded(['/bin/sh', '-c', 'sleep 30 >/dev/null 2>&1 </dev/null & echo $!'],
                          timeout=20, max_stdout=100)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        child = int(res.stdout.split()[0])
        self.assertTrue(_wait_dead(child), 'background descendant leaked after normal exit')

    def test_exit_is_observed_without_reaping_leader(self):
        # Group kill is only safe while the leader is an unreaped zombie (pgid reserved).
        proc = subprocess.Popen(['/bin/sh', '-c', 'exit 3'], start_new_session=True)
        watcher = R._ExitWatcher(proc.pid)
        try:
            self.assertTrue(watcher.wait(5))
            self.assertIsNone(proc.returncode)
            os.kill(proc.pid, 0)  # zombie still exists, so its pid/pgid is not recyclable
        finally:
            watcher.close()
        self.assertEqual(proc.wait(timeout=5), 3)

    def test_fast_exits_are_not_misread_as_timeouts(self):
        start = time.monotonic()
        for _ in range(40):
            res = run_bounded(['/usr/bin/true'], timeout=5, max_stdout=10)
            self.assertEqual((res.returncode, res.timed_out), (0, False))
        self.assertLess(time.monotonic() - start, 20)

    def test_rejects_non_list_argv(self):
        with self.assertRaises(RunnerError):
            run_bounded('sleep 1', timeout=1, max_stdout=1)


class DockerBinaryTrustTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.chmod(self.dir, 0o700)
        self.exe = os.path.join(self.dir, 'docker')
        Path(self.exe).write_text('#!/bin/sh\nexit 0\n')
        os.chmod(self.exe, 0o755)

    def tearDown(self):
        os.chmod(self.exe, 0o755)
        os.unlink(self.exe)
        os.rmdir(self.dir)

    def test_trusted_absolute_executable_accepted(self):
        self.assertEqual(R.discover_docker_binary(self.exe), os.path.realpath(self.exe))

    def test_relative_writable_or_non_executable_rejected(self):
        with self.assertRaises(RunnerError):
            R.discover_docker_binary('docker')
        os.chmod(self.exe, 0o757)
        with self.assertRaises(RunnerError):
            R.discover_docker_binary(self.exe)
        os.chmod(self.exe, 0o644)
        with self.assertRaises(RunnerError):
            R.discover_docker_binary(self.exe)

    def test_docker_env_does_not_inherit_unrelated_host_secrets(self):
        os.environ['OPENDEVENGINE_TEST_HOST_SECRET'] = 'should-not-leak'
        try:
            runner = DockerRunner('envtest', docker_binary=self.exe)
        finally:
            del os.environ['OPENDEVENGINE_TEST_HOST_SECRET']
        self.assertNotIn('OPENDEVENGINE_TEST_HOST_SECRET', runner._env)
        self.assertLessEqual(set(runner._env) - {'PATH', 'LANG', 'LC_ALL'}, set(R._DOCKER_ENV_PASSTHROUGH))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)


CID = 'a' * 64
IMG = 'sha256:' + 'b' * 64
DAEMON_DOWN = b'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n'


def _info(running):
    return {'Id': CID, 'Image': IMG, 'State': {'Running': running},
            'Config': {'Labels': {'org.opendevengine.managed': '1', 'org.opendevengine.instance': 'inst',
                                  'org.opendevengine.session': 'sess', 'org.opendevengine.attempt': 'att1'}}}


class StopControlFailureTest(unittest.TestCase):
    """Review majors 1 and 3: stop() must never report false confirmation and
    must finish within one total 10 s budget regardless of control-plane delays."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.chmod(self.dir, 0o700)
        exe = os.path.join(self.dir, 'docker')
        Path(exe).write_text('#!/bin/sh\nexit 99\n')
        os.chmod(exe, 0o755)
        self.addCleanup(lambda: (os.unlink(exe), os.rmdir(self.dir)))
        self.runner = DockerRunner('inst', docker_binary=exe)
        self.handle = RunnerHandle('inst', 'sess', CID, IMG)
        self.clock = FakeClock()
        self.calls = []
        fake_time = type('T', (), {'monotonic': staticmethod(self.clock.monotonic),
                                   'sleep': staticmethod(self.clock.sleep)})
        patcher = unittest.mock.patch.object(R, 'time', fake_time)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake(self, responder):
        """responder(op, call_index) -> (delay_seconds, BoundedResult). The fake honors
        the timeout passed in, as run_bounded does: a slower call returns timed_out."""
        def cli(args, *, timeout=R.CONTROL_TIMEOUT, max_stdout=0, stdin=None):
            op = args[0] if args[0] != 'container' else args[1]
            delay, res = responder(op, len(self.calls))
            self.calls.append((op, timeout, self.clock.t))
            if delay > timeout:
                self.clock.t += timeout
                return R.BoundedResult(-9, b'', b'', True, False, False)
            self.clock.t += delay
            return res
        self.runner._cli = cli

    @staticmethod
    def _ok(info):
        return R.BoundedResult(0, json.dumps(info).encode(), b'', False, False, False)

    @staticmethod
    def _fail(stderr, stdout=b''):
        return R.BoundedResult(1, stdout, stderr, False, False, False)

    def _stop(self):
        start = self.clock.t
        result = self.runner.stop(self.handle)
        elapsed = self.clock.t - start
        self.assertLessEqual(elapsed, R.STOP_CONFIRM_SECONDS, f'stop took {elapsed}s')
        for op, timeout, at in self.calls:
            self.assertLessEqual(at + timeout, start + R.STOP_CONFIRM_SECONDS + 1e-9,
                                 f'{op} given a timeout beyond the stop deadline')
        return result

    def test_daemon_unreachable_is_never_confirmed(self):
        self._fake(lambda op, i: (0.01, self._fail(DAEMON_DOWN)))
        self.assertEqual(self._stop(), {'confirmed': False, 'code': 'containment_unverified'})
        with self.assertRaises(RunnerError) as cm:
            self.runner.discard(self.handle)
        self.assertEqual(cm.exception.code, 'inspect_failed')

    def test_daemon_lost_after_kill_is_never_confirmed(self):
        def responder(op, i):
            if i == 0:
                return 0.05, self._ok(_info(True))
            if op == 'kill':
                return 0.05, R.BoundedResult(0, CID.encode(), b'', False, False, False)
            return 0.05, self._fail(DAEMON_DOWN)
        self._fake(responder)
        self.assertEqual(self._stop()['confirmed'], False)

    def test_only_exact_not_found_for_this_id_counts_as_absent(self):
        self._fake(lambda op, i: (0.01, self._fail(f'Error: No such container: {CID}\n'.encode())))
        self.assertEqual(self._stop(), {'confirmed': True, 'code': 'absent'})
        for stderr in [f'Error: No such container: {"c" * 64}\n'.encode(),
                       f'Error: No such container: {CID}\nCannot connect to the Docker daemon\n'.encode(),
                       b'Error response from daemon: No such container\n',
                       b'permission denied while trying to connect to the Docker daemon socket\n']:
            with self.subTest(stderr=stderr):
                self.calls.clear()
                self._fake(lambda op, i, s=stderr: (0.01, self._fail(s)))
                self.assertEqual(self._stop()['code'], 'containment_unverified')

    def test_garbage_or_foreign_inspect_output_is_not_trusted(self):
        other = dict(_info(False), Id='d' * 64)
        for res in [R.BoundedResult(0, b'not json', b'', False, False, False), self._ok(other), self._ok([])]:
            with self.subTest(res=res.stdout[:20]):
                self.calls.clear()
                self._fake(lambda op, i, r=res: (0.01, r))
                self.assertEqual(self._stop()['code'], 'containment_unverified')

    def test_reviewer_slow_control_plane_scenario_bounded(self):
        # inspect 4 s + kill 8 s + inspect 8 s previously "confirmed stopped" after 20 s.
        def responder(op, i):
            if op == 'kill':
                return 8.0, R.BoundedResult(0, b'', b'', False, False, False)
            if i == 0:
                return 4.0, self._ok(_info(True))
            return 8.0, self._ok(_info(False))
        self._fake(responder)
        self.assertEqual(self._stop(), {'confirmed': False, 'code': 'containment_unverified'})

    def test_hung_initial_inspect_bounded(self):
        self._fake(lambda op, i: (60.0, self._ok(_info(True))))
        self.assertEqual(self._stop()['code'], 'containment_unverified')
        self.assertEqual(len(self.calls), 1)

    def test_still_running_after_kill_times_out_unverified(self):
        self._fake(lambda op, i: (0.3, self._ok(_info(True)) if op != 'kill'
                                  else R.BoundedResult(0, b'', b'', False, False, False)))
        self.assertEqual(self._stop()['code'], 'containment_unverified')

    def test_normal_stop_confirmed(self):
        def responder(op, i):
            if op == 'kill':
                return 0.2, R.BoundedResult(0, b'', b'', False, False, False)
            return 0.1, self._ok(_info(i == 0))
        self._fake(responder)
        self.assertEqual(self._stop(), {'confirmed': True, 'code': 'stopped'})
        self._fake(lambda op, i: (0.1, self._ok(_info(False))))
        self.assertEqual(self._stop(), {'confirmed': True, 'code': 'already_stopped'})

    def test_forged_handle_still_raises(self):
        forged = dict(_info(True))
        forged['Config'] = {'Labels': dict(_info(True)['Config']['Labels'], **{'org.opendevengine.session': 'other'})}
        self._fake(lambda op, i: (0.01, self._ok(forged)))
        with self.assertRaises(RunnerError) as cm:
            self.runner.stop(self.handle)
        self.assertEqual(cm.exception.code, 'ownership_unverified')
        self.assertEqual([c[0] for c in self.calls], ['inspect'])


class CreateArgumentsTest(unittest.TestCase):
    def setUp(self):
        self.limits = validate_limits({})
        self.args = DockerRunner._create_args(image_id='sha256:' + 'c' * 64, instance_id='inst',
                                              session_id='sess', attempt_id='att', limits=self.limits)

    def _value(self, flag):
        return [self.args[i + 1] for i, a in enumerate(self.args) if a == flag]

    def test_isolation_flags_present_and_no_mount_or_privilege_flags(self):
        for flag in ['-v', '--volume', '--mount', '--privileged', '--cap-add', '--device', '--volumes-from',
                     '--pid', '--ipc', '--userns', '-p', '--publish']:
            self.assertNotIn(flag, self.args)
        self.assertEqual(self._value('--network'), ['none'])
        self.assertEqual(self._value('--cap-drop'), ['ALL'])
        self.assertEqual(self._value('--security-opt'), ['no-new-privileges=true'])
        self.assertEqual(self._value('--user'), ['65534:65534'])
        self.assertEqual(self._value('--pull'), ['never'])
        self.assertIn('--read-only', self.args)
        tmpfs = self._value('--tmpfs')
        self.assertIn(f'size={64 * 1024 * 1024}', tmpfs[0])
        self.assertTrue(tmpfs[0].startswith('/workspace:') and ',exec,' in tmpfs[0])
        self.assertTrue(tmpfs[1].startswith('/tmp:') and 'noexec' in tmpfs[1] and f'size={16 * 1024 * 1024}' in tmpfs[1])
        self.assertEqual(self._value('--memory'), self._value('--memory-swap'))
        self.assertEqual(self._value('--pids-limit'), ['64'])
        self.assertEqual(self.args[-2:], ['sha256:' + 'c' * 64, '600'])
        labels = self._value('--label')
        self.assertIn('org.opendevengine.instance=inst', labels)
        self.assertIn('org.opendevengine.session=sess', labels)
        self.assertIn('org.opendevengine.attempt=att', labels)

    def _info(self, **hc_overrides):
        hc = {'Binds': None, 'Mounts': None, 'NetworkMode': 'none', 'Privileged': False,
              'ReadonlyRootfs': True, 'CapDrop': ['ALL'], 'CapAdd': None,
              'SecurityOpt': ['no-new-privileges=true'], 'Tmpfs': {'/workspace': 'x', '/tmp': 'y'},
              'PidsLimit': 64, 'Memory': 512 * 1024 * 1024, 'IpcMode': 'private', 'PidMode': ''}
        hc.update(hc_overrides)
        return {'HostConfig': hc, 'Mounts': [], 'Config': {'User': '65534:65534'}}

    def test_post_create_inspection_rejects_weakened_isolation(self):
        DockerRunner._verify_hostconfig(self._info(), self.limits)
        bad = [dict(Binds=['/Users:/host']), dict(NetworkMode='bridge'), dict(Privileged=True),
               dict(ReadonlyRootfs=False), dict(CapAdd=['SYS_ADMIN']), dict(PidMode='host'),
               dict(Tmpfs={'/workspace': 'x'}), dict(PidsLimit=0), dict(SecurityOpt=[])]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(RunnerError) as cm:
                DockerRunner._verify_hostconfig(self._info(**override), self.limits)
            self.assertEqual(cm.exception.code, 'isolation_unverified')
        info = self._info()
        info['Mounts'] = [{'Type': 'volume', 'Destination': '/data'}]
        with self.assertRaises(RunnerError):
            DockerRunner._verify_hostconfig(info, self.limits)


class InContainerScriptLogicTest(unittest.TestCase):
    """Exercise the fixed POSIX file scripts against a temp root (test harness only)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        Path(self.outside, 'secret').write_text('outside-secret')
        Path(self.root, 'src').mkdir()
        Path(self.root, 'src', 'a.txt').write_text('A')
        os.symlink(self.outside, os.path.join(self.root, 'evil_dir'))
        os.symlink(os.path.join(self.outside, 'secret'), os.path.join(self.root, 'evil_file'))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root)
        shutil.rmtree(self.outside)

    def _run(self, script, path, stdin=b''):
        script = script.replace('cur=/workspace', f'cur={self.root}')
        return subprocess.run(['/bin/sh', '-c', script, 'opendevengine', path], input=stdin,
                              capture_output=True, timeout=10)

    def test_read_rejects_symlinked_parent_and_final(self):
        self.assertEqual(self._run(R._READ_SCRIPT, 'src/a.txt').stdout, b'A')
        self.assertEqual(self._run(R._READ_SCRIPT, 'evil_dir/secret').returncode, 90)
        self.assertEqual(self._run(R._READ_SCRIPT, 'evil_file').returncode, 90)
        self.assertEqual(self._run(R._READ_SCRIPT, 'missing').returncode, 92)
        self.assertEqual(self._run(R._READ_SCRIPT, 'src').returncode, 94)

    def test_write_never_follows_symlinks_and_replaces_atomically(self):
        self.assertEqual(self._run(R._WRITE_SCRIPT, 'evil_dir/new', b'x').returncode, 90)
        self.assertEqual(self._run(R._WRITE_SCRIPT, 'evil_file', b'x').returncode, 90)
        self.assertEqual(Path(self.outside, 'secret').read_text(), 'outside-secret')
        self.assertEqual(os.listdir(self.outside), ['secret'])
        self.assertEqual(self._run(R._WRITE_SCRIPT, 'deep/new/f.txt', b'hello').returncode, 0)
        self.assertEqual(Path(self.root, 'deep/new/f.txt').read_bytes(), b'hello')
        self.assertEqual(self._run(R._WRITE_SCRIPT, 'src/a.txt/x', b'h').returncode, 91)
        self.assertFalse([n for n in os.listdir(os.path.join(self.root, 'deep/new')) if n.startswith('.opendevengine')])

    def test_delete_removes_link_itself_never_target(self):
        self.assertEqual(self._run(R._DELETE_SCRIPT, 'evil_dir/secret').returncode, 90)
        self.assertEqual(self._run(R._DELETE_SCRIPT, 'evil_file').returncode, 0)
        self.assertTrue(Path(self.outside, 'secret').exists())
        self.assertEqual(self._run(R._DELETE_SCRIPT, 'src').returncode, 94)
        self.assertEqual(self._run(R._DELETE_SCRIPT, 'gone').returncode, 92)

    def test_glob_and_option_like_paths_are_literal(self):
        Path(self.root, '-n').write_text('dash')
        self.assertEqual(self._run(R._READ_SCRIPT, '-n').stdout, b'dash')
        self.assertEqual(self._run(R._READ_SCRIPT, 'src/*').returncode, 92)


DOCKER_TESTS = os.environ.get('OPENDEVENGINE_DOCKER_TESTS') == '1'
TEST_IMAGE = os.environ.get('OPENDEVENGINE_TEST_IMAGE', 'debian:stable-slim')


@unittest.skipUnless(DOCKER_TESTS, 'set OPENDEVENGINE_DOCKER_TESTS=1 to run real Docker isolation checks')
class RealDockerIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = 'test' + secrets.token_hex(4)
        cls.runner = DockerRunner(cls.instance)
        pre = cls.runner.preflight(TEST_IMAGE)
        if pre['blocking_reasons']:
            raise unittest.SkipTest(f'preflight blocked: {pre["blocking_reasons"]}')
        cls.image_id = pre['image_id']

    @classmethod
    def tearDownClass(cls):
        for handle in cls.runner.list_owned():
            cls.runner.stop(handle)
            cls.runner.discard(handle)
        assert cls.runner.list_owned() == []

    def _provision(self, sid, files, **limits):
        handle = self.runner.provision(sid, files, image=TEST_IMAGE, limits=limits)
        self.addCleanup(self._cleanup, handle)
        return handle

    def _cleanup(self, handle):
        self.runner.stop(handle)
        self.runner.discard(handle)

    def _sh(self, handle, script, timeout=30):
        return self.runner.run_command(handle, ['sh', '-c', script], timeout_seconds=timeout)

    def test_two_sessions_isolated_unprivileged_no_network_no_host_mounts(self):
        a = self._provision('sessa', {'a.txt': b'secret-A'})
        b = self._provision('sessb', {'b.txt': b'secret-B'})
        self.assertEqual(self.runner.list_files(a), ['a.txt'])
        self.assertEqual(self.runner.list_files(b), ['b.txt'])
        self.assertEqual(self._sh(a, 'id -u; id -g')['stdout'].split(), ['65534', '65534'])
        status = self._sh(a, 'grep -E "^(CapEff|CapPrm|NoNewPrivs)" /proc/self/status')['stdout']
        self.assertIn('CapEff:\t0000000000000000', status)
        self.assertIn('NoNewPrivs:\t1', status)
        found = self._sh(a, 'find / -xdev -name b.txt 2>/dev/null; grep -rl "secret-[B]" /workspace /tmp; true')
        self.assertFalse(found['timed_out'])
        self.assertEqual(found['stdout'].strip(), '')
        # network none: no routes; only loopback is up (kernel fallback tunnel devices stay down)
        routes = self._sh(a, 'tail -n +2 /proc/net/route; grep -v " lo$" /proc/net/ipv6_route; true')
        self.assertEqual(routes['stdout'].strip(), '')
        states = self._sh(a, 'for d in /sys/class/net/*; do [ -d "$d" ] || continue; '
                             'echo "${d##*/}=$(cat $d/operstate)"; done')['stdout'].split()
        self.assertIn('lo=unknown', states)
        self.assertEqual([s for s in states if not s.startswith('lo=') and not s.endswith('=down')], [])
        mounts = self._sh(a, 'cat /proc/mounts')['stdout']
        for marker in ['/Users', '/home', 'docker.sock', '/host_mnt', 'virtiofs', 'grpcfuse', 'osxfs']:
            self.assertNotIn(marker, mounts)
        self.assertRegex(mounts, r'tmpfs /workspace tmpfs rw,nosuid,nodev')
        self.assertRegex(mounts, r'tmpfs /tmp tmpfs rw,nosuid,nodev,noexec')
        self.assertNotEqual(self._sh(a, 'test -e /var/run/docker.sock || test -e /run/docker.sock')['exit_code'], 0)
        self.assertNotEqual(self._sh(a, 'touch /etc/opendevengine-x')['exit_code'], 0)
        env = self._sh(a, 'env; tr "\\0" "\\n" < /proc/1/environ')['stdout']
        self.assertNotIn('HOME=/Users', env)
        self.assertNotIn('DOCKER_', env)

    def test_workspace_byte_cap_tmp_noexec_output_and_time_bounds(self):
        h = self._provision('sesscap', {}, workspace_mib=1, max_output_bytes=1024)
        res = self._sh(h, 'head -c 3000000 /dev/zero > big; echo rc=$?; wc -c < big')
        self.assertIn('rc=1', res['stdout'])
        self.assertLessEqual(int(res['stdout'].split()[-1]), 1024 * 1024)
        with self.assertRaises(RunnerError) as cm:
            self.runner.write_file(h, 'more.bin', b'x' * 200000)
        self.assertEqual(cm.exception.code, 'workspace_full')
        self._sh(h, 'rm -f big')
        res = self._sh(h, 'printf "#!/bin/sh\\necho ran\\n" > /tmp/x; chmod +x /tmp/x; /tmp/x')
        self.assertNotEqual(res['exit_code'], 0)
        self.assertNotIn('ran', res['stdout'])
        res = self._sh(h, 'printf "#!/bin/sh\\necho ran\\n" > w.sh; chmod +x w.sh; ./w.sh')
        self.assertEqual(res['stdout'].strip(), 'ran')
        res = self._sh(h, 'head -c 200000 /dev/zero | tr "\\0" a')
        self.assertTrue(res['truncated'])
        self.assertLessEqual(len(res['stdout']), 1024)
        start = time.monotonic()
        res = self.runner.run_command(h, ['sleep', '60'], timeout_seconds=2)
        self.assertTrue(res['timed_out'])
        self.assertLess(time.monotonic() - start, 10)
        # the in-container timeout must have killed the command, not just the docker client
        leftover = self._sh(h, 'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline; echo; done')['stdout']
        cmdlines = [line.strip() for line in leftover.splitlines()]
        self.assertNotIn('sleep 60', cmdlines)
        self.assertIn('sleep 600', cmdlines)  # PID1 wall-limit sleep is the only sleep left

    def test_symlinks_rejected_snapshot_manifest_and_stop(self):
        h = self._provision('sesssnap', {'keep.txt': b'k', 'old.txt': b'o'})
        self.runner.write_file(h, 'new/n.txt', b'n')
        self.runner.delete_file(h, 'old.txt')
        self._sh(h, 'ln -s /etc/passwd link; ln -s /etc d')
        for path in ['link', 'd/passwd']:
            with self.assertRaises(RunnerError) as cm:
                self.runner.read_file(h, path)
            self.assertEqual(cm.exception.code, 'unsafe_path')
        with self.assertRaises(RunnerError):
            self.runner.write_file(h, 'd/x', b'x')
        with self.assertRaises(RunnerError) as cm:
            self.runner.snapshot(h)
        self.assertEqual(cm.exception.code, 'snapshot_rejected')
        self.runner.delete_file(h, 'link')
        self.runner.delete_file(h, 'd')
        self._sh(h, 'ln keep.txt hard')
        with self.assertRaises(RunnerError):
            self.runner.snapshot(h)
        self.runner.delete_file(h, 'hard')
        snap = self.runner.snapshot(h)
        self.assertEqual(compare_snapshots({'keep.txt': b'k', 'old.txt': b'o'}, snap),
                         {'added': ['new/n.txt'], 'modified': [], 'deleted': ['old.txt']})
        self._sh(h, 'sleep 300 >/dev/null 2>&1 &')
        start = time.monotonic()
        result = self.runner.stop(h)
        self.assertEqual(result, {'confirmed': True, 'code': 'stopped'})
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(self.runner.stop(h)['code'], 'already_stopped')
        with self.assertRaises(RunnerError) as cm:
            self.runner.run_command(h, ['true'])
        self.assertEqual(cm.exception.code, 'not_running')
        # Real Docker not-found wording must be recognised as verified absence.
        self.runner.discard(h)
        self.assertEqual(self.runner.stop(h), {'confirmed': True, 'code': 'absent'})
        self.assertIsNone(self.runner.discard(h))

    def test_orphan_recovery_lists_only_owned_and_refuses_duplicates(self):
        h = self._provision('sessorph', {'x': b'1'})
        with self.assertRaises(RunnerError) as cm:
            self.runner.provision('sessorph', {}, image=TEST_IMAGE, limits={})
        self.assertEqual(cm.exception.code, 'session_exists')
        fresh = DockerRunner(self.instance)
        owned = [o for o in fresh.list_owned() if o.session_id == 'sessorph']
        self.assertEqual(owned, [h])
        self.assertEqual(fresh.read_file(owned[0], 'x'), b'1')
        self.assertEqual(DockerRunner('other' + secrets.token_hex(3)).list_owned(), [])
        with self.assertRaises(RunnerError) as cm:
            fresh.discard(owned[0])
        self.assertEqual(cm.exception.code, 'not_stopped')
        forged = RunnerHandle(self.instance, 'sessforged', h.container_id, h.image_id)
        with self.assertRaises(RunnerError) as cm:
            fresh.stop(forged)
        self.assertEqual(cm.exception.code, 'ownership_unverified')


if __name__ == '__main__':
    unittest.main()
