"""Publication service behavior with real SQLite/jobs and a fake GitHub boundary."""

import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest import TestCase

from opendevengine.github.errors import GitHubError
from opendevengine.platform.worker import Worker
from opendevengine.publication import PublicationError, PublicationService
import test_publication_schema

BASE = 'a' * 40
CURRENT = 'b' * 40
TREE = 'c' * 40
COMMIT = 'd' * 40
BLOB = 'e' * 40


class Sessions:
    def __init__(self, repository_id):
        self.value = {
            'session_id':'ses', 'plan_id':'plan', 'plan_version_id':'pv',
            'repository_id':repository_id, 'base_sha':BASE, 'base_branch':'main',
            'session_branch':'opendevengine/ses', 'diff_hash':'f' * 40,
            'checkpoint_artifact_id':'checkpoint', 'patch_artifact_id':'patch',
            'checkpoint':{'base_sha':BASE,
                          'changed':{'calc.py':base64.b64encode(b'return a + b\n').decode()},
                          'deleted':['old.sh'], 'modes':{'calc.py':'100755','old.sh':'100644'}, 'rejected':[]},
            'patch':{'base_sha':BASE,'files':[{'path':'calc.py'}],'rejected':[],
                     'stats':{'files':2,'additions':1,'deletions':1}},
            'policy':'manual_draft',
            'outcome':{'result':'reviewable','publication_eligible':True,
                       'containment':{'confirmed':True},'artifacts_stale':False},
            'validations':{'fully_validated':True,'summary':{'passed':1}},
            'source_available':True,
        }

    def publication_snapshot(self, ident):
        if ident != 'ses':
            raise PublicationError('not_found')
        return copy.deepcopy(self.value)


class Client:
    def __init__(self):
        self.calls=[];self.base=BASE;self.ref=None;self.ref_branch=None;self.pr=None
        self.unknown_ref=False;self.unknown_pr=False;self.collision_once=False
        self.on_blob=None
        self.compare_files=[];self.compare_complete=True

    def default_branch_sha(self, token, owner, name, branch):
        self.calls.append(('base',token,branch));return self.base
    def compare(self, token, owner, name, base, head):
        self.calls.append(('compare',base,head));return {'files':self.compare_files,'complete':self.compare_complete}
    def commit(self, token, owner, name, sha):
        self.calls.append(('commit',sha));return {'sha':sha,'tree_sha':TREE}
    def create_blob(self, token, owner, name, content):
        self.calls.append(('blob',content))
        if self.on_blob:self.on_blob()
        return BLOB
    def create_tree(self, token, owner, name, base_tree, entries):
        self.calls.append(('tree',base_tree,entries));return '1' * 40
    def create_commit(self, token, owner, name, message, tree, parent, identity):
        self.calls.append(('create_commit',tree,parent,identity));return COMMIT
    def create_ref(self, token, owner, name, branch, sha):
        self.calls.append(('create_ref',branch,sha));self.ref_branch=branch
        if self.collision_once:
            self.collision_once=False;self.ref='9'*40;return False
        self.ref=sha
        if self.unknown_ref:
            self.unknown_ref=False;raise GitHubError('timeout')
        return True
    def get_ref(self, token, owner, name, branch):
        self.calls.append(('get_ref',branch))
        if self.ref is None or branch != self.ref_branch:raise GitHubError('not_found')
        return self.ref
    def create_pull_request(self, token, owner, name, title, body, head, base):
        self.calls.append(('create_pr',head,base,True))
        self.pr_body=body
        self.pr=self._pr(head,base)
        if self.unknown_pr:
            self.unknown_pr=False;raise GitHubError('timeout')
        return self.pr
    def pull_requests(self, token, owner, name, head, base):
        self.calls.append(('find_pr',head,base));return [self.pr] if self.pr and head == owner + ':' + self.pr['head']['ref'] and base == self.pr['base']['ref'] else []
    def pull_request(self, token, owner, name, number):
        self.calls.append(('get_pr',number))
        if not self.pr or number != self.pr['number']:raise GitHubError('not_found')
        return self.pr
    @staticmethod
    def _pr(head,base):
        return {'id':99,'number':7,'draft':True,'state':'open','merged':False,
                'html_url':'https://github.com/acme/repo/pull/7',
                'head':{'sha':COMMIT,'ref':head,'repo':{'full_name':'acme/repo'}},
                'base':{'ref':base,'repo':{'full_name':'acme/repo'}}}


class GitHub:
    def __init__(self):
        self.client=Client();self.tokens=[];self.access=[];self.hooks={};self.denied=False
    def register_purge_hook(self,name,fn):self.hooks[name]=fn
    def require_access(self,repo,cap):
        self.access.append((repo,cap))
        if self.denied:raise GitHubError('capability_missing')
        return {}
    def get_repository(self,repo):return {'id':repo,'owner':'acme','name':'repo','full_name':'acme/repo','default_branch':'main'}
    def mint_installation_token(self,repo,permissions=None):
        self.tokens.append((repo,permissions));return {'token':'transient'}


class PublicationTests(TestCase):
    def setUp(self):
        self.fixture=test_publication_schema.PublicationSchemaTests(methodName='runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.platform=SimpleNamespace(store=self.fixture.f.store,jobs=self.fixture.f.jobs);self.repo=self.fixture.f.repo
        self.sessions=Sessions(self.repo);self.github=GitHub()
        self.svc=PublicationService(self.platform,self.github,self.sessions,clock=lambda:1763164800000)
        self.svc.update_settings('OpenDevEngine','operator@example.test')

    def publish(self,key='one'):
        return self.svc.publish('ses','Fix add','Body','f'*40,1,key)

    def run_job(self):
        self.assertTrue(Worker(self.platform.jobs,{'publication.run':self.svc.run},worker_id='pub').run_once())

    def pinned_content(self, content):
        plan = self.fixture.plan
        self.sessions.value.update(plan_id=plan['id'], plan_version_id=plan['version']['id'])
        with self.platform.store.transaction() as db:
            db.execute('UPDATE plan_versions SET content_json=? WHERE id=?',
                       (json.dumps(content), plan['version']['id']))

    def test_defaults_use_pinned_goal_steps_and_redacted_results(self):
        token = 'ghp_' + 'a' * 30
        self.pinned_content({'goal': 'Fix login ' + token + '\nfor users.',
                             'steps': [{'title': 'Update login'}, {'title': 'Verify behavior'}]})
        self.sessions.value['validations'] = {'fully_validated': True, 'summary': {'passed': 1},
            'items': [{'command': {'argv': ['python', '-m', 'unittest']}, 'status': 'passed',
                       'output': 'SOURCE MUST NEVER APPEAR'}]}
        preview = self.svc.preview('ses')
        self.assertEqual(preview['title'], 'Fix login [redacted credential] for users')
        self.assertIn('- Update login', preview['body'])
        self.assertIn('python -m unittest — passed', preview['body'])
        self.assertNotIn(token, str(preview))
        self.assertNotIn('SOURCE MUST NEVER APPEAR', preview['body'])
        self.assertNotIn('Limitations:', preview['body'])

    def test_defaults_bound_long_goal_and_disclose_failed_and_not_run_checks(self):
        self.pinned_content({'goal': 'Improve ' * 3000, 'steps': []})
        for status in ('failed', 'not_run', 'timeout', 'unavailable'):
            with self.subTest(status=status):
                self.sessions.value['validations'] = {'fully_validated': False,
                    'items': [{'command': {'argv': ['check', '/home/pilot/private', 'token=synthetic']}, 'status': status}]}
                preview = self.svc.preview('ses')
                self.assertLessEqual(len(preview['title']), 256)
                self.assertLessEqual(len(preview['body']), 12000)
                self.assertIn('Limitations:', preview['body'])
                self.assertIn('— ' + ('failed' if status == 'failed' else 'not run'), preview['body'])
                self.assertNotIn('/home/pilot', preview['body'])
                self.assertNotIn('token=synthetic', preview['body'])

    def test_defaults_ignore_newer_plan_version(self):
        plan = self.fixture.plan
        self.sessions.value.update(plan_id=plan['id'], plan_version_id=plan['version']['id'])
        newer = copy.deepcopy(self.fixture.f.content)
        newer['goal'] = 'Unrelated newer goal'
        self.fixture.f.plans.save_version(plan['id'], newer, plan['version']['id'])
        preview = self.svc.preview('ses')
        self.assertEqual(preview['title'], self.fixture.f.content['goal'])
        self.assertNotIn('Unrelated newer goal', preview['body'])

    def test_body_bounds_keep_limitations_and_counts(self):
        self.pinned_content({'goal': 'g' * 5000, 'steps': [{'title': 's' * 1000}] * 30})
        snap = self.sessions.value
        snap['validations'] = {'fully_validated': False,
            'items': [{'command': {'argv': ['c' * 1000]}, 'status': 'not_run'}] * 30}
        snap['omitted'] = ['build/' + 'p' * 1000] * 30
        title, body = self.svc._defaults(snap)
        self.assertLessEqual(len(body), 12000)
        self.assertIn('Limitations:', body)
        self.assertIn('10 more recorded checks omitted', body)
        self.assertIn('10 more paths omitted', body)
        self.assertIn('10 more steps omitted', body)

    def test_defaults_fall_back_without_goal(self):
        self.pinned_content({'steps': []})
        self.assertEqual(self.svc.preview('ses')['title'], 'OpenDevEngine session ses')

    def test_preview_is_read_only_and_publish_is_atomic_idempotent(self):
        preview=self.svc.preview('ses')
        self.assertTrue(preview['publishable']);self.assertEqual(self.github.tokens,[(self.repo,{'contents':'read','pull_requests':'read'})])
        first=self.publish();again=self.publish()
        self.assertEqual(first['id'],again['id'])
        with self.assertRaises(PublicationError):self.svc.publish('ses','Other','Body','f'*40,1,'one')
        with self.platform.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publications').fetchone()[0],1)
            job=db.execute("SELECT effect,max_attempts FROM jobs WHERE kind='publication.run'").fetchone()
        self.assertEqual(tuple(job),('external',1))

    def test_success_uses_base_tree_modes_deletion_exact_token_and_draft(self):
        pub=self.publish();self.run_job();done=self.svc.get(pub['id'])
        self.assertEqual(done['state'],'published')
        self.assertEqual(self.github.tokens,[(self.repo,{'contents':'write','pull_requests':'write'})])
        tree=next(c for c in self.github.client.calls if c[0]=='tree')
        self.assertEqual(tree[1],TREE)
        self.assertEqual(tree[2],[{'path':'calc.py','mode':'100755','type':'blob','sha':BLOB},
                                  {'path':'old.sh','mode':'100644','type':'blob','sha':None}])
        self.assertIn(('create_pr',done['head_branch'],'main',True),self.github.client.calls)
        self.assertEqual(done['pull_request_url'],'https://github.com/acme/repo/pull/7')

    def test_only_added_generated_rejections_are_allowed(self):
        entry = {'path': '__pycache__/x.pyc', 'reason': 'excluded_generated', 'status': 'added'}
        snap = self.sessions.value
        snap['omitted'] = [entry['path']]
        snap['checkpoint']['rejected'] = [entry]
        snap['patch']['rejected'] = [entry]
        preview = self.svc.preview('ses')
        self.assertEqual(preview['omitted_paths'], snap['omitted'])
        self.assertIn(entry['path'], preview['body'])
        for reason, status in [('excluded_generated', 'modified'), ('excluded_generated', 'deleted'),
                               ('excluded_sensitive', 'added'), ('excluded_configured', 'added'),
                               ('git_metadata', 'added')]:
            with self.subTest(reason=reason, status=status):
                entry.update(reason=reason, status=status)
                with self.assertRaises(PublicationError) as caught:
                    self.svc.preview('ses')
                self.assertEqual(caught.exception.code, 'rejected_paths')

    def test_empty_diff_never_mints_token_or_writes(self):
        self.sessions.value['checkpoint'].update(changed={},deleted=[])
        self.sessions.value['patch']['stats']['files']=0
        pub=self.publish();self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'no_changes')
        # Publish checks capabilities, but run recognizes empty source before token mint.
        self.assertEqual(self.github.tokens,[]);self.assertEqual(self.github.client.calls,[])

    def test_automatic_policy_requires_success_gate(self):
        self.sessions.value['policy']='on_success_draft'
        self.sessions.value['outcome']['publication_eligible']=False
        self.assertIn('automatic_policy_not_satisfied',self.svc.preview('ses')['blocking_reasons'])
        with self.assertRaises(PublicationError) as caught:self.publish()
        self.assertEqual(caught.exception.code,'automatic_policy_not_satisfied')
        # Manual publication truthfully keeps failed checks in the frozen body/preview.
        self.sessions.value['policy']='manual_draft';self.sessions.value['validations']={'fully_validated':False,'summary':{'failed':1}}
        self.assertTrue(self.svc.preview('ses')['publishable'])

    def test_base_advance_overlap_blocks(self):
        self.github.client.base=CURRENT;self.github.client.compare_files=[{'filename':'calc.py'}]
        pub=self.publish();self.run_job();self.assertEqual(self.svc.get(pub['id'])['error_code'],'base_conflict')

    def test_base_advance_incomplete_300_file_comparison_blocks(self):
        self.github.client.base=CURRENT;self.github.client.compare_complete=False
        self.github.client.compare_files=[{'filename':f'other-{n}.py'} for n in range(300)]
        pub=self.publish();self.run_job();self.assertEqual(self.svc.get(pub['id'])['error_code'],'divergence_unknown')

    def test_disjoint_base_advance_keeps_frozen_parent_without_rebase(self):
        self.github.client.base=CURRENT;self.github.client.compare_files=[{'filename':'unrelated.py'}]
        pub=self.publish();self.run_job();self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        commit=next(c for c in self.github.client.calls if c[0]=='create_commit')
        self.assertEqual(commit[2],BASE)

    def test_branch_collision_chooses_fresh_name_without_update_or_force(self):
        self.github.client.collision_once=True
        pub=self.publish();self.run_job();done=self.svc.get(pub['id'])
        self.assertEqual(done['state'],'published');self.assertTrue(done['head_branch'].endswith('-1'))
        refs=[c for c in self.github.client.calls if c[0]=='create_ref']
        self.assertEqual(len(refs),2);self.assertNotEqual(refs[0][1],refs[1][1])

    def test_permission_revocation_after_intent_blocks_before_token(self):
        pub=self.publish();self.github.denied=True;self.run_job();done=self.svc.get(pub['id'])
        self.assertEqual((done['state'],done['error_code']),('blocked','publication_permission_denied'))
        self.assertEqual(self.github.tokens,[])

    def test_permission_revocation_between_external_steps_stops_before_tree(self):
        pub=self.publish();self.github.client.on_blob=lambda:setattr(self.github,'denied',True);self.run_job()
        done=self.svc.get(pub['id']);self.assertEqual(done['error_code'],'publication_permission_denied')
        self.assertFalse(any(c[0]=='tree' for c in self.github.client.calls))

    def test_lost_branch_response_reconciles_without_second_branch(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'reconciling')
        self.assertEqual(self.platform.jobs.get(self.svc.get(pub['id']) and self._job(pub['id'])).state,'needs_reconciliation')
        resolved=self.svc.reconcile(pub['id']);self.assertEqual(resolved['state'],'creating_pr')
        self.svc.retry(pub['id'],'retry-one');self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(sum(c[0]=='create_ref' for c in self.github.client.calls),1)

    def test_retry_double_click_is_idempotent_and_different_key_conflicts(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job();self.svc.reconcile(pub['id'])
        first=self.svc.retry(pub['id'],'same');again=self.svc.retry(pub['id'],'same')
        self.assertEqual(first['id'],again['id'])
        with self.assertRaises(PublicationError) as caught:self.svc.retry(pub['id'],'different')
        self.assertEqual(caught.exception.code,'idempotency_conflict')

    def test_lost_pr_response_is_adopted_without_second_pr(self):
        pub=self.publish();self.github.client.unknown_pr=True;self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'reconciling')
        self.assertEqual(self.svc.reconcile(pub['id'])['state'],'published')
        self.assertEqual(sum(c[0]=='create_pr' for c in self.github.client.calls),1)

    def _job(self,pub):
        with self.platform.store.read() as db:return db.execute('SELECT job_id FROM publications WHERE id=?',(pub,)).fetchone()[0]

    def test_purge_blocks_active_and_tombstones_artifacts(self):
        pub=self.publish();self.svc.purge_repository(self.repo);saved=self.svc.get(pub['id'])
        self.assertEqual((saved['state'],saved['error_code']),('blocked','repository_purged'))
        with self.platform.store.read() as db:
            row=db.execute('SELECT checkpoint_artifact_id,patch_artifact_id FROM publications WHERE id=?',(pub['id'],)).fetchone()
        self.assertEqual(tuple(row),(None,None))

    def test_purge_and_exclusion_mid_run_fence_writes(self):
        for purge in (True, False):
            with self.subTest(purge=purge):
                self.setUp()
                pub=self.publish()
                def change():
                    if purge:self.svc.purge_repository(self.repo)
                    else:self.sessions.value['source_available']=False
                self.github.client.on_blob=change
                self.run_job()
                self.assertEqual(self.svc.get(pub['id'])['state'],'blocked')
                self.assertFalse(any(c[0] in ('tree','create_commit','create_ref','create_pr') for c in self.github.client.calls))
                if purge:
                    with self.platform.store.read() as db:
                        row=db.execute('SELECT title,body,commit_message FROM publications').fetchone()
                    self.assertEqual(tuple(row),('[purged]',)*3)

    def test_resume_lost_pr_and_confirmation_timeout_are_recoverable(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        # Branch was not created after all: resume must use the shared PR path.
        self.github.client.ref=None
        self.svc.recover();self.svc.retry(pub['id'],'resume')
        self.github.client.unknown_pr=True;self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'reconciling')
        self.svc.recover()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],0)

    def test_confirmation_get_timeout_keeps_number(self):
        pub=self.publish();original=self.github.client.pull_request
        def unavailable(*args):raise GitHubError('timeout')
        self.github.client.pull_request=unavailable;self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['pull_request_number'],7)
        self.assertEqual(self.svc.get(pub['id'])['state'],'reconciling')
        self.github.client.pull_request=original;self.svc.recover()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(sum(c[0]=='create_pr' for c in self.github.client.calls),1)

    def test_missing_create_number_reconciles_exact_pr(self):
        pub=self.publish();original=self.github.client.create_pull_request
        def missing(*args):original(*args);return {}
        self.github.client.create_pull_request=missing;self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'reconciling')
        self.svc.recover();self.assertEqual(self.svc.get(pub['id'])['state'],'published')

    def test_expired_lease_recovery_fences_old_worker_and_retries(self):
        pub=self.publish();lease=self.platform.jobs.claim('dead',kinds=['publication.run'])
        with self.platform.store.transaction() as db:
            db.execute("UPDATE publications SET state='creating_branch',commit_sha=? WHERE id=?",(COMMIT,pub['id']))
            db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(lease.job.id,))
        self.github.client.ref=COMMIT;self.github.client.ref_branch=pub['head_branch']
        self.assertEqual(self.svc.recover(),{'resolved':1,'pending':0})
        self.svc.retry(pub['id'],'restart')
        self.svc.run(lease.job,None)
        self.assertFalse(any(c[0]=='create_pr' for c in self.github.client.calls))
        self.run_job();self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],0)
        self.assertEqual(sum(c[0]=='create_pr' for c in self.github.client.calls),1)

    def test_transient_objects_resume_frozen_identity_message_and_date(self):
        pub=self.publish();original=self.github.client.create_commit;seen=[]
        def timeout(*args):seen.append(args[3:]);raise GitHubError('upstream_unavailable')
        self.github.client.create_commit=timeout;self.run_job()
        self.svc.update_settings('Changed','changed@example.test')
        self.svc.recover();self.svc.retry(pub['id'],'objects')
        def capture(*args):seen.append(args[3:]);return original(*args)
        self.github.client.create_commit=capture;self.run_job()
        self.assertEqual(seen[0],seen[1]);self.assertEqual(self.svc.get(pub['id'])['state'],'published')

    def test_all_snapshot_bindings_and_automatic_gate_rechecked(self):
        for field,value in [('diff_hash','0'*40),('repository_id','other'),('checkpoint_artifact_id','other'),
                            ('patch_artifact_id','other'),('base_branch','other'),('policy','on_success_draft')]:
            with self.subTest(field=field):
                self.setUp();pub=self.publish();self.sessions.value[field]=value;self.run_job()
                self.assertEqual(self.svc.get(pub['id'])['error_code'],'stale_diff')
                self.assertEqual(self.github.tokens,[])
        self.setUp();self.sessions.value['policy']='on_success_draft';pub=self.publish()
        self.github.client.on_blob=lambda:self.sessions.value['validations'].update(fully_validated=False)
        self.run_job();self.assertEqual(self.svc.get(pub['id'])['error_code'],'automatic_policy_not_satisfied')

    def test_preview_observes_rename_conflict_and_real_head_pattern(self):
        self.github.client.base=CURRENT
        self.github.client.compare_files=[{'filename':'new.py','previous_filename':'calc.py'}]
        preview=self.svc.preview('ses')
        self.assertEqual(preview['base_observation'],{'sha':CURRENT,'diverged':True})
        self.assertIn('base_conflict',preview['blocking_reasons'])
        self.assertIn('<suffix>',preview['head_branch'])
        pub=self.publish();self.run_job();self.assertEqual(self.svc.get(pub['id'])['error_code'],'base_conflict')

    def test_manual_failed_checks_body_and_automatic_success(self):
        self.sessions.value['validations']={'fully_validated':False,'summary':{'failed':1,'unavailable':1}}
        preview=self.svc.preview('ses')
        pub=self.svc.publish('ses',preview['title'],preview['body'],'f'*40,1,'manual');self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertIn('"failed":1',self.github.client.pr_body)
        self.assertIn('"unavailable":1',self.github.client.pr_body)
        with self.platform.store.read() as db:row=db.execute('SELECT body FROM publications').fetchone()
        self.assertIn('"failed":1',row[0]);self.assertIn('"unavailable":1',row[0])
        self.setUp();self.sessions.value['policy']='on_success_draft';pub=self.publish();self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(self.svc.get(pub['id'])['checks'],{'fully_validated':True})
        self.assertTrue(all(c[1].startswith('opendevengine/') and c[1]!='main' for c in self.github.client.calls if c[0]=='create_ref'))

    def test_negative_pr_confirmation_and_shapes(self):
        mutations=[lambda p:p.update(draft=False),lambda p:p['head'].update(sha=BASE),
                   lambda p:p['head'].update(ref='other'),lambda p:p['head']['repo'].update(full_name='other/repo'),
                   lambda p:p['base'].update(ref='other'),lambda p:p.update(html_url='https://evil.test'),
                   lambda p:p.pop('id'),lambda p:p.update(head=[]),lambda p:p.update(base={'repo':[]})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp();pub=self.publish();original=self.github.client.pull_request
                def altered(*args):
                    value=copy.deepcopy(original(*args));mutate(value);return value
                self.github.client.pull_request=altered;self.run_job()
                self.assertEqual(self.svc.get(pub['id'])['state'],'blocked')

    def test_path_hygiene_stale_preview_rejected_paths_and_identity(self):
        for path in ['.git/config','a/.GIT/config','a//b','a/','calc.py/x','old.sh']:
            with self.subTest(path=path):
                self.setUp();self.sessions.value['checkpoint']['changed'][path]='YQ=='
                with self.assertRaises(PublicationError) as caught:self.publish()
                self.assertEqual(caught.exception.code,'session_snapshot_invalid')
        self.setUp()
        with self.assertRaises(PublicationError) as caught:self.svc.publish('ses','T','B','0'*40,1,'stale')
        self.assertEqual(caught.exception.code,'stale_diff')
        self.sessions.value['checkpoint']['rejected']=['secret']
        with self.assertRaises(PublicationError) as caught:self.publish()
        self.assertEqual(caught.exception.code,'rejected_paths')
        self.setUp()
        with self.platform.store.transaction() as db:db.execute('DELETE FROM publication_settings')
        with self.assertRaises(PublicationError) as caught:self.publish()
        self.assertEqual(caught.exception.code,'publication_identity_required')

    def test_refresh_without_source_uses_read_scope(self):
        pub=self.publish();self.run_job();self.sessions.value['source_available']=False
        self.github.client.pr.update(state='closed',merged=True)
        self.assertEqual(self.svc.refresh(pub['id'])['pull_request_state'],'merged')
        self.assertEqual(self.github.tokens[-1][1],{'contents':'read','pull_requests':'read'})

    def test_recovery_foreign_branch_selects_fresh_suffix(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        self.github.client.ref=BASE;old=self.svc.get(pub['id'])['head_branch']
        self.svc.recover();self.assertNotEqual(self.svc.get(pub['id'])['head_branch'],old)
        self.svc.retry(pub['id'],'collision');self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')

    def test_cancellation_between_blob_and_tree(self):
        pub=self.publish()
        self.github.client.on_blob=lambda:self.platform.jobs.cancel(self._job(pub['id']))
        self.run_job()
        self.assertFalse(any(c[0] in ('tree','create_ref','create_pr') for c in self.github.client.calls))
        self.assertEqual(self.svc.get(pub['id'])['error_code'],'publication_stopped')

    def test_confirmed_commit_survives_purge_during_response(self):
        pub=self.publish();original=self.github.client.create_commit
        def purged(*args):
            value=original(*args);self.svc.purge_repository(self.repo);return value
        self.github.client.create_commit=purged;self.run_job()
        done=self.svc.get(pub['id'])
        self.assertEqual(done['state'],'blocked');self.assertEqual(done['commit_sha'],COMMIT)
        self.assertFalse(any(c[0] in ('create_ref','create_pr') for c in self.github.client.calls))

    def test_malformed_find_response_blocks_instead_of_creating_pr(self):
        pub=self.publish();self.github.client.pull_requests=lambda *args:[{'head':[], 'base':{}}]
        self.run_job();self.assertEqual(self.svc.get(pub['id'])['error_code'],'pull_request_invalid')
        self.assertFalse(any(c[0]=='create_pr' for c in self.github.client.calls))

    def test_retry_pending_read_never_orphans_old_job(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        original=self.github.client.get_ref;reads=[]
        def flaky(*args):
            reads.append(1)
            if len(reads)==1:raise GitHubError('timeout')
            return original(*args)
        self.github.client.get_ref=flaky
        with self.assertRaises(PublicationError) as caught:self.svc.retry(pub['id'],'retry')
        self.assertEqual(caught.exception.code,'reconciliation_pending')
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],1)
        self.svc.retry(pub['id'],'retry');self.run_job()
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],0)
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(sum(c[0]=='create_ref' for c in self.github.client.calls),1)
        self.assertEqual(sum(c[0]=='create_pr' for c in self.github.client.calls),1)

    def test_overlapping_recovery_retry_and_leased_worker(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        entered=Event();release=Event();original=self.github.client.get_ref
        def paused(*args):
            entered.set()
            if not release.wait(5):raise AssertionError('recovery not released')
            return original(*args)
        self.github.client.get_ref=paused
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery=pool.submit(self.svc.recover)
            self.assertTrue(entered.wait(5))
            retry=pool.submit(self.svc.retry,pub['id'],'same')
            release.set()
            self.assertEqual(recovery.result(timeout=5),{'resolved':1,'pending':0})
            retry.result(timeout=5)
        lease=self.platform.jobs.claim('replacement',kinds=['publication.run'])
        self.assertEqual(self.svc.recover(),{'resolved':0,'pending':0})
        self.assertEqual(self.svc.retry(pub['id'],'same')['state'],'creating_pr')
        self.svc.run(lease.job,None)
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],0)

    def test_public_reads_persist_checks_without_snapshot_and_legacy_unavailable(self):
        pub=self.publish()
        def forbidden(*args):raise AssertionError('public read loaded source snapshot')
        self.sessions.publication_snapshot=forbidden
        self.assertEqual(self.svc.get(pub['id'])['checks'],{'fully_validated':True})
        self.svc._state(pub['id'],'preparing')
        with self.platform.store.transaction() as db:
            db.execute("DELETE FROM publication_events WHERE type='publication.checks'")
        self.assertEqual(self.svc.get(pub['id'])['checks'],{'status':'unavailable'})

    def test_snapshot_loads_are_constant_for_blob_step_outside_write_lock(self):
        for i in range(20):
            self.sessions.value['checkpoint']['changed']['file%d'%i]='YQ=='
            self.sessions.value['checkpoint']['modes']['file%d'%i]='100644'
        original=self.sessions.publication_snapshot;calls=[]
        def counted(*args):
            calls.append(1)
            # Snapshot IO must allow a write transaction, not nest inside one.
            with self.platform.store.transaction() as db:db.execute('SELECT 1')
            return original(*args)
        self.sessions.publication_snapshot=counted
        pub=self.publish();calls.clear();self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['state'],'published')
        self.assertLess(len(calls),15)
        self.assertEqual(sum(c[0]=='blob' for c in self.github.client.calls),21)

    def test_refresh_malformed_or_wrong_number_returns_safe_state(self):
        pub=self.publish();self.run_job()
        for value in (None,[],{'number':8,'state':'closed'},{'number':True,'state':'closed'}):
            self.github.client.pull_request=lambda *args:value
            self.assertEqual(self.svc.refresh(pub['id'])['pull_request_state'],'open')

    def test_no_commit_reconciliation_needs_no_remote_access(self):
        pub=self.publish()
        self.svc._state(pub['id'],'reconciling')
        def forbidden(*args):raise AssertionError('unexpected remote read')
        self.github.mint_installation_token=forbidden
        self.assertEqual(self.svc.reconcile(pub['id'])['state'],'queued')

    def test_purge_during_recovery_does_not_abort_startup(self):
        pub=self.publish();self.github.client.unknown_ref=True;self.run_job()
        def purged(*args):
            self.svc.purge_repository(self.repo)
            raise PublicationError('repository_purged')
        self.github.client.get_ref=purged
        self.assertEqual(self.svc.recover(),{'resolved':1,'pending':0})
        self.assertEqual(self.svc.get(pub['id'])['error_code'],'repository_purged')
        self.assertEqual(self.platform.jobs.counts()['needs_reconciliation'],0)

    def test_blob_step_rechecks_lightweight_source_policy(self):
        self.sessions.value['checkpoint']['changed']['second.py']='YQ=='
        self.sessions.value['checkpoint']['modes']['second.py']='100644'
        self.sessions.source_policy=lambda ident:{'available':self.sessions.value['source_available']}
        pub=self.publish()
        self.github.client.on_blob=lambda:self.sessions.value.update(source_available=False)
        self.run_job()
        self.assertEqual(self.svc.get(pub['id'])['error_code'],'source_unavailable')
        self.assertEqual(sum(c[0]=='blob' for c in self.github.client.calls),1)
        self.assertFalse(any(c[0] in ('tree','create_ref','create_pr') for c in self.github.client.calls))
