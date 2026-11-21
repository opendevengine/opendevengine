# AI providers settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Settings → AI providers section where the user adds Claude, Codex or Local providers (API key / URL) and picks a provider per job; coding setup and its test happen automatically.

**Architecture:** A façade (`opendevengine/providers/catalog.py`, class `AIProviders`) stores provider records in a new `ai_providers` table and creates/reuses the existing provider profiles, role assignments and one managed execution profile. Execution profiles gain an `api_key` auth mode and a managed flag (migration 16). The web page is a new `settings-providers.js`; the old Models and Coding agent UIs move under Advanced unchanged.

**Tech Stack:** Python stdlib + SQLite, vanilla ES modules, `unittest`, `node --test` with the fake DOM in `tests/ui/dom.mjs`.

**Spec:** `docs/superpowers/specs/2026-11-21-ai-providers-design.md`

## Global Constraints

- Python tests run from the repo root: `python3 -m unittest discover -s tests`. UI: `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/*.test.mjs`.
- No merging of provider and execution profile tables; existing profiles keep working.
- API keys go to the vault (`SecretStore.create('provider_api_key', label, value)`), never into responses, logs, job payloads or `config_json`.
- Adding a Claude or Codex provider requires `consent: true` server-side; it turns `local_only` off. Later job choices do not ask.
- Any URL saved as a Local provider is trusted like `--ollama-url` (`destination kind 'local'`).
- Coding defaults: image `python:3.12-slim`, `AGENT_DEFAULTS`, default runner limits, `allow_external_source: true`.
- Endpoints: Claude API key chat → `openai_compatible` at `https://api.anthropic.com/v1`; Codex → `openai_compatible` at `https://api.openai.com/v1` (editable); Claude login → `claude_cli`; Local → `ollama` when the URL answers Ollama discovery, else `openai_compatible`.
- Copy fixed by the spec: consent "This lets OpenDevEngine send parts of your code to Anthropic/OpenAI." (name the one company for the kind); Local note "Treated as your own machine: code is sent here without asking."; blocked job "Blocked by privacy setting".
- Phase 1 only: Codex and Local are not offered for Coding; the Coding dropdown shows Codex as "Coding — coming later", disabled.

## Review Focus

1. Removing a provider a job uses: job becomes unassigned, the managed execution profile is deleted, plans show `execution_not_configured`; nothing 500s. (Task 4 test `test_remove_provider_clears_jobs`.)
2. Rotating the Claude key: both the chat profile and the coding profile get the new key; coding re-tests; the old refs are revoked. (Task 4 `test_rotate_key_updates_chat_and_coding`.)
3. Claude/Codex added via the API without `consent` is rejected, not just hidden in the UI. (Task 4 `test_remote_provider_requires_consent`.)
4. Two Local providers on the same host: removing one keeps the host trusted. (Task 3 `test_trusted_hosts_are_reference_counted` via `set_trusted_hosts`, Task 4 `test_remove_local_keeps_shared_host`.)
5. The key never appears in any API response or job payload. (Task 4 `test_key_never_returned`.)

---

### Task 1: Execution profiles accept api_key and a managed flag (migration 16)

**Files:**
- Modify: `opendevengine/platform/execution_schema.py`, `opendevengine/platform/migrations.py`, `opendevengine/execution/profiles.py`
- Test: `tests/test_execution_schema.py`, `tests/test_execution_profiles.py`

**Interfaces:**
- Produces: migration 16 `"ai_providers"` (`rebuild_tables=True`, pattern of `PROVIDER_ADAPTER_V12_SQL`): rebuild `execution_profile_versions` with `auth_mode IN ('cli_login','api_key')` and new nullable `secret_ref TEXT REFERENCES secret_refs(id)`; `ALTER TABLE execution_profiles ADD COLUMN managed INTEGER NOT NULL DEFAULT 0 CHECK(managed IN (0,1))`; create `ai_providers (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('claude','codex','local')), name TEXT NOT NULL, endpoint TEXT, auth TEXT NOT NULL CHECK(auth IN ('api_key','cli_login','none')), secret_ref TEXT REFERENCES secret_refs(id), created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)`; create `ai_provider_profiles (provider_id TEXT NOT NULL REFERENCES ai_providers(id), kind TEXT NOT NULL CHECK(kind IN ('provider','execution')), profile_id TEXT NOT NULL, model TEXT NOT NULL, PRIMARY KEY(kind, profile_id))`.
- Produces: `ExecutionProfiles.create(spec, *, managed=False)`; spec may carry `auth_mode: 'api_key'` with `secret_ref` (required for api_key, forbidden for cli_login); `update(ident, changes)` accepts `secret_ref`; `_version()` exposes `secret_ref` presence as `has_secret: bool`, never the ref id in public output; `default_profile_id()` returns the managed profile when its active version passed, else today's rule.

- [ ] **Step 1: Failing tests**

```python
# test_execution_schema.py
def test_migration_16_allows_api_key_auth(self):  # insert a version row with auth_mode='api_key' succeeds; 'token' raises sqlite3.IntegrityError
def test_migration_16_keeps_existing_versions(self):  # DB at v15 with one cli_login version migrates; row, id and config_json unchanged
# test_execution_profiles.py
def test_api_key_mode_requires_secret_ref(self):  # create(auth_mode='api_key') without secret_ref -> ExecutionProfileError code 'validation'
def test_cli_login_rejects_secret_ref(self):       # -> 'validation'
def test_managed_profile_wins_default(self):       # two passed profiles, one managed -> default_profile_id() == managed id
def test_default_rule_unchanged_without_managed(self):  # two passed unmanaged -> None
def test_public_version_hides_secret_ref(self):    # get(id) has version['has_secret'] is True and no 'secret_ref' key anywhere
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_execution_schema tests.test_execution_profiles` — Expected: the new tests FAIL (CHECK constraint / unexpected kwarg).
- [ ] **Step 3: Implement** migration 16 and the `profiles.py` changes above. `_FIELDS` gains `secret_ref`; `_validate` returns `auth_mode` from the spec.
- [ ] **Step 4: Run** the same command — Expected: OK. Then full Python suite — Expected: OK.
- [ ] **Step 5: Commit** `feat(execution): api_key auth mode and managed profiles (migration 16)`

### Task 2: Claude Code runs with an API key

**Files:**
- Modify: `opendevengine/execution/claude.py`, `opendevengine/execution/profiles.py:~609`, `opendevengine/sessions/attempt.py:~784`, `opendevengine/sessions/service.py:~315`
- Test: `tests/test_execution_adapter.py`, `tests/test_sessions.py`

**Interfaces:**
- Consumes: Task 1 `secret_ref` on execution versions.
- Produces: `ClaudeAdapter.run(endpoint, prompt, *, auth_mode, api_key=None, ...)`. `api_key` is a `str` resolved by the caller from the vault right before launch; `auth_mode='api_key'` requires it, `cli_login` forbids it.

- [ ] **Step 1: Record the CLI's auth output.** Run `env -i PATH="$PATH" HOME="$HOME" ANTHROPIC_API_KEY=sk-ant-test claude auth status --json` (or the exact metadata argv `_check_auth_profile` uses) and the same without the variable. Paste both outputs into the ledger as `Task 2: Ruling: api-key auth profile is <fields>`. If no first-party API-key profile can be recognised, stop and ask: the design depends on it.
- [ ] **Step 2: Failing tests**

```python
def test_api_key_mode_sets_env_only_for_cli(self):   # fake CLI sees ANTHROPIC_API_KEY == key; os.environ unchanged
def test_api_key_mode_requires_key(self):            # run(auth_mode='api_key', api_key=None) -> AdapterError('unsupported_auth_mode')
def test_cli_login_ignores_ambient_key(self):        # os.environ has ANTHROPIC_API_KEY; cli_login child env lacks it
def test_api_key_mode_rejects_third_party_provider(self):  # auth status apiProvider 'bedrock' -> AdapterError('unsupported_auth_profile')
def test_api_key_mode_accepts_first_party_key(self):  # auth status as recorded in Step 1 -> passes
# test_sessions.py
def test_session_launch_resolves_execution_key(self):  # api_key profile: adapter received api_key from vault; key absent from events and instructions_json
```

- [ ] **Step 3: Run** `python3 -m unittest tests.test_execution_adapter tests.test_sessions` — Expected: new tests FAIL.
- [ ] **Step 4: Implement.** `_environment()` stays the allowlist; `_run_attempt` adds `ANTHROPIC_API_KEY` only in api_key mode; `_check_auth_profile(auth_mode)` accepts the Step 1 profile for api_key and keeps today's check for cli_login. Callers in `profiles.run_test` and `attempt` resolve the key with `SecretStore.resolve(ref)` and pass it; `sessions/service.py:315` accepts both modes; `DESTINATION` auth_mode comes from the version.
- [ ] **Step 5: Run** the same command, then the full suite — Expected: OK.
- [ ] **Step 6: Commit** `feat(execution): run Claude Code with an Anthropic API key`

### Task 3: Several role assignments at once and trusted Local hosts

**Files:**
- Modify: `opendevengine/providers/service.py`, `opendevengine/providers/validation.py`, `opendevengine/api.py:194-200`
- Test: `tests/test_providers_service.py`, `tests/test_api.py` (or the file holding the model-roles API tests)

**Interfaces:**
- Produces: `ProviderService.assign_roles(assignments: dict[str, str | None]) -> dict` (all in one transaction; any failure assigns nothing). `assign_role` becomes `assign_roles({role: id})[role]`.
- Produces: `validation.set_trusted_hosts(hosts: Iterable[str])` replacing the module's trusted set; `is_local_host(host)` returns True for loopback, the `--ollama-url` IP, or any host in that set (compared lowercase, exact).
- Produces: `PATCH /api/settings/model-roles` accepts up to 4 assignments in one call.

- [ ] **Step 1: Failing tests**

```python
def test_assign_roles_is_all_or_nothing(self):  # {'ask': ok_id, 'planning': embedding_only_id} -> UnsupportedCapability; roles()['ask']['profile_id'] unchanged
def test_assign_roles_sets_several(self):       # {'ask','planning','summary'} -> all three point at the profile
def test_trusted_host_is_local(self):           # set_trusted_hosts(['100.64.0.9']); destination_for('http://100.64.0.9:1234/v1')['kind'] == 'local'
def test_trusted_hosts_are_reference_counted(self):  # callers pass the full set; set_trusted_hosts([]) -> 'remote' again
def test_patch_roles_accepts_several(self):     # API PATCH with 3 assignments -> 200
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_providers_service` (+ API test module) — Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** again, then the full suite — Expected: OK.
- [ ] **Step 5: Commit** `feat(providers): atomic multi-role assignment and trusted local hosts`

### Task 4: AIProviders façade and its API

**Files:**
- Create: `opendevengine/providers/catalog.py`, `opendevengine/ai_providers_api.py`
- Modify: `opendevengine/services.py` (construct `AIProviders` after providers and execution profiles; call `refresh_trust()` on start), `opendevengine/api.py` (route `/api/ai-providers*` and `/api/ai-jobs*` to the new API, like `ExecutionAPI`)
- Test: `tests/test_ai_providers.py`

**Interfaces:**
- Consumes: Task 1 `create(spec, managed=True)`, Task 2 api_key mode, Task 3 `assign_roles`, `set_trusted_hosts`.
- Produces `AIProviders(store, secrets, providers, execution_profiles, *, discovery)`:
  - `list() -> list[dict]` — `{id, kind, name, endpoint, auth, has_key, status}`; never the key or ref.
  - `add(kind, *, endpoint=None, api_key=None, use_login=False, consent=False) -> dict` — claude/codex need `consent is True` (else `ValueError('consent_required')`), set `local_only` False; local needs `endpoint` and adds its host to the trusted set; claude needs exactly one of `api_key` / `use_login`; codex needs `api_key`; endpoint defaults per Global Constraints.
  - `update(provider_id, *, api_key=None, endpoint=None) -> dict` — rotates the key on every linked provider profile (`update_profile(..., secret_value=)`) and creates a new execution version with a new ref (old refs revoked), which re-tests.
  - `remove(provider_id) -> None` — unassigns roles that use it, deletes linked profiles (managed execution included), recomputes trusted hosts, revokes refs.
  - `jobs() -> dict` — `{'plans': J, 'coding': J, 'search': J, 'summary': J}`, `J = {provider_id, model, status}`; status from `providers.roles()` (`ready`, `blocked` …) or, for coding, the managed execution check (`queued`, `running`, `passed`, `failed`, `blocked`, `unassigned`).
  - `choose(job, provider_id, model) -> dict` — `plans` assigns ask+planning, and summary too unless summary was chosen separately; `search` assigns embedding; `summary` assigns summary; `coding` allows only `kind == 'claude'` (`ValueError('coding_unsupported')` otherwise), creates or versions the managed execution profile with the Global Constraints defaults and `agent_model=model`, then `request_test`. Reuses a provider profile when one with the same provider and model is linked.
- Produces routes: `GET/POST /api/ai-providers`, `PATCH/DELETE /api/ai-providers/{id}`, `GET /api/ai-jobs`, `PUT /api/ai-jobs/{job}` with `{provider_id, model}` (`provider_id: null` clears). Errors map to the existing JSON `{code}` shape (`consent_required`, `coding_unsupported`, `not_found`, `validation`).

- [ ] **Step 1: Failing tests** (use the fake secrets backend, fake transport and fake execution runner the existing service tests use)

```python
def test_remote_provider_requires_consent(self):     # add('claude', api_key='k') -> consent_required; with consent -> settings()['local_only'] is False
def test_key_never_returned(self):                   # 'k-secret' not in json.dumps(list()), jobs(), API responses, job payloads table
def test_local_provider_is_trusted(self):            # add('local', endpoint='http://100.64.0.9:11434') -> its profile destination 'local', no consent needed
def test_remove_local_keeps_shared_host(self):       # two locals same host, remove one -> host still local; remove both -> remote
def test_plans_sets_ask_planning_summary(self):      # choose('plans', p, 'qwen3:8b') -> roles ask/planning/summary same profile
def test_summary_choice_survives_plans_change(self): # choose('summary', a, m1); choose('plans', b, m2) -> summary still a
def test_coding_creates_managed_profile_and_tests(self):  # choose('coding', claude, 'sonnet') -> one managed profile, api_key mode, check queued; default_profile_id() is it
def test_coding_rejects_codex_and_local(self):       # -> coding_unsupported
def test_rotate_key_updates_chat_and_coding(self):   # update(p, api_key='k2') -> chat profile new version + execution new version; old refs revoked
def test_remove_provider_clears_jobs(self):          # remove(claude) -> jobs()['coding']['status'] == 'unassigned', plan readiness has execution_not_configured
def test_privacy_back_on_blocks_remote_jobs(self):   # update_settings(local_only=True) -> jobs()['plans']['status'] == 'blocked'
```

- [ ] **Step 2: Run** `python3 -m unittest tests.test_ai_providers` — Expected: ImportError / FAIL.
- [ ] **Step 3: Implement** `catalog.py` and `ai_providers_api.py`; wire in `services.py` and `api.py`.
- [ ] **Step 4: Run** again, then the full suite — Expected: OK.
- [ ] **Step 5: Commit** `feat(providers): AI providers façade and API`

### Task 5: Settings → AI providers page

**Files:**
- Create: `opendevengine/web/settings-providers.js`
- Modify: `opendevengine/web/settings.js` (SECTIONS: replace `models` and `agent` with `["providers", "AI providers"]`), `opendevengine/web/app.js` (route regex adds `providers`; `#/settings/models` and `#/settings/agent` redirect to `#/settings/providers`; pass the existing models host and `executionUI.element` into the new section's Advanced), `opendevengine/web/styles.css` if needed, `README.md` (replace the Models / coding-agent setup steps with the new flow), `CHANGELOG.md` (Unreleased → Changed)
- Test: `tests/ui/settings-providers.test.mjs`, `tests/ui/settings.test.mjs`, `tests/test_web_assets.py` if it lists files

**Interfaces:**
- Consumes: Task 4 routes.
- Produces: `createProvidersSection({ request, advanced: { models, agent } }) -> { element, refresh() }`.

- [ ] **Step 1: Failing UI tests**

```js
test("lists providers without keys", ...)               // GET /api/ai-providers -> rows "Claude", "Local"; no input holds a key value
test("add Claude asks consent with Anthropic copy", ...) // kind Claude -> text "This lets OpenDevEngine send parts of your code to Anthropic."; cancel -> no POST
test("add Local shows trust note, no consent", ...)      // "Treated as your own machine: code is sent here without asking."
test("Plans & answers sends one PUT", ...)                // change dropdown -> PUT /api/ai-jobs/plans {provider_id, model}
test("Coding offers Claude only; Codex disabled", ...)    // option "Coding — coming later" disabled
test("coding status polls until passed", ...)             // jobs coding running -> after refresh passed -> "✓ tested"
test("blocked job shows privacy text", ...)               // status blocked -> "Blocked by privacy setting"
test("old models and agent UIs live under Advanced", ...) // <details> contains passed elements
// settings.test.mjs
test("settings nav has AI providers, not Models/Coding agent", ...)
```

- [ ] **Step 2: Run** `OPENDEVENGINE_TEST_PYTHON="$(command -v python3)" node --test tests/ui/settings-providers.test.mjs tests/ui/settings.test.mjs` — Expected: FAIL.
- [ ] **Step 3: Implement** the section per the spec's screen sketch; fields per kind as in the spec; save then `refresh()`; poll jobs every 2 s while coding status is `queued` or `running` and the page is visible.
- [ ] **Step 4: Run** the UI suite and `python3 -m unittest tests.test_web_assets` — Expected: pass. Then the full Python suite.
- [ ] **Step 5: Manual check** against the running app: add Local (ryzen-ai URL, qwen3:8b) and pick it for Plans & answers; generate a plan in Ask.
- [ ] **Step 6: Commit** `feat(web): Settings → AI providers` and `docs: AI providers setup`

## Plan decisions (beyond the spec)

- Existing users: providers are not derived from old profiles; old profiles stay visible and editable under Advanced through the unchanged Models and Coding agent UIs. Spec said "shows providers it can derive"; this is the cheaper subset of "anything it can't map appears under Advanced". Cost if wrong: existing users re-add providers once.
- Provider grouping lives in a new `ai_providers` table plus a link table (spec left table vs settings key to the builder).
