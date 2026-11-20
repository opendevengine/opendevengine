"""Socket-free UI integration bridge: real candidate services, no model or downloads."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from opendevengine.platform import Platform, Worker
from opendevengine.providers import ProviderService
from opendevengine.indexing import IndexService
from opendevengine.ask import ConversationService, PlanService, RoutingService
from opendevengine.ask_api import AskAPI
from opendevengine.context_api import ContextAPI
from opendevengine.api import error_status
from opendevengine.platform.errors import PlatformError

root = Path(sys.argv[1])
p = Platform.open(root)
index = None
try:
    providers = ProviderService(p.store, p.secrets)
    def reader(source, revision, caps):
        return {'revision': revision or 'a'*40, 'revision_kind': 'git', 'branch': 'main', 'files': [{'path': 'worker.py', 'content': b'def deliver():\n    return True\n'}]}
    index = IndexService(p.store,p.jobs,None,providers,data_dir=root/'context',snapshot_reader=reader)
    routing = RoutingService(p.store,index)
    plans = PlanService(p.store,index,providers)
    conversations = ConversationService(p.store,p.jobs,index,providers,routing,plans)
    api = AskAPI(conversations,plans,routing)
    request = json.load(sys.stdin)
    if request['path'] == '/test/seed':
        repo = index.register_local('synthetic/mail-worker',root)['repository_id']
        index.request_index(repo)
        Worker(p.jobs,{'index.run':index.run_index},worker_id='test-index').run_once()
        snap = index.get_state(repo)['active_snapshot']
        c = conversations.create('Durable UI test',[repo])
        content = {'goal':'Bound delivery retries','user_request':'Add retries','repositories':[{'repository_id':repo,'role':'write','snapshot_id':snap['id'],'revision':snap['revision'],'branch':'main'}], 'citations':[{'id':'e1','repository_id':repo,'snapshot_id':snap['id'],'revision':snap['revision'],'path':'worker.py','start_line':1,'end_line':2}], 'findings':[{'text':'Delivery returns True','citation_ids':['e1']}], 'requirements':['Preserve mail'], 'steps':[{'title':'Retry','description':'Bound attempts','paths':['worker.py'],'citation_ids':['e1']}], 'validation':[{'command':'python -m unittest','expected':'Pass','source':'proposed','citation_ids':[]}], 'risks':[], 'assumptions':[], 'open_questions':[]}
        plan = plans.create(c['id'],content)
        result = {'status':200,'body':{'conversation':c,'plan':plan}}
    else:
        response = api.dispatch(request['method'],request['path'],request.get('query',''),request.get('body'))
        if response is None:
            response = ContextAPI(p,index).dispatch(request['method'],request['path'],request.get('query',''),request.get('body'))
        result = {'status':response.status,'body':response.body} if response else {'status':404,'body':{'code':'not_found'}}
    print(json.dumps(result))
except PlatformError as e:
    print(json.dumps({'status':error_status(e.code),'body':{'code':e.code}}))
except ValueError as e:
    print(json.dumps({'status':400,'body':{'message':str(e)}}))
finally:
    if index is not None:
        index.vectors.close()
    p.close()
