"""The plan evaluation harness scores the real Ask path; a scripted model checks the scoring."""
import json
import unittest

from opendevengine.providers import TransportResponse
import plan_evaluation

SPEC = {'name': 'scripted', 'adapter': 'ollama', 'endpoint': 'http://127.0.0.1:11434', 'model': 'scripted',
        'config': {'capabilities': ['chat', 'structured_output'], 'context_limit': 16384, 'max_output_tokens': 2048}}


class ScriptedPlanner:
    """Answers like a careful model, using only the evidence and repositories it was offered."""

    def __init__(self, invent=False):
        self.invent, self.calls = invent, 0

    def send(self, request, *, cancel_event=None):
        self.calls += 1
        body = json.loads(request.body)
        payload = json.loads(body['messages'][1]['content'])
        schema = body['format']['properties']
        repositories = schema['repositories']['items']['properties']['repository_id'].get('enum', [])
        evidence = payload['evidence']
        clarify = any(word in payload['request'] for word in ('Stripe', 'better'))
        refs = [] if clarify else list(evidence)
        writes = [r for r in repositories if r != 'demo:notifications-worker'] or repositories
        draft = {
            'goal': payload['request'][:80],
            'repositories': [] if clarify else [{'repository_id': r, 'role': 'write' if r == writes[0] else 'read'}
                                                for r in repositories],
            'citation_ids': refs + (['e999'] if self.invent else []),
            'findings': [{'text': 'Unknown: no matching source.' if clarify else 'Existing behavior.', 'citation_ids': refs[:1]}],
            'requirements': [payload['request']],
            'steps': [] if clarify else [{'title': 'Change', 'description': payload['request'],
                                          'paths': [evidence[refs[0]]['path']], 'citation_ids': refs[:1]}],
            'validation': [] if clarify else [{'command': 'make test', 'expected': 'pass', 'citation_ids': []}],
            'risks': [], 'assumptions': [],
            'open_questions': [{'text': 'What should change?', 'blocking': True}] if clarify else [],
        }
        message = {'done': True, 'message': {'content': json.dumps(draft)}, 'prompt_eval_count': 100, 'eval_count': 50}
        return TransportResponse(200, {}, json.dumps(message).encode())


class PlanEvaluationTests(unittest.TestCase):
    def test_careful_model_passes_every_labeled_task_through_the_real_stack(self):
        planner = ScriptedPlanner()
        report = plan_evaluation.evaluate(SPEC, provider_transport=planner, task_timeout=30)
        failures = {r['id']: [k for k, v in r['checks'].items() if not v] for r in report['results'] if not r['passed']}
        self.assertEqual(failures, {})
        self.assertTrue(report['gate_passed'])
        self.assertEqual(report['total'], len(plan_evaluation.load_tasks()['tasks']))
        self.assertEqual(planner.calls, report['total'])
        outbox = next(r for r in report['results'] if r['id'] == 'outbox-retry')
        self.assertEqual(outbox['readiness'], 'needs_input')
        self.assertIn('execution_not_configured', outbox['blocking_reasons'])
        self.assertEqual((outbox['usage']['input_tokens'], outbox['usage']['output_tokens']), (100, 50))
        # Evidence is content-free: no prompts, plan text or source lines.
        text = json.dumps(report)
        self.assertNotIn('OutboxPublisher fails', text)
        self.assertNotIn('kafka.send', text)

    def test_invented_evidence_is_rejected_by_the_server_and_scored_invalid(self):
        tasks = plan_evaluation.load_tasks()
        tasks['tasks'] = tasks['tasks'][:1]
        planner = ScriptedPlanner(invent=True)
        report = plan_evaluation.evaluate(SPEC, provider_transport=planner, tasks=tasks, task_timeout=30)
        result = report['results'][0]
        self.assertFalse(result['checks']['valid_output'])
        self.assertEqual((result['run_state'], result['error_code']), ('failed', 'invalid_output'))
        self.assertEqual(planner.calls, 2)  # one repair attempt, then a durable failure
        self.assertFalse(report['gate_passed'])

    def test_rate_limited_task_is_retried_rather_than_scored(self):
        tasks = plan_evaluation.load_tasks()
        tasks['tasks'] = tasks['tasks'][:1]

        class QuotaThenPlan(ScriptedPlanner):
            def send(self, request, *, cancel_event=None):
                if self.calls == 0:
                    self.calls += 1
                    return TransportResponse(429, {'retry-after': '1'}, b'{"error":{"code":"rate_limit_exceeded"}}')
                return super().send(request, cancel_event=cancel_event)

        planner = QuotaThenPlan()
        report = plan_evaluation.evaluate(SPEC, provider_transport=planner, tasks=tasks, task_timeout=30,
                                          rate_limit_wait=0)
        result = report['results'][0]
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['rate_limit_retries'], 1)

    def test_requirement_terms_match_whole_tokens(self):
        self.assertTrue(plan_evaluation._mentions('5', ['at most 5 attempts']))
        self.assertFalse(plan_evaluation._mentions('5', ['retry 15 times']))
        self.assertTrue(plan_evaluation._mentions('blank|empty', ['skip EMPTY addresses']))


if __name__ == '__main__':
    unittest.main()
