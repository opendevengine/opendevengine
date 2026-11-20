"""Real composed APIs persist saved conversations with context workers enabled."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from opendevengine.platform import Platform
from opendevengine.services import ApplicationServices
from opendevengine.indexing import IndexService
from opendevengine.ask import RoutingService,PlanService,ConversationService
from opendevengine.__main__ import ASSETS

class AskCompositionTests(TestCase):
    def test_public_saved_ask_survives_application_restart(self):
        self.assertEqual(ASSETS['/ask.js'][0],'ask.js')
        with TemporaryDirectory() as directory:
            conversation_id=None
            for restart in (False,True):
                platform=Platform.open(Path(directory));services=ApplicationServices(platform)
                try:
                    index=IndexService(platform.store,platform.jobs,services.github,services.providers)
                    services.enable_context(index)
                    routing=RoutingService(platform.store,index)
                    plans=PlanService(platform.store,index,services.providers)
                    conversations=ConversationService(platform.store,platform.jobs,index,services.providers,routing,plans)
                    services.enable_ask(routing,plans,conversations)
                    self.assertIn('index.run',services.handlers)
                    self.assertIn('ask.answer',services.handlers)
                    if not restart:
                        result=services.api.dispatch('POST','/api/conversations',body={'title':'Saved end-to-end setup'})
                        self.assertEqual(result.status,201);conversation_id=result.body['id']
                    else:
                        saved=services.api.dispatch('GET','/api/conversations/'+conversation_id).body
                        self.assertEqual(saved['title'],'Saved end-to-end setup')
                        self.assertEqual(saved['messages'],[])
                finally:
                    services.close();platform.close()


    def test_cli_model_seeds_missing_roles_without_inference_or_overwriting_saved_choices(self):
        import json,socket,subprocess,sys,time,urllib.request
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with TemporaryDirectory() as directory:
            for model in ('first-cli-model','replacement-cli-model'):
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
                process=subprocess.Popen([sys.executable,'-m','opendevengine','--data-dir',directory,'--port',str(port),'--model',model],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    deadline=time.monotonic()+15
                    while True:
                        try:
                            with opener.open(f'http://127.0.0.1:{port}/api/settings/model-roles',timeout=1) as response:
                                roles=json.load(response)['roles']
                            break
                        except OSError:
                            if process.poll() is not None or time.monotonic()>deadline:
                                self.fail('CLI did not serve saved Ask')
                            time.sleep(.05)
                    self.assertEqual(roles['ask']['model'],'first-cli-model')
                    self.assertEqual(roles['planning']['model'],'first-cli-model')
                    self.assertEqual(roles['ask']['profile_id'],roles['planning']['profile_id'])
                    base=f'http://127.0.0.1:{port}'
                    if model=='first-cli-model':
                        request=urllib.request.Request(base+'/api/repositories/demo:orders-api/purge',b'{}',{'Content-Type':'application/json','Origin':base})
                        with opener.open(request,timeout=5) as response:response.read()
                    else:
                        with opener.open(base+'/api/repositories/demo:orders-api/context',timeout=1) as response:
                            context=json.load(response)
                        self.assertTrue(context['purged']);self.assertFalse(context['eligible'])
                        with opener.open(base+'/api/repos',timeout=1) as response:
                            self.assertNotIn('orders-api',[r['name'] for r in json.load(response)['repos']])
                    with opener.open(f'http://127.0.0.1:{port}/ask.js',timeout=1) as response:
                        self.assertIn(b'createAskPanel',response.read())
                finally:
                    process.terminate()
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait();self.fail('CLI did not stop')
