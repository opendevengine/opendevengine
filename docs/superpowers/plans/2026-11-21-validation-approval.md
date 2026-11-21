# Validation Command Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validation commands run in a session only when the user approved them in the plan version; unapproved ones are skipped with a warning (issue #9).

**Architecture:** An optional boolean `approved` on each plan validation item, forced `false` for any non-manual plan version. Readiness warns when a check is unapproved. The session attempt skips unapproved items without calling the runner. The web UI shows approval state, lets the user tick "Run this check", and labels skipped checks.

**Tech Stack:** Python 3.10+ stdlib (`unittest`), SQLite, vanilla ES modules with `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-21-validation-approval-design.md`

## Global Constraints

- No schema migration: `validation_runs` already has `required` and `environment_json`.
- Missing `approved` means not approved; existing plan versions and their hashes stay unchanged.
- Only versions with provenance `manual` may carry `approved: true`; `model` and `amendment` are forced to `false`.
- Readiness warning code: `validation_unapproved` (a warning, never a blocking reason).
- Skip reason code: `not_approved`, recorded in `environment_json.reason`, `status='not_run'`, `required=0`.
- An unapproved command must never be passed to `runner.run_command`.
- Copy (exact):
  - plan view, unapproved: `Not approved — this check will be skipped. Approve it by editing the plan.`
  - plan view, approved: `Approved — runs as a shell command in the session container.`
  - readiness warning: `Some checks are not approved and will be skipped.`
  - editor checkbox label: `Run this check (runs the command as shell in the session container)`
  - editor prose warning: `This looks like a sentence, not a command.`
  - sessions view: `Skipped: not approved in the plan.`
- Run tests the way CI does: `python -m unittest discover -s tests` and `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/*.test.mjs`.

## Review Focus

1. A model plan whose JSON says `"approved": true` (non-native path goes straight through `plans.validate`) must still be stored unapproved — Task 1 test `test_model_version_is_forced_unapproved`.
2. A user saving a new version of a model plan without touching checks keeps them unapproved; ticking one approves only that one — Task 1 test `test_manual_save_keeps_user_approval`.
3. Agent fails before validation: the "nothing ran" path (`_validate(run=False)`) must record unapproved items as `not_approved`, not `agent_not_completed` — Task 2 test `test_unapproved_reason_wins_when_agent_fails`.
4. Command text with backticks and `$(...)` that is unapproved never reaches the runner — Task 2 test `test_unapproved_command_never_reaches_runner`.
5. Publication with skipped checks: auto-publish (`on_success_draft`) must not fire; manual draft still allowed — Task 2 test `test_skipped_checks_block_auto_publication`.

---

### Task 1: Plan content carries approval

**Files:**
- Modify: `opendevengine/ask/plans.py` (`validate` ~line 87–101, `_insert` ~line 129, `_readiness` ~line 168–215)
- Test: `tests/test_validation_approval.py` (create; plan tests in class `PlanApprovalTests`, reuse the `SessionCase` fixture from `tests/test_sessions_fixture.py` for a ready plan service)

**Interfaces:**
- Produces: validation item shape `{'command','expected','source','citation_ids'}` plus optional `'approved': bool`. Helper `approved(item: dict) -> bool` in `opendevengine/ask/plans.py` returning `item.get('approved') is True`; Task 2 imports it.

- [ ] **Step 1: Write failing tests**

```python
class PlanApprovalTests(SessionCase):
    def check(self, **extra):
        return {'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': [], **extra}

    def test_missing_approved_is_accepted_and_warns(self):
        plan = self.plans.create(None, self.content(validation=[self.check()]))
        r = plan['version']['readiness']
        self.assertEqual(r['state'], 'ready')
        self.assertIn('validation_unapproved', r['warnings'])
        self.assertNotIn('approved', plan['version']['content']['validation'][0])

    def test_non_boolean_approved_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plans.create(None, self.content(validation=[self.check(approved='yes')]))

    def test_manual_save_keeps_user_approval(self):
        plan = self.plans.create(None, self.content(validation=[self.check(), self.check(command='npm test')]))
        content = plan['version']['content']; content['validation'][1]['approved'] = True
        saved = self.plans.save_version(plan['id'], content, plan['version']['id'])
        v = saved['version']['content']['validation']
        self.assertEqual([x.get('approved') for x in v], [None, True])
        self.assertIn('validation_unapproved', saved['version']['readiness']['warnings'])

    def test_all_approved_has_no_warning(self):
        plan = self.plans.create(None, self.content(validation=[self.check(approved=True)]))
        self.assertNotIn('validation_unapproved', plan['version']['readiness']['warnings'])

    def test_model_version_is_forced_unapproved(self):
        plan = self.plans.create(None, self.content(validation=[self.check()]))
        content = plan['version']['content']; content['validation'][0]['approved'] = True
        saved = self.plans.save_version(plan['id'], content, plan['version']['id'], source='model', profile_version_id='pv_x')
        self.assertIs(saved['version']['content']['validation'][0]['approved'], False)
```

(If `source='model'` with a fake profile id is refused elsewhere in `_insert`, call `self.plans._insert` inside `self.store.transaction()` as `conversations.py:463` does.)

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m unittest discover -s tests -p test_validation_approval.py`
Expected: FAIL (`approved` rejected by `shape` as an unknown key; no warning).

- [ ] **Step 3: Implement**

In `validate`, pass `optional=('approved',)` for validation items and reject `approved` that is present and not `bool`. Add `approved(item)`. In `_insert`, before encoding, when `source != 'manual'` set `item['approved'] = False` on every validation item (on a copy, so the caller's dict is untouched). In `_readiness`, append `validation_unapproved` to `warnings` if any validation item is not `approved(item)`.

- [ ] **Step 4: Run Task 1 tests and the plan suites**

Run: `python -m unittest discover -s tests -p "test_validation_approval.py"` then `python -m unittest discover -s tests -p "test_plan*.py"` and `-p "test_ask*.py"` and `-p test_native_plan_generation.py`
Expected: all PASS. If an existing test asserts an exact `warnings` list, add `validation_unapproved` there only where its plan has unapproved checks.

- [ ] **Step 5: Commit**

```bash
git add opendevengine/ask/plans.py tests/test_validation_approval.py
git commit -m "feat(plans): record user approval of validation commands (#9)"
```

### Task 2: Sessions skip unapproved checks

**Files:**
- Modify: `opendevengine/sessions/attempt.py` (`_validate` ~line 853–930, outcome ~line 958–983 unchanged in logic)
- Modify: `opendevengine/sessions/service.py` (`validations` ~line 869–891)
- Modify fixtures so existing session tests keep running their checks: `tests/test_sessions_fixture.py:534`, `tests/test_sessions.py:138-140`, `tests/test_sessions_docker.py` (3 items), `tests/ui/session-service-bridge.py` if it builds plans with validation — add `'approved': True`.
- Test: `tests/test_validation_approval.py` (class `SessionApprovalTests(SessionCase)`)

**Interfaces:**
- Consumes: `approved(item)` from `opendevengine.ask.plans`.
- Produces: `SessionService.validations(sid)` returns an extra key `'skipped_not_approved': int`; each skipped item has `environment['reason'] == 'not_approved'`, `status == 'not_run'`, `required is False`.

- [ ] **Step 1: Write failing tests**

Use the fixture's `script_fix()` and `world.commands` (keyed by the `sh -c` text, see `FakeRunner.run_command`).

```python
class SessionApprovalTests(SessionCase):
    BAD = 'Run `python3 -m http.server 8000` and $(touch /tmp/x) check it.'

    def test_unapproved_command_never_reaches_runner(self):
        self.script_fix(); called = []
        self.world.commands[self.BAD] = lambda *a, **k: called.append(1) or {'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False, 'truncated': False}
        _, v = self.ready_plan(validation=[
            {'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': [], 'approved': True},
            {'command': self.BAD, 'expected': 'x', 'source': 'proposed', 'citation_ids': []}])
        self.sessions.start()
        sid = self.sessions.launch(v['id'], v['content_hash'], 'k-approve')['session_id']
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'completed', final)
        self.assertEqual(called, [])
        items = self.sessions.validations(sid)
        self.assertEqual([i['status'] for i in items['items']], ['passed', 'not_run'])
        self.assertEqual(items['items'][1]['environment']['reason'], 'not_approved')
        self.assertFalse(items['items'][1]['required'])
        self.assertEqual(items['skipped_not_approved'], 1)
        self.assertFalse(final['outcome']['validation']['fully_validated'])
        self.assertNotIn(self.BAD, json.dumps(self.all_events(sid)))  # no validation.started for it
```

Match `'passed'` to whatever the fixture's handler for `python test_calc.py` returns after `script_fix()`; if the fixture has no handler, assert the first status is not `'not_run'`.

- `test_unapproved_reason_wins_when_agent_fails`: script the agent to fail (use the fixture's existing failing-agent helper as in `tests/test_sessions.py`), plan with an approved check then an unapproved one; assert `[i['environment']['reason'] for i in items] == ['agent_not_completed', 'not_approved']`.
- `test_skipped_checks_block_auto_publication`: plan with `publication={'policy': 'on_success_draft'}` and one unapproved check, session completes with a change; assert `final['outcome']['publication_eligible'] is False`.

- [ ] **Step 2: Run and confirm they fail**

Run: `python -m unittest discover -s tests -p test_validation_approval.py`
Expected: SessionApprovalTests FAIL (unapproved command is run; no `skipped_not_approved`).

- [ ] **Step 3: Implement**

In `_validate`, when `not approved(item)`: set `reason = 'not_approved'`, leave `status = 'not_run'`, skip the runner branch and the `validation.started` event, and insert with `required = 0` (the INSERT currently hard-codes `1`; pass it as a parameter). This check comes before the `run`/`self.reason` branches so it wins over `agent_not_completed` and `stopped`. In `validations()`, add `'skipped_not_approved'` = count of items with `status == 'not_run'` and `environment.get('reason') == 'not_approved'`.

- [ ] **Step 4: Update fixtures, then run the session suites**

Add `'approved': True` to the fixture validation items listed under Files.
Run: `python -m unittest discover -s tests -p "test_session*.py"` and `-p test_validation_approval.py` and `-p "test_publication*.py"`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add opendevengine/sessions tests
git commit -m "feat(sessions): skip validation commands the plan did not approve (#9)"
```

### Task 3: Show and edit approval in the web UI

**Files:**
- Modify: `opendevengine/web/plan-view.js` (`WARNINGS` ~line 18; validation section ~line 113–122)
- Modify: `opendevengine/web/ask.js` (validation editor row ~line 468; new-item default ~line 472)
- Modify: `opendevengine/web/sessions.js` (check item ~line 984; `renderChecks` ~line 265–276)
- Test: `tests/ui/plan-view.test.mjs`, `tests/ui/ask.test.mjs` (or whichever file covers the plan editor), `tests/ui/session-ux.test.mjs`

**Interfaces:**
- Consumes: plan item `approved`, readiness warning `validation_unapproved`, validations `skipped_not_approved` and item `environment.reason`.

- [ ] **Step 1: Write failing UI tests**
  - plan-view: a version with checks `[{approved: true}, {}]` renders both exact plan-view strings from Global Constraints; the old `Proposed, not run yet.` assertion (`tests/ui/plan-view.test.mjs:47`) is replaced. Readiness warnings `['validation_unapproved']` render `Some checks are not approved and will be skipped.`
  - editor: each validation row has a checkbox with the exact label; ticking it sets `approved: true` in the saved POST body; editing the command text of an approved row leaves `approved: true` (spec: the user wrote the new text); a new validation row defaults to `approved: false`; a command of `Run the tests. Then check output` shows the prose warning, `python -m unittest` does not.
  - sessions: a check with `status: 'not_run'` and `environment: {reason: 'not_approved'}` shows `Skipped: not approved in the plan.`; `renderChecks` with `skipped_not_approved: 1` and `summary.not_run: 1` does not list `not run` among failing checks.

- [ ] **Step 2: Run and confirm they fail**

Run: `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/plan-view.test.mjs tests/ui/ask.test.mjs tests/ui/session-ux.test.mjs`
Expected: FAIL on the new assertions.

- [ ] **Step 3: Implement**

Prose heuristic `looksLikeProse(command: string) -> boolean` in `ask.js`: true when the trimmed text contains `". "` followed by a letter, or ends with `.` and has at least 5 words. Warning re-evaluates on input. Subtract `skipped_not_approved` from `not_run` before computing `failing` in `renderChecks`, and append `N skipped (not approved)` to the message when non-zero.

- [ ] **Step 4: Run UI tests and web asset checks**

Run: `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/*.test.mjs` and `python -m unittest tests.test_web_assets`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add opendevengine/web tests/ui
git commit -m "feat(web): show, edit and report validation approval (#9)"
```

### Task 4: Changelog and full verification

**Files:**
- Modify: `CHANGELOG.md` (under `## [Unreleased]`)

- [ ] **Step 1: Add entry**

Under `### Changed` (create the heading if absent):
`- **Validation checks need your approval:** a check runs only if you ticked "Run this check" in the plan. Checks from the model start unapproved; unapproved checks are skipped and shown as skipped, and a session with skipped checks is never published automatically. Saved plans need a new version with checks approved. (#9)`

- [ ] **Step 2: Full suite**

Run: `python -m unittest discover -s tests` and `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/*.test.mjs`
Expected: `OK` and all node tests pass.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for validation approval (#9)"
```
