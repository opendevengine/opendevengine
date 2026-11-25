# Changelog

All notable user-visible changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may change behavior).

## [Unreleased]

### Fixed
- Provider creation preserves privacy preferences and cleans up staged credentials on failure.
- API-key rotation updates linked model and execution profiles atomically.
- Provider deletion retires linked profiles, credentials and queued checks atomically.
- Running coding checks cannot be invalidated by credential rotation or provider deletion.
- Public endpoints cannot acquire local-only trust through Local provider registration
  or previously saved configuration.

### Changed
- **Settings → AI providers** replaces Models and Coding agent: add Claude (API key or
  `claude` login), Codex (OpenAI API key) or Local models (Ollama or OpenAI-compatible URL),
  then choose a provider for Plans & answers, Coding and Code search. Choosing Claude for
  Coding sets up and tests the coding environment automatically. Adding Claude or Codex asks
  once to allow sending code; Local providers require localhost or a private LAN/VPN
  IP address. Older profiles stay under Advanced.
- **Validation checks need your approval:** a check runs only if you ticked "Run this check"
  in the plan. Checks from the model start unapproved; unapproved checks are skipped and
  shown as skipped, and a session with skipped checks is never published automatically.
  Saved plans need a new version with checks approved. (#9)

### Added
- The coding agent can run Claude Code with an Anthropic API key, kept in your OS vault and
  passed only to the Claude Code process.

## [0.1.0] — 2026-11-21, first public preview

### Added
- **Workspace foundation:** durable SQLite storage with migrations, a recoverable job
  queue, OS-vault secret references and a loopback-only web app with host and origin checks.
- **GitHub:** one-click private GitHub App creation from a manifest, installation discovery,
  a paginated repository catalog designed for hundreds of repositories, and per-repository
  **Use in Ask** and **Allow changes** switches.
- **Indexing and retrieval:** persistent snapshots of committed source, full-text search,
  optional vector search (sqlite-vec), credential and generated-file exclusion, bounded
  retries and restart recovery.
- **Ask and plans:** repository routing with reasons, cited answers, saved conversations,
  and editable, immutable-versioned implementation plans with launch validation.
- **Models:** Ollama, OpenAI-compatible endpoints and the Claude Code CLI as providers,
  assigned per role, with local-only mode on by default and explicit per-profile opt-in for
  sending source.
- **Implementation sessions:** the Claude Code CLI on the host with its own tools disabled,
  acting only through OpenDevEngine's brokered tools on a repository copy in a network-less,
  capability-dropped, resource-limited container; live progress, guidance messages, diff,
  validation checks, stop with containment confirmation, and interrupted/retry recovery.
- **Draft pull requests:** reviewed publication to new `opendevengine/…` branches as draft PRs,
  generated-file omission, and reconciliation that never duplicates a branch or PR.
- **Interface:** Ask, Sessions, Repositories and Settings in a responsive dark/light
  workspace with keyboard navigation, visible focus and 44px touch targets on small screens.
- **Ollama on another machine:** `--ollama-url` (or `OPENDEVENGINE_OLLAMA_URL`) points
  discovery, `--model` and new Ollama profiles at another computer you own, such as a GPU
  box on Tailscale. Its private-network IP is treated as local, including in local-only
  mode; public IPs and host names are refused.
- Open-source project files: contributing guide, security policy, code of conduct,
  third-party notices, issue and pull request templates, and CI for Python 3.10–3.13 on
  Linux and macOS plus UI tests and a package build check.
- **Delete session content:** a finished session's events, guidance, diffs, checkpoints and
  validation output can be deleted; its identity, base commit and pull request link remain.
- **Base branch freshness:** starting or retrying a session whose base branch moved since the
  plan was made asks whether to start on the saved base or regenerate the plan.
- Execution profiles can pin a Claude model (alias or id) for the coding agent; blank keeps the
  Claude Code default. The model is part of the immutable profile version and shown on sessions.
- System health shows the saved index by state, the models assigned to Ask and planning, and
  the session runtime, using status codes only.
- Answer runs report whether they were a question or a plan request, so a failed plan says so;
  model timeouts suggest a smaller or GPU-backed model.
- A static product site and user manual in `site/`, with a GitHub Pages workflow that deploys
  only from `main` or by manual dispatch.
- Indexing also pauses before the data volume would drop below 1 GiB free, and the
  Repositories page explains how to recover from a disk-limit pause.

### Changed
- `--ollama-port` is replaced by `--ollama-url`.

### Fixed
- Ask keeps a question that is waiting for a repository choice across reloads and restarts,
  shows why each repository was suggested, and never resends a question once repositories
  were chosen.
- Credential detection is shared by indexing, session logs and exports, and now also catches
  `sk-…`, `github_pat_…`, Slack and Google API key formats before source is indexed.
- The route bar stays on one line when the source panel is open.
- The index disk cap (512 MiB, counted over the whole database) stopped indexing at about
  40,000 chunks. It is now 4 GiB of live database pages, so a deleted index frees room, and
  runs paused under the old default continue with the new one.
- Repository suggestions are faster on large indexes: at 100,000 chunks, p95 routing time
  dropped from 1.4 s to 0.94 s.
- Startup no longer waits for an unresponsive Docker daemon (up to 30 s) or for a reverse
  DNS lookup (8 s+ on some hosts) before serving the web app.
- A contained profile test left by a crashed process is resolved at startup instead of
  staying "running" until its lease expires.
- Quick-setup local Ollama chat profiles use a 600 s limit instead of 180 s.

### Known limitations
- Single user on one computer; not safe to expose on a network.
- One writable repository per session; cross-repository execution is planned.
- Skills, plugins/MCP tools and memory are not available yet.
- Claude Code is the only coding-agent adapter.
