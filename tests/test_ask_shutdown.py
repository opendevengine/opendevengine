"""A blocked provider call cancels cooperatively and resumes without duplicate messages."""
import threading
import time
from types import SimpleNamespace
from unittest import TestCase
import test_ask_services as fixtures
from opendevengine.providers.transport import TransportCancelled
from opendevengine.services import ApplicationServices


class AskShutdownTests(TestCase):
    def test_shutdown_interrupts_inflight_provider_and_requeues_without_losing_request(self):
        f=fixtures.AskTests(methodName='runTest');f.setUp();self.addCleanup(f.tearDown)
        entered=threading.Event()
        def block():
            entered.set()
            if f.transport.cancel_events[-1].wait(9):
                raise TransportCancelled('synthetic cooperative cancellation')
            self.fail('shutdown did not cancel provider')
        f.transport.callback=block
        services=ApplicationServices(SimpleNamespace(store=f.store,jobs=f.jobs,secrets=None))
        services.enable_context(f.index)
        services.enable_ask(f.routing,f.plans,f.service)
        run=f.service.send(f.conv,'How does send_confirmation work?','shutdown-request')
        services.start()
        try:
            self.assertTrue(entered.wait(3))
            started=time.monotonic();services.close()
            self.assertLess(time.monotonic()-started,8)
            self.assertFalse(services.running)
            self.assertEqual(f.jobs.get(run['job_id']).state,'queued')
            self.assertEqual(f.jobs.get(run['job_id']).attempts,0)
            self.assertEqual(f.service.get_run(run['id'])['state'],'running')
            # Recreate the vector connection as application restart would.
            from opendevengine.indexing.vectors import VectorStore
            f.index.vectors=VectorStore(f.root/'data')
            f.transport.callback=None;f.work()
            self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
            saved=f.service.get(f.conv)
            self.assertEqual(len(saved['messages']),2)
            self.assertEqual(sum(m['role']=='user' for m in saved['messages']),1)
        finally:
            services.close()
