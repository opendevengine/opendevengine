"""Deterministic 250 catalog/50 task evaluation on the actual IndexService."""
import json
from pathlib import Path
import tempfile
import unittest
from opendevengine.index import Snapshot
from opendevengine.indexing import IndexService
from opendevengine.platform.db import Store
from opendevengine.platform.jobs import JobQueue
from opendevengine.platform.worker import Worker
from opendevengine.providers import ProviderService
from opendevengine.ask import RoutingService


def evaluate():
    data=json.loads((Path(__file__).parent/'fixtures/routing/catalog.json').read_text())
    import test_github_service as github_fixture
    fixture=github_fixture.GitHubTests(methodName='runTest')
    fixture.setUpClass(); fixture.setUp()
    try:
        store=fixture.platform.store; jobs=fixture.platform.jobs
        providers=ProviderService(store,None)
        mapping={r['id']:'gh:'+str(i) for i,r in enumerate(data['repositories'],1)}
        fixture.remote.repos['11']=[]
        for i,repo in enumerate(data['repositories'],1):
            record=github_fixture.repo(i,repo['name'])
            record.update(description=repo['profile'],topics=[])
            fixture.remote.repos['11'].append(record)
        fixture.refresh()
        def reader(source,revision,caps):
            i=int(source['id'].split(':')[1])-1
            return {'revision':'a'*40,'revision_kind':'git','branch':'main','files':[{'path':'README.md','content':data['repositories'][i]['profile'].encode()}]}
        index=IndexService(store,jobs,fixture.service,providers,snapshot_reader=reader)
        routing=RoutingService(store,index)
        for i in range(1,26):
            run=index.request_index('gh:'+str(i))
            Worker(jobs,{'index.run':index.run_index},worker_id='fixture').run_once()
            assert index.get_run(run['id'])['state']=='completed'
        for group in ('tasks','multi_tasks'):
            for task in data[group]:
                task['expected']=[mapping[r] for r in task['expected']]
        results={}; failures=[]
        for group in ('tasks','multi_tasks'):
            recalled=baseline=all_covered=0; labels=0
            for task in data[group]:
                raw=index.catalog_candidates(task['prompt'],limit=3)
                d=routing.route(task['prompt'])
                expected=set(task['expected']); found={c['repository_id'] for c in d['candidates'][:3]}
                old={c['repository_id'] for c in raw}
                recalled+=len(expected&found); baseline+=len(expected&old); labels+=len(expected)
                all_covered+=expected<=found
                if not expected<=found:
                    failures.append({'group':group,'prompt':task['prompt'],'expected':sorted(expected),'actual':sorted(found)})
            results[group]={'count':len(data[group]),'top3_label_recall':recalled/labels,'catalog_lexical_baseline_recall':baseline/labels,'all_expected_top3':all_covered/len(data[group])}
        expanded=json.loads((Path(__file__).parent/'fixtures/routing/expanded-r1.json').read_text())
        path_recall=path_resolved=0; path_failures=[]
        for task in expanded['path_tasks']:
            expected={mapping[r] for r in task['expected']}
            pins=[mapping[r] for r in task.get('pins',[])]
            decision=routing.route(task['prompt'],pinned_repository_ids=pins)
            found={c['repository_id'] for c in decision['candidates'][:3]}
            path_recall+=expected<=found
            correct=set(decision['selected_repository_ids'])==expected and decision['status']=='resolved'
            path_resolved+=correct
            if not correct or not expected<=found:
                path_failures.append({'prompt':task['prompt'],'expected':sorted(expected),'selected':decision['selected_repository_ids'],'status':decision['status']})
        results['path_tasks']={'count':len(expanded['path_tasks']),'all_expected_top3':path_recall/len(expanded['path_tasks']),'correct_scope':path_resolved/len(expanded['path_tasks']),'failures':path_failures}
        results['path_ambiguous']=[routing.route(p)['status'] for p in expanded['path_ambiguous']]
        results['path_absent']=[routing.route(p)['status'] for p in expanded['path_absent']]
        results['ambiguous']=[routing.route(p)['status'] for p in data['ambiguous']]
        results['absent']=[routing.route(p)['status'] for p in data['absent']]
        # Exact short-name collision and qualified selection with two current owners.
        collision_ids=[]
        for i,name in enumerate(('alpha/shared','beta/shared')):
            path=Path(fixture.temp.name)/str(i); path.mkdir()
            collision_ids.append(index.register_local(name,path)['repository_id'])
        results['collision']=routing.route('Update shared')['status']
        results['qualified_correct']=routing.route('Update alpha/shared')['selected_repository_ids']==[collision_ids[0]]
        decision=routing.route('billing',pinned_repository_ids=[mapping['demo:billing']])
        fixture.service.update_selection(mapping['demo:billing'],index_enabled=False,implementation_enabled=False)
        results['revoked_visible']=len(routing.get(decision['id'])['candidates'])
        results['missing_pin']=routing.route('billing',pinned_repository_ids=['demo:missing'])['status']
        results['failures']=failures
        index.vectors.close()
        return results
    finally:
        fixture.doCleanups()


class RoutingEvaluation(unittest.TestCase):
    def test_labeled_catalog(self):
        result=evaluate()
        self.assertGreaterEqual(result['tasks']['top3_label_recall'],.9,result)
        self.assertTrue(all(s=='needs_selection' for s in result['ambiguous']),result)
        self.assertTrue(all(s=='no_match' for s in result['absent']),result)
        self.assertEqual(result['path_tasks']['all_expected_top3'],1.0,result)
        self.assertEqual(result['path_tasks']['correct_scope'],1.0,result)
        self.assertTrue(all(s!='resolved' for s in result['path_ambiguous']),result)
        self.assertTrue(all(s=='no_match' for s in result['path_absent']),result)
        self.assertEqual(result['collision'],'needs_selection')
        self.assertTrue(result['qualified_correct'])
        self.assertEqual(result['revoked_visible'],0)
        self.assertEqual(result['missing_pin'],'needs_selection')

if __name__=='__main__':
    print(json.dumps(evaluate(),indent=2))
