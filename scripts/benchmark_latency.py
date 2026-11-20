#!/usr/bin/env python3
"""Offline latency baseline. Harness uses stdlib plus existing app/test dependencies.

Reproduces evaluate()'s setup from test_ask_routing_evaluation, without its
post-evaluation mutations. No HTTP server, network, provider or model calls.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


GENERIC = ('self value result item config request response error handler client cache store '
           'update create delete fetch load save parse format validate token record event state '
           'context options payload buffer stream index count limit offset query filter').split()


def source_files(repository, vocabulary, target):
    """Deterministic synthetic Python files producing exactly `target` real chunks.

    Lines are indented (no top-level def/class) so the production chunker emits
    60-line windows; one third of identifiers come from the repository's own profile.
    """
    import random
    from opendevengine.indexing.safety import chunks
    rng = random.Random(repository['id'])
    own = [w.lower().strip(',;.') for w in repository['profile'].split() if len(w) > 3]
    def word():
        pool = own if rng.random() < 1 / 3 else vocabulary if rng.random() < .5 else GENERIC
        return rng.choice(pool)
    files, produced = [], 0
    while produced < target:
        lines = ['class Module%d:' % len(files)]
        while len(lines) < 1000:
            lines.append('    %s_%s = %s(%s)  # %s' % tuple(word() for _ in range(5)))
            if len(lines) % 5 == 0 and len(lines) >= 55 * (target - produced - 2):
                current = sum(1 for _ in chunks('\n'.join(lines)))
                if produced + current > target:
                    del lines[-5:]
                    break
        count = sum(1 for _ in chunks('\n'.join(lines)))
        produced += count
        files.append({'path': 'src/module_%04d.py' % len(files), 'content': '\n'.join(lines).encode()})
    return files


@contextmanager
def fixture(source_chunks=0):
    import test_github_service as github_fixture
    from opendevengine.ask import RoutingService
    from opendevengine.indexing import IndexService
    from opendevengine.platform.worker import Worker
    from opendevengine.providers import ProviderService

    data = json.loads((ROOT / 'tests/fixtures/routing/catalog.json').read_text())
    test = github_fixture.GitHubTests(methodName='runTest')
    test.setUpClass()
    index = None
    try:
        test.setUp()  # TemporaryDirectory, real SQLite, in-memory vault, fake transport.
        test.remote.repos['11'] = []
        for i, repo in enumerate(data['repositories'], 1):
            record = github_fixture.repo(i, repo['name'])
            record.update(description=repo['profile'], topics=[])
            test.remote.repos['11'].append(record)
        test.refresh()

        vocabulary = sorted({w.lower().strip(',;.') for item in data['tasks'] + data['repositories']
                             for w in item.get('prompt', item.get('profile', '')).split() if len(w) > 3})
        # Spread the total exactly; each indexed repository keeps its README chunk.
        share = [source_chunks // 25 + (i < source_chunks % 25) for i in range(25)]

        def reader(source, revision, caps):
            i = int(source['id'].split(':')[1]) - 1
            repository = data['repositories'][i]
            files = [{'path': 'README.md', 'content': repository['profile'].encode()}]
            if source_chunks:
                files += source_files(repository, vocabulary, share[i] - 1)
            return {'revision': 'a' * 40, 'revision_kind': 'git', 'branch': 'main', 'files': files}

        index = IndexService(test.platform.store, test.platform.jobs, test.service,
                             ProviderService(test.platform.store, None), snapshot_reader=reader)
        started = time.perf_counter()
        for i in range(1, 26):
            run = index.request_index('gh:' + str(i))
            worker = Worker(test.platform.jobs, {'index.run': index.run_index}, worker_id='fixture')
            # Large snapshots checkpoint at the elapsed cap and resume as the same job.
            while index.get_run(run['id'])['state'] not in ('completed', 'failed', 'cancelled'):
                if not worker.run_once():
                    break
            if index.get_run(run['id'])['state'] != 'completed':
                raise RuntimeError('Fixture indexing failed: %s' % index.get_run(run['id']))
        test.indexing_seconds = time.perf_counter() - started
        # Fail closed if any measured operation unexpectedly attempts transport.
        def offline(request):
            raise RuntimeError('Unexpected transport during measurement')
        test.service.client.transport = offline
        yield test, index, RoutingService(test.platform.store, index), data
    finally:
        if index is not None:
            index.vectors.close()
        test.doCleanups()


def summary(values):
    ordered = sorted(values)
    def percentile(p):
        return ordered[max(0, math.ceil(len(ordered) * p) - 1)]
    return {'samples': len(values), 'p50_ms': percentile(.5),
            'p95_ms': percentile(.95), 'max_ms': max(values)}


def timed(call):
    start = time.perf_counter_ns()
    result = call()
    return (time.perf_counter_ns() - start) / 1_000_000, result


def benchmark(reads, repeats, prompt_limit, source_chunks=0):
    with fixture(source_chunks) as (test, index, routing, data):
        prompts = data['tasks'][:prompt_limit]
        # UI emits only optional query, plus limit=100 and cursors. Cover empty,
        # broad multi-page, selective and no-match searches, not invented UI filters.
        variants = {'all': '', 'broad': 'owner', 'selective': 'billing',
                    'no_match': 'zzzz-no-such-repository'}
        calls = []
        cold = {}
        cold_values = []
        for label, query in variants.items():
            cursor = None
            values = []
            while True:
                kwargs = dict(limit=100, query=query, cursor=cursor)
                elapsed, page = timed(lambda: test.service.list_repositories(**kwargs))
                values.append(elapsed)
                cold_values.append(elapsed)
                calls.append((label, kwargs))
                cursor = page['next_cursor']
                if cursor is None:
                    break
            cold[label] = summary(values)
        route_cold = [timed(lambda p=p: routing.route(p['prompt']))[0] for p in prompts]
        # Explicit full warm-up, unmeasured; route() persists each decision, as in production.
        for _, kwargs in calls:
            test.service.list_repositories(**kwargs)
        for prompt in prompts:
            routing.route(prompt['prompt'])
        warm = {label: [] for label in variants}
        # N reads per distinct variant/page, rather than hiding later pages in aggregate.
        pages = []
        for label, kwargs in calls:
            values = [timed(lambda: test.service.list_repositories(**kwargs))[0]
                      for _ in range(reads)]
            warm[label].extend(values)
            pages.append({'variant': label, 'page': 1 + sum(p['variant'] == label for p in pages),
                          **summary(values)})
        route_values = []
        per_prompt = []
        for position, prompt in enumerate(prompts, 1):
            values = [timed(lambda: routing.route(prompt['prompt']))[0]
                      for _ in range(repeats)]
            route_values.extend(values)
            per_prompt.append({'prompt_number': position, **summary(values)})
        catalog_values = [v for values in warm.values() for v in values]
        with test.platform.store.read() as db:
            chunks = db.execute('SELECT count(*) FROM code_chunks').fetchone()[0]
            files = db.execute('SELECT count(DISTINCT snapshot_id || path) FROM code_chunks').fetchone()[0]
            page = db.execute('PRAGMA page_size').fetchone()[0] * db.execute('PRAGMA page_count').fetchone()[0]
        catalog_summary = summary(catalog_values)
        routing_summary = summary(route_values)
        return {
            'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                            'platform': platform.platform(), 'processor': platform.processor(),
                            'cpu_count': os.cpu_count()},
            'fixture': {'repositories': len(data['repositories']), 'indexed_repositories': 25,
                        'chunks': chunks, 'files': files,
                        'file_content': 'synthetic profile in README.md' + (
                            '; deterministic synthetic Python under src/' if source_chunks else ''),
                        'labelled_prompts': len(prompts), 'embedding_models': 0,
                        'database_bytes': page, 'indexing_seconds': round(test.indexing_seconds, 1)},
            'method': {'percentile': 'nearest rank', 'reads_per_variant_page': reads,
                       'routing_repeats_per_prompt': repeats,
                       'cold': 'first-use after fixture construction; OS/SQLite caches not flushed',
                       'warm': 'same process/database after full unmeasured warm-up',
                       'scope': 'service calls only; no HTTP/JSON/browser, network, clone, model or container startup'},
            'catalog': {'queries': variants, 'cold_by_variant': cold,
                        'cold': summary(cold_values),
                        'warm': catalog_summary, 'warm_by_variant': {k: summary(v) for k, v in warm.items()},
                        'warm_by_page': pages},
            'routing': {'cold': summary(route_cold), 'warm': routing_summary,
                        'warm_by_prompt': per_prompt},
            'gates': {'catalog_p95_under_1000_ms': catalog_summary['p95_ms'] < 1000,
                      'routing_p95_under_2000_ms': routing_summary['p95_ms'] < 2000}}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def source_total(value):
    number = int(value)
    if number != 0 and not 25 <= number <= 250_000:
        raise argparse.ArgumentTypeError('must be 0 or 25-250000 (25 indexed repositories, 10,000 chunks each)')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog-reads', type=positive, default=100,
                        help='warm reads per query variant/page (default: 100)')
    parser.add_argument('--routing-repeats', type=positive, default=5)
    parser.add_argument('--prompt-limit', type=positive, default=50,
                        help='reduce prompts for smoke checks; full baseline uses all 50')
    parser.add_argument('--source-chunks', type=source_total, default=0,
                        help='total eligible chunks across the 25 indexed repositories, e.g. 100000 '
                             'for the PRD reference workload (default: README profiles only)')
    parser.add_argument('--assert', dest='assert_gates', action='store_true')
    args = parser.parse_args()
    result = benchmark(args.catalog_reads, args.routing_repeats, args.prompt_limit, args.source_chunks)
    print(json.dumps(result, indent=2))
    return int(args.assert_gates and not all(result['gates'].values()))


if __name__ == '__main__':
    sys.exit(main())
