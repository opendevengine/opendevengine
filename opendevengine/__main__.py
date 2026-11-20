"""Run the local OpenDevEngine web app."""

from argparse import ArgumentParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socketserver
import json
from pathlib import Path
import re
import signal
from subprocess import SubprocessError
from urllib.parse import parse_qs, urlsplit

from .answer import Ollama, respond
from .index import CodeIndex, MAX_REPOS, load_demos, load_git
from .indexing import IndexService
from .ask import RoutingService, PlanService, ConversationService
from .platform import Platform
from .platform.clock import now_ms
from .platform.errors import PlatformError
from .platform.workspace import apply_github_status, build_workspace_summary, build_health, apply_service_health
from .api import error_status
from .services import ApplicationServices
from .platform.keyring_backend import OSKeyringBackend


ROOT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/context.js": ("context.js", "text/javascript; charset=utf-8"),
    "/ask.js": ("ask.js", "text/javascript; charset=utf-8"),
    "/sessions.js": ("sessions.js", "text/javascript; charset=utf-8"),
    "/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
    "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/repo-source.js": ("repo-source.js", "text/javascript; charset=utf-8"),
    "/picker.js": ("picker.js", "text/javascript; charset=utf-8"),
    "/mentions.js": ("mentions.js", "text/javascript; charset=utf-8"),
    "/composer.js": ("composer.js", "text/javascript; charset=utf-8"),
    "/thread.js": ("thread.js", "text/javascript; charset=utf-8"),
    "/plan-view.js": ("plan-view.js", "text/javascript; charset=utf-8"),
    "/code-viewer.js": ("code-viewer.js", "text/javascript; charset=utf-8"),
    "/repositories.js": ("repositories.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/settings-models.js": ("settings-models.js", "text/javascript; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Bind without HTTPServer's reverse-DNS getfqdn() lookup, which can stall startup for
    seconds on hosts with slow DNS; a loopback-only server never needs its FQDN."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "OpenDevEngine"
    sys_version = ""

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, format, *args):
        # Source filenames and question text stay out of request logs.
        return

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def legacy_allowed(self, name, path=None):
        services = getattr(self.server, "services", None)
        return services is None or services.legacy_source_allowed(name, path)

    def send_data(self, status, content, body, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com",
        )
        for name, value in (extra_headers or {}).items():
            if name in {"Location", "Retry-After"} and "\r" not in str(value) and "\n" not in str(value):
                self.send_header(name, str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status, value, headers=None):
        self.send_data(status, "application/json; charset=utf-8", json.dumps(value).encode("utf-8"), headers)

    def do_GET(self):
        if not self.allowed_host():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Use the local URL printed by OpenDevEngine."})
            return
        url = urlsplit(self.path)
        if url.path in ASSETS:
            filename, mime = ASSETS[url.path]
            self.send_data(HTTPStatus.OK, mime, (ROOT / "web" / filename).read_bytes())
        elif url.path == "/api/repos":
            self.send_json(HTTPStatus.OK, {
                "repos": [repo for repo in self.server.index.list_repos() if self.legacy_allowed(repo["name"])],
                "model": self.server.model.model if self.server.model else None,
            })
        elif url.path in {"/api/workspace", "/api/health"}:
            if url.query:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "This status route takes no query parameters.", "code": "bad_request"})
                return
            builder = build_workspace_summary if url.path == "/api/workspace" else build_health
            try:
                result = builder(self.server.index, self.server.model,
                                 getattr(self.server, "platform", None), now=now_ms())
                services = getattr(self.server, "services", None)
                if services is not None:
                    if url.path == "/api/workspace":
                        result["contract"] = "opendevengine.workspace/m1.1"
                        apply_github_status(result, services.github.status_summary())
                        result["providers"]["profiles"] = {"status": "available", **services.providers.status_summary()}
                        result["navigation"][1]["status"] = "available"
                        result["navigation"][2]["status"] = "available"
                        result["execution"] = services.execution_status()
                        if services.sessions is not None:
                            result["navigation"].append({"id": "sessions", "label": "Sessions",
                                                         "href": "#/sessions", "status": "available"})
                    else:
                        apply_service_health(result, services)
                        result["components"]["jobs"]["workers_running"] = int(services.running)
                        result["components"]["jobs"]["error_code"] = services.error_code
                        if services.error_code:
                            result["status"] = "degraded"
            except PlatformError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Workspace status is temporarily unavailable.", "code": error.code})
            except Exception:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Workspace status is unavailable.", "code": "internal"})
            else:
                self.send_json(HTTPStatus.OK, result)
        elif url.path == "/api/source":
            self.send_source(url.query)
        else:
            self.feature_request("GET", host_checked=True)

    def send_source(self, query):
        try:
            params = parse_qs(query, max_num_fields=5, strict_parsing=True)
            names = ("repo", "revision", "path", "start", "end")
            if set(params) != set(names) or any(len(params[name]) != 1 for name in names):
                raise ValueError
            repo, revision, path, start, end = (params[name][0] for name in names)
            start, end = int(start), int(end)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid source request."})
            return
        if not self.legacy_allowed(repo, path):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "This source is unavailable."})
            return
        try:
            source = self.server.index.source(repo, revision, path, start, end)
        except KeyError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "This file is unavailable in the current index."})
        except ValueError as error:
            self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
        else:
            self.send_json(HTTPStatus.OK, source)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.allowed_host() or (origin and origin != f"http://{self.headers['Host']}"):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "This request must come from OpenDevEngine."})
            return
        if self.path != "/api/ask":
            self.feature_request("POST", host_checked=True)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send an application/json request."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 32 * 1024:
                raise ValueError
            request = json.loads(self.rfile.read(length))
            question, previous, repos = self.validate_question(request)
        except (TimeoutError, ValueError, TypeError, KeyError, RecursionError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Enter a question and select valid repositories."})
            return
        response = respond(self.server.index, self.server.model, repos, question, previous, allowed_path=self.legacy_allowed)
        if any(not self.legacy_allowed(repo) for repo in repos) or any(not self.legacy_allowed(c["repo"], c["path"]) for c in response["sources"]):
            self.send_json(HTTPStatus.CONFLICT, {"error": "Source access changed. Reload repository selection.", "code": "repository_unavailable"})
            return
        self.send_json(HTTPStatus.OK, response)

    def do_PATCH(self):
        self.feature_request("PATCH")

    def do_DELETE(self):
        self.feature_request("DELETE")

    def feature_request(self, method, *, host_checked=False):
        origin = self.headers.get("Origin")
        if not host_checked and (not self.allowed_host() or
                (method != "GET" and origin and origin != f"http://{self.headers['Host']}")):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Use this workspace's local origin.", "code": "forbidden_host"})
            return
        features = getattr(self.server, "features", None)
        if features is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
            return
        try:
            body = None
            if method != "GET":
                if self.headers.get_content_type() != "application/json":
                    self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send JSON.", "code": "unsupported_media_type"})
                    return
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 64 * 1024 or (length == 0 and method != "DELETE"):
                    raise ValueError()
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError()
            url = urlsplit(self.path)
            result = features.dispatch(method, url.path, url.query, body)
            if result is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
            else:
                self.send_json(result.status, result.body, result.headers)
        except PlatformError as error:
            code = error.code if isinstance(error.code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", error.code) else "internal"
            status = getattr(error, "http_status", error_status(code))
            if type(status) is not int or status not in {400,404,409,429,500,502,503,504}:
                status = 500
            payload = {"error": "The operation could not complete. Check setup and retry.", "code": code}
            retry_at = getattr(error, "retry_at", None)
            if type(retry_at) is int and 0 <= retry_at < 2**63:
                payload["detail"] = {"retry_at": retry_at}
            self.send_json(status, payload)
        except (TimeoutError, ValueError, TypeError, KeyError, RecursionError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request.", "code": "bad_request"})
        except Exception:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "The operation could not complete.", "code": "internal"})

    def validate_question(self, request):
        question = request["question"]
        previous = request.get("previous", "")
        repos = request["repos"]
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
            raise ValueError
        if not isinstance(previous, str) or len(previous) > 2000:
            raise ValueError
        if not isinstance(repos, list) or not 1 <= len(repos) <= MAX_REPOS:
            raise ValueError
        if any(not isinstance(repo, str) or repo not in self.server.index.snapshots or not self.legacy_allowed(repo) for repo in repos):
            raise ValueError
        return question.strip(), previous.strip(), list(dict.fromkeys(repos))


def main():
    parser = ArgumentParser(description="Explore local Git repositories with clickable source citations.")
    parser.add_argument("--repo", metavar="NAME=PATH", action="append", default=[],
                        help="Index HEAD of a local Git checkout. Repeat to add repositories.")
    parser.add_argument("--model", metavar="NAME", help="Seed unassigned Ask/planning roles with this local Ollama model.")
    parser.add_argument("--ollama-port", type=int, default=11434, help="Loopback port for Ollama.")
    parser.add_argument("--data-dir", type=Path, help="Persist workspace metadata in this directory; omitted uses memory.")
    parser.add_argument("--port", type=int, default=8765, help="Loopback port for the web app.")
    args = parser.parse_args()
    if len(args.repo) > MAX_REPOS:
        parser.error(f"Up to {MAX_REPOS} repositories are supported.")
    if not (1 <= args.port <= 65535 and 1 <= args.ollama_port <= 65535):
        parser.error("Ports must be between 1 and 65535.")

    snapshots = []
    local_roots = {}
    names = set()
    for spec in args.repo:
        name, separator, directory = spec.partition("=")
        if not separator or not directory or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            parser.error("Use --repo NAME=PATH with letters, numbers, dots, underscores, or hyphens in NAME.")
        if name in names:
            parser.error(f"Repository name {name} is already in use.")
        try:
            snapshot = load_git(name, Path(directory))
        except (OSError, ValueError, SubprocessError) as error:
            detail = str(error) if isinstance(error, ValueError) else "Check the path, Git installation, and committed HEAD."
            parser.error(f"Could not index {name}. {detail}")
        snapshots.append(snapshot)
        local_roots[name] = Path(directory).expanduser().resolve()
        names.add(name)
    if not snapshots:
        snapshots = load_demos(ROOT / "examples")

    index = CodeIndex([])
    model = Ollama(args.model, args.ollama_port) if args.model else None
    try:
        platform = Platform.open(args.data_dir.expanduser() if args.data_dir else None,
                                 secret_backend=OSKeyringBackend() if args.data_dir else None)
    except (PlatformError, RuntimeError, ValueError, OSError):
        index.db.close()
        parser.exit(1, "Could not open workspace storage. Check the directory and database compatibility.\n")
    try:
        server = LoopbackHTTPServer(("127.0.0.1", args.port), RequestHandler)
    except OSError as error:
        platform.close()
        index.db.close()
        parser.exit(1, f"Could not start OpenDevEngine on port {args.port}. {error.strerror}\n")
    services = ApplicationServices(platform, public_base_url=f"http://127.0.0.1:{args.port}")
    try:
        with server:
            indexing = IndexService(platform.store, platform.jobs, services.github, services.providers)
            services.enable_context(indexing)
            if args.model:
                roles = services.providers.roles()
                missing_roles = [role for role in ('ask','planning') if not roles.get(role,{}).get('profile_id')]
                if missing_roles:
                    profile = services.providers.create_profile({
                        'name': 'CLI local Ollama', 'adapter': 'ollama',
                        'endpoint': f'http://127.0.0.1:{args.ollama_port}', 'model': args.model,
                        'config': {'capabilities': ['chat','structured_output'], 'timeout_seconds': 600}})
                    for role in missing_roles:
                        services.providers.assign_role(role, profile['id'])
            services.legacy_index = index
            services.bootstrap_context(snapshots, local_roots)
            snapshots.clear()
            snapshot = None  # Do not retain a purged startup source as an in-memory restore cache.
            routing = RoutingService(platform.store, indexing)
            plans = PlanService(platform.store, indexing, services.providers)
            conversations = ConversationService(platform.store, platform.jobs, indexing, services.providers, routing, plans)
            services.enable_ask(routing, plans, conversations)
            services.enable_execution(data_dir=args.data_dir.expanduser() if args.data_dir else None)
            server.index = index
            server.platform = platform
            server.model = model
            server.services = services
            server.features = services.api
            services.start()
            count = len(services.legacy_sources)
            print(f"Registered {count} {'repository' if count == 1 else 'repositories'}. Open http://127.0.0.1:{args.port}", flush=True)
            def stop_server(_signum, _frame):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_server)
            signal.signal(signal.SIGINT, stop_server)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        services.close()
        platform.close()
        index.db.close()

if __name__ == "__main__":
    main()
