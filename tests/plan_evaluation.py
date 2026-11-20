"""Labeled plan evaluation over the bundled examples through the real Ask stack.

The harness composes the application services exactly as the CLI does, on a
temporary data directory, indexes only the bundled synthetic examples, and
sends each labeled task through ConversationService with intent=plan. Scores
are content-free: states, codes, counts, matched labels and token usage.

Live use (provider key is read from the environment and never printed):

    OPENDEVENGINE_EVAL_API_KEY=... PYTHONPATH=.:tests python -m plan_evaluation \\
        --endpoint https://api.groq.com/openai/v1 --model openai/gpt-oss-120b
"""
from argparse import ArgumentParser
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from opendevengine.ask import ConversationService, PlanService, RoutingService
from opendevengine.index import load_demos
from opendevengine.indexing import IndexService
from opendevengine.platform.platform import Platform
from opendevengine.platform.secrets import BackendStatus
from opendevengine.services import ApplicationServices

ROOT = Path(__file__).resolve().parent.parent
TASKS = Path(__file__).resolve().parent / 'fixtures' / 'plan_eval' / 'tasks.json'


class EphemeralSecretBackend:
    """Process memory only; the evaluation never writes a credential to an OS vault."""
    name = 'evaluation_memory'

    def __init__(self):
        self.values = {}

    def status(self):
        return BackendStatus(self.name, True, None)

    def put(self, ref_id, value):
        self.values[ref_id] = value

    def get(self, ref_id):
        return self.values[ref_id]

    def delete(self, ref_id):
        self.values.pop(ref_id, None)


def load_tasks(path=TASKS):
    return json.loads(Path(path).read_text())


def _mentions(term, texts):
    return any(re.search(r'(?<![\w])(' + term + r')(?![\w])', t, re.I) for t in texts)


def score(task, run, version, source_lines):
    """Return named boolean checks for one task. A missing plan fails every check."""
    checks = {'valid_output': run['state'] == 'completed' and version is not None}
    content = (version or {}).get('content') or {}
    repos = {r['repository_id']: r['role'] for r in content.get('repositories', [])}
    citations = content.get('citations', [])
    questions = content.get('open_questions', [])
    steps = content.get('steps', [])
    readiness = ((version or {}).get('readiness') or {}).get('state')
    checks['not_ready_without_execution'] = checks['valid_output'] and readiness != 'ready'
    checks['citations_resolve'] = checks['valid_output'] and all(source_lines(c) for c in citations)
    if task['kind'] == 'clarify':
        checks['asks_or_declines'] = checks['valid_output'] and (
            any(q['blocking'] for q in questions) or not steps)
        return checks
    texts = [content.get('goal', '')] + list(content.get('requirements', []))
    texts += [s['title'] + ' ' + s['description'] for s in steps]
    texts += [f['text'] for f in content.get('findings', [])]
    expected = {'demo:' + name for name in task['expected_repositories']}
    checks['expected_repositories'] = checks['valid_output'] and expected <= set(repos)
    if task.get('write'):
        checks['write_target'] = checks['valid_output'] and repos.get('demo:' + task['write']) == 'write'
    checks['cites_expected_path'] = checks['valid_output'] and bool(
        {c['path'] for c in citations} & set(task['expected_paths']))
    checks['has_steps_and_validation'] = checks['valid_output'] and bool(steps) and bool(content.get('validation'))
    checks['preserves_requirements'] = checks['valid_output'] and all(
        _mentions(term, texts) for term in task['must_mention'])
    return checks


def compose(data_dir, provider_spec, secret_value=None, *, provider_transport=None):
    platform = Platform.open(data_dir, secret_backend=EphemeralSecretBackend())
    services = ApplicationServices(platform, provider_transport=provider_transport)
    indexing = IndexService(platform.store, platform.jobs, services.github, services.providers, data_dir=data_dir)
    services.enable_context(indexing)
    services.bootstrap_context(load_demos(ROOT / 'opendevengine' / 'examples'), {})
    routing = RoutingService(platform.store, indexing)
    plans = PlanService(platform.store, indexing, services.providers)
    conversations = ConversationService(platform.store, platform.jobs, indexing, services.providers, routing, plans)
    services.enable_ask(routing, plans, conversations)
    profile = services.providers.create_profile(provider_spec, secret_value=secret_value)
    if services.providers.get_version(profile['active_version_id'])['destination']['kind'] != 'local':
        # Only the bundled synthetic examples are indexed in this temporary workspace.
        services.providers.update_settings(local_only=False)
    services.providers.assign_role('planning', profile['id'])
    return platform, services, indexing, conversations


def _wait(predicate, timeout, interval=0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def evaluate(provider_spec, secret_value=None, *, provider_transport=None, tasks=None, task_timeout=600, log=None,
             rate_limit_retries=4, rate_limit_wait=30):
    tasks = tasks or load_tasks()
    with tempfile.TemporaryDirectory() as tmp:
        platform, services, indexing, conversations = compose(
            Path(tmp), provider_spec, secret_value, provider_transport=provider_transport)
        try:
            services.start()
            ids = dict(services.legacy_sources)
            if not _wait(lambda: all(indexing.get_state(r)['status'] == 'ready' for r in ids.values()), 120):
                raise RuntimeError('Bundled examples did not finish indexing.')

            def source_lines(c):
                try:
                    lines = indexing.source(c['repository_id'], c['snapshot_id'], c['path'],
                                            c['start_line'], c['end_line'], context_lines=0)['lines']
                except Exception:
                    return False
                return len(lines) == c['end_line'] - c['start_line'] + 1

            results = []
            for task in tasks['tasks']:
                conversation = conversations.create(repository_ids=[ids[name] for name in task['pins']])['id']
                started = time.monotonic()
                run = conversations.send(conversation, task['prompt'], 'eval-' + task['id'], intent='plan')
                retries = 0
                while True:
                    final = _wait(lambda: (lambda r: r if r['state'] in ('completed', 'failed') else None)(
                        conversations.get_run(run['id'])), task_timeout) or conversations.get_run(run['id'])
                    # Quota pauses measure the provider tier, not plan quality: wait and retry.
                    if final.get('error_code') != 'provider_rate_limited' or retries >= rate_limit_retries:
                        break
                    retries += 1
                    time.sleep(rate_limit_wait)
                    conversations.retry(run['id'])
                plans = conversations.get(conversation)['plans']
                version = plans[0]['version'] if plans else None
                checks = score(task, final, version, source_lines)
                content = (version or {}).get('content') or {}
                generation = (version or {}).get('generation') or {}
                result = {
                    'id': task['id'], 'kind': task['kind'], 'passed': all(checks.values()), 'checks': checks,
                    'run_state': final['state'], 'error_code': final.get('error_code'),
                    'readiness': ((version or {}).get('readiness') or {}).get('state'),
                    'blocking_reasons': ((version or {}).get('readiness') or {}).get('blocking_reasons', []),
                    'counts': {k: len(content.get(k, [])) for k in
                               ('repositories', 'citations', 'findings', 'requirements', 'steps', 'validation', 'open_questions')},
                    'cited_paths': sorted({c['path'] for c in content.get('citations', [])}),
                    'roles': {r['repository_id']: r['role'] for r in content.get('repositories', [])},
                    'usage': generation.get('usage'), 'seconds': round(time.monotonic() - started, 2),
                    'rate_limit_retries': retries,
                }
                results.append(result)
                if log:
                    log(result)
        finally:
            services.close()
            indexing.vectors.close()
            platform.close()
    passed = sum(r['passed'] for r in results)
    valid = sum(r['checks']['valid_output'] for r in results)
    gate = tasks['gate']
    return {
        'tasks_sha256': hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest(),
        'model': provider_spec['model'], 'adapter': provider_spec['adapter'],
        'passed': passed, 'valid_output': valid, 'total': len(results),
        'gate_passed': passed >= gate['min_passed'] and valid >= gate['min_valid_output'],
        'results': results,
    }


def main(argv=None):
    parser = ArgumentParser(description='Run the labeled plan evaluation against one provider.')
    parser.add_argument('--adapter', default='openai_compatible', choices=('openai_compatible', 'ollama', 'claude_cli'))
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--context-limit', type=int, default=8192)
    parser.add_argument('--max-output-tokens', type=int, default=4096)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--output', type=Path, help='Write content-free JSON evidence here.')
    args = parser.parse_args(argv)
    spec = {'name': 'Plan evaluation', 'adapter': args.adapter, 'endpoint': args.endpoint, 'model': args.model,
            'config': {'capabilities': ['chat', 'structured_output'], 'context_limit': args.context_limit,
                       'max_output_tokens': args.max_output_tokens, 'timeout_seconds': args.timeout,
                       'allow_external_source': True}}
    secret = os.environ.get('OPENDEVENGINE_EVAL_API_KEY') or None

    def log(r):
        failed = [k for k, v in r['checks'].items() if not v]
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['id']:<16} {r['run_state']:<9} "
              f"{r['error_code'] or '':<18} {r['seconds']:>6}s {' '.join(failed)}", flush=True)

    report = evaluate(spec, secret, log=log)
    print(f"passed {report['passed']}/{report['total']}, valid output {report['valid_output']}/{report['total']}, "
          f"gate {'PASSED' if report['gate_passed'] else 'FAILED'}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=1, sort_keys=True) + '\n')
    return 0 if report['gate_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
