# AI providers settings

## Why

Setting up a coding session today needs two unrelated setups: a **model profile** under
Settings → Models (answers, plans, summaries, search) and a separate **execution profile** under
Settings → Coding agent (Claude Code CLI login only, container image, limits, agent model), which
must then pass a synthetic coding test. The user found this confusing while testing locally and
asked to keep it simple: one place where they add the AI providers they have, then choose which
provider does which job.

## What the user decided

- One **AI providers** section in Settings, replacing the Models and Coding agent sections.
- Provider kinds: **Claude**, **Codex** (OpenAI), and **Local models**. Configuration accepts an
  API key and/or an API URL.
- **Per-job choice**: the user picks a provider per job, e.g. "Plans & answers: Local qwen3:8b,
  Coding: Claude".
- Approach A (my pick, the user handed me the how): keep the existing internals (provider
  profiles, execution profiles, immutable versions, synthetic test, policies) and build the new
  page as a façade that creates and manages them.

## Goals

- A new user can go from nothing to "ready to start a session" by adding providers and choosing
  two dropdowns, without seeing the words "profile", "execution", "image" or "adapter".
- Claude works with either an Anthropic **API key** or the existing `claude` CLI login.
- Local models: Ollama URL (default and `--ollama-url` value prefilled) plus a model from the
  discovered list; any OpenAI-compatible URL (LM Studio, vLLM, llama.cpp) is also "Local / custom".
- Codex: an OpenAI API key (URL defaults to `https://api.openai.com/v1`, editable) for plans and
  answers.
- The coding setup (container image, limits, test run) happens automatically when the user picks
  a coding provider, with progress and a plain-language result.

## Non-goals

- **Codex as the coding agent.** It needs a new agent integration (CLI adapter, tool broker
  wiring, containment, auth checks). It is phase 2 and gets its own design. In phase 1 the Coding
  dropdown offers Claude only; Codex appears there as "Coding — coming later" (disabled).
- Local models as the coding agent (no agent CLI to drive them).
- Merging provider and execution profile tables. (One small migration is needed: see
  "Builder's calls".)
- Changing session, validation, publication or containment behaviour.

## Anti-goals

- Weakening privacy: picking a remote provider must never silently send source. The existing
  consent model stays; the new page asks for it in place instead of sending the user to Privacy.
- Showing secrets: keys go to the OS vault as today, never back to the browser or into logs.

## Design

### Screen

```
AI providers                                   [+ Add provider]
  Claude        API key ••••abcd            ✓ ready      Edit · Remove
  Local         http://100.102.234.29:11434 qwen3:8b ✓   Edit · Remove

What each job uses
  Plans & answers   [Local · qwen3:8b ▾]
  Coding            [Claude ▾]          ✓ tested 2 min ago   [Test again]
  Code search       [Local · nomic-embed-text ▾]   (optional)
  ▸ Advanced: repository summaries, container image, limits
```

- **Add provider** asks the kind first (Claude / Codex / Local), then only that kind's fields:
  - Claude: "API key" *or* "Use my `claude` login (Pro/Max)", model (default `sonnet`, editable).
  - Codex: API key, model (default from a short list), API URL under "More".
  - Local: URL (prefilled), model picker from discovery; free-text model for non-Ollama URLs.
- Saving tests the connection with synthetic content (existing `test_profile`) and shows ✓ or the
  existing plain-language error.
- **Plans & answers** sets both the `ask` and `planning` roles. Summary follows it unless changed
  under Advanced. Code search maps to the `embedding` role and lists only embedding-capable models.

### Mapping onto the existing internals

- A **provider** in the UI is a stored grouping: one record per provider (kind, display name,
  vault ref for the key, URL, chosen models). Each job choice creates or reuses the provider
  profile it needs (`adapter`: `ollama`, `openai_compatible`, or `claude_cli`) and assigns the
  role, as `useDiscoveredModel` already does for Ollama.
  - Claude with API key → `openai_compatible` profile against Anthropic's OpenAI-compatible
    endpoint (`https://api.anthropic.com/v1`) for chat jobs. If planning's structured output does
    not work there, it falls back to the existing no-structured-output path.
  - Claude with login → existing `claude_cli` profile.
  - Codex → `openai_compatible` profile at the OpenAI URL.
- **Coding = Claude** creates (or reuses) one execution profile with defaults: image
  `python:3.12-slim`, default runner and agent limits, the Claude model chosen on the provider, and
  runs its synthetic test automatically. Changing the Claude model or key creates a new execution
  profile version and re-tests. The result shows next to the Coding dropdown.
- **Claude API key for coding** is new: execution profiles gain `auth_mode: 'api_key'`. The Claude
  adapter passes the key from the vault as `ANTHROPIC_API_KEY` to the CLI process only, and the
  auth check accepts the API-key auth profile in that mode (still rejects third-party
  `apiProvider` values). `cli_login` stays as it is.
- Plans pick up the coding profile automatically through the existing `default_profile_id` rule
  (one passing profile). The façade keeps exactly one active façade-managed execution profile, so
  that rule keeps working; older hand-made profiles stay usable but are not shown.

### Privacy and consent

- **User decision: ask once, when adding.** Adding a Claude or Codex provider shows one
  confirmation: "This lets OpenDevEngine send parts of your code to Anthropic/OpenAI." Confirming
  turns off "Keep everything on this computer" and the façade sets `allow_external_source` on every
  profile it creates for that provider. Cancelling does not save the provider. Later job choices
  do not ask again.
- If the user later turns "Keep everything on this computer" back on in Settings → Privacy, the
  remote job choices are blocked as today, and the page shows them as "Blocked by privacy setting".
- **User decision: trust what you add as Local.** Any URL saved as a Local provider is treated as
  the user's own machine, the same way `--ollama-url` is today: no consent prompt and no blocking
  by local-only. The backend keeps a list of trusted local hosts (the `--ollama-url` host plus the
  hosts of saved Local providers); removing the provider removes its host. The Local form says
  "Treated as your own machine: code is sent here without asking."

### Existing users

- Profiles created before this change keep working. The new page shows providers it can derive
  from existing profiles (grouped by adapter + endpoint); anything it can't map appears under
  Advanced as "Other configured models" with the old controls.
- The `#/settings/models` and `#/settings/agent` links redirect to `#/settings/providers`.

## Builder's calls (settled after the builder check; marked as my calls)

- **api_key migration.** `execution_profile_versions.auth_mode` has `CHECK(auth_mode='cli_login')`.
  A migration rebuilds that table to allow `'api_key'`; `_validate` and the Claude adapter accept it.
- **Which coding profile plans use.** The façade marks its execution profile as managed;
  `default_profile_id` prefers the managed profile when it has passed, and falls back to today's
  "exactly one passing profile" rule otherwise.
- **Where the Claude key lives.** The coding profile gets its own vault ref (execution profile
  versions gain a nullable secret ref). The façade writes the key to both the chat profile and the
  coding profile and rotates or removes both together, so removing one job never breaks the other.
- **API-key auth check.** In `api_key` mode the adapter sets `ANTHROPIC_API_KEY` and requires
  `claude auth status` to report first-party API-key auth; `cli_login` mode keeps today's check
  and must not pass with a key in the environment. The first plan task records the CLI's exact
  output for both modes before coding against it.
- **Several jobs in one save.** `PATCH /api/settings/model-roles` accepts several assignments and
  applies them in one transaction (all or none), so "Plans & answers" sets ask, planning and summary
  together.

## Left to the builder

- Exact copy, layout and component split inside `settings-models.js` / a new file.
- Where the provider grouping record lives (a small new table vs. a settings JSON key), within
  "no migration of existing tables".
- Default model lists for Claude and Codex.
- How the automatic coding test reports progress (poll vs. existing events).
