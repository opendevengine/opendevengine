# Third-party notices

OpenDevEngine itself is released under the [MIT License](LICENSE). The core application uses
only the Python standard library. The optional extras below install third-party
packages, each under its own license. Versions are those in `requirements-dev.lock`
at the time of writing; check each project for its current terms.

| Package | Installed by extra | License |
| --- | --- | --- |
| [keyring](https://github.com/jaraco/keyring) | `credentials`, `full` | MIT |
| [jaraco.classes](https://github.com/jaraco/jaraco.classes), [jaraco.context](https://github.com/jaraco/jaraco.context), [jaraco.functools](https://github.com/jaraco/jaraco.functools) | via keyring | MIT |
| [more-itertools](https://github.com/more-itertools/more-itertools) | via keyring | MIT |
| [cryptography](https://github.com/pyca/cryptography) | `github`, `full` | Apache-2.0 OR BSD-3-Clause |
| [cffi](https://github.com/python-cffi/cffi) | via cryptography | MIT-0 |
| [pycparser](https://github.com/eliben/pycparser) | via cffi | BSD-3-Clause |
| [sqlite-vec](https://github.com/asg017/sqlite-vec) | `vectors`, `full` | MIT OR Apache-2.0 |
| [APSW](https://github.com/rogerbinns/apsw) | `vectors`, `full` | zlib-style (OSI approved, "any-OSI") |

Platform-specific keyring backends (for example `SecretStorage` and `jeepney` on Linux,
`pywin32-ctypes` on Windows) are pulled in automatically on those systems and are also
permissively licensed.

## Not distributed with OpenDevEngine

OpenDevEngine talks to these tools but does not bundle them. You install them yourself and
they are governed by their own terms:

- Docker (container runtime for implementation sessions)
- Claude Code CLI (the coding agent used in sessions) and Anthropic's terms of service
- Ollama or any OpenAI-compatible model server you configure
- GitHub, through a GitHub App you create on your own account

## Bundled sample repositories

`opendevengine/examples/` contains small sample repositories written for this project and
covered by its MIT License.
