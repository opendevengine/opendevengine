# Contributing to OpenDevEngine

Thanks for helping. OpenDevEngine is a single-user, self-hosted workspace: it runs on your
computer, binds to `127.0.0.1`, and keeps credentials in your operating system's vault.
Contributions should preserve those boundaries.

## Development setup

Requirements:

- Python 3.10 or later, built with SQLite FTS5 (the python.org and Homebrew builds are)
- Node.js 20 or later (UI tests only; the app itself needs no Node build step)
- Git
- Optional: Docker and the Claude Code CLI, to exercise implementation sessions end to end

```sh
git clone https://github.com/opendevengine/opendevengine.git opendevengine
cd opendevengine
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[full]'
```

Run the app against the bundled sample repositories (in-memory, nothing is saved):

```sh
opendevengine
# or, without the optional packages
python3 -m opendevengine
```

Use `--data-dir <path>` to persist state. Never point a development build at a data
directory that holds credentials you care about.

## Running the tests

```sh
python -m unittest discover -s tests
OPENDEVENGINE_TEST_PYTHON="$(command -v python)" node --test tests/ui/*.test.mjs
```

- The Python suite covers storage, jobs, GitHub, indexing, Ask, plans, sessions and
  publication. Tests that need Docker, a GitHub account or a model provider use
  synthetic fakes; nothing in the default suite talks to the network.
- The UI suite runs the static web app in a DOM shim. Some UI tests start a small
  Python bridge, which is why `OPENDEVENGINE_TEST_PYTHON` must point at the interpreter with
  the project installed.
- Vector tests need the `vectors` extra (`apsw`, `sqlite-vec`).

CI runs both suites on every pull request.

## Making a change

1. Open an issue first for anything larger than a small fix, so scope can be agreed.
2. Keep pull requests focused on one behavior change. Include tests that exercise the
   behavior (state changes, recovery, permission boundaries), not tests that mirror the
   implementation.
3. Match the surrounding code: standard library first, no new runtime dependency without
   discussion, no frontend build step. Static assets live in `opendevengine/web/`.
4. UI changes must keep keyboard navigation, visible focus, 44px touch targets at narrow
   widths, and dark/light contrast. Attach before/after screenshots at 390px and desktop
   widths.
5. Update `README.md` and the relevant page under `site/docs/` when user-visible
   behavior changes, and add a line to `CHANGELOG.md`.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat(web): …`, `fix(sessions): …`, `docs: …`).

## Safety boundaries contributors must keep

- Ask never executes repository code or changes files.
- Credentials are stored only through the OS vault; there is no plaintext fallback.
- Repository source leaves the machine only when the user has turned off local-only
  mode and opted the relevant profile in.
- Sessions run in containers without network access and never pull images.
- GitHub writes go only to new `opendevengine/…` branches as draft pull requests, only for
  repositories with **Allow changes** on. Nothing merges or touches a default branch.

If a change needs to relax one of these, open an issue to discuss it first.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE) and that you will follow the [Code of Conduct](CODE_OF_CONDUCT.md).
