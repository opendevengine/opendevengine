# Security policy

## Supported versions

OpenDevEngine is pre-1.0. Security fixes are made on the latest release and the default
branch only.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository (GitHub private vulnerability
reporting). Do not open a public issue, discussion or pull request for an unfixed issue.

Include the version or commit, your operating system, steps to reproduce, and the
impact you observed. You should get an acknowledgement within 7 days. We will agree a
disclosure date with you once a fix is available.

## Security model

OpenDevEngine is designed for **one user on their own computer**. Reports are most useful
when they show a way around one of these boundaries:

| Boundary | Intended behavior |
| --- | --- |
| Network exposure | The server binds to `127.0.0.1` only and checks request origin on state-changing calls. It has no user authentication and is **not** safe to expose on a network or the internet. |
| Credentials | GitHub App keys and provider API keys are stored only in the OS credential vault (macOS Keychain, Linux Secret Service, Windows Credential Manager). There is no plaintext fallback; credentials are referenced by ID and redacted from logs, events and diagnostics. |
| Source privacy | Local-only mode is on by default. Repository source is sent to a remote model only after the user turns local-only mode off and opts the specific provider or execution profile in. |
| Ask | Ask reads indexed snapshots and calls configured models. It never runs repository commands or edits files. |
| Indexing | Indexing reads committed files only and never runs repository install or build scripts. Credential-like files and generated artifacts are excluded. |
| Sessions | Repository files and commands for a session live in a container with no network (`--network none`), all capabilities dropped, a read-only root filesystem, memory/CPU/process limits and timeouts, using an image already present locally (never pulled). The coding-agent CLI (Claude Code) runs **on the host**, in an empty private temporary directory, with its built-in tools, hooks, plugins, settings, memory and slash commands disabled; it can act only through OpenDevEngine's brokered tools, which operate inside that container. The CLI needs network access to its model provider and uses the user's own login, with turn, budget and time limits. Stopping a session confirms the container and agent processes are gone, or reports a containment failure. |
| GitHub writes | Only for repositories with **Allow changes** on, only to new `opendevengine/…` branches, and only as draft pull requests after explicit publication. OpenDevEngine never merges and never pushes to a default branch. |
| Permissions | Repository content and model output cannot widen a plan's scope, a session's grants or a repository's permissions. (User-managed skills, plugins and memory are not available yet.) |

Out of scope: attacks that already require control of the user's account, operating
system or credential vault; running OpenDevEngine on a shared or network-exposed host;
and vulnerabilities in Docker, the coding-agent CLI or model providers themselves (please
report those upstream, though we welcome a heads-up).

## What a session can see

The coding agent receives the saved plan and the repository source permitted by the
plan, and its CLI runs on the host with the credentials the user logged in with (for
example Claude Code). Its access to the repository copy goes through OpenDevEngine's tools into
the container. Treat a session like giving that agent read/write access to a copy of the
repository: review the diff before publishing a pull request.
