"""Read-only indexing and retrieval for local repository snapshots."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
from threading import Lock


MAX_REPOS = 8
MAX_FILES = 4_000
MAX_REPO_BYTES = 12 * 1024 * 1024
MAX_FILE_BYTES = 96 * 1024
EXCERPT_LINES = 12
MAX_EXCERPT_CHARS = 1_400
SOURCE_TYPES = set("""
    c cc cpp cs css go h hpp html java js json jsx kt kts md mjs mts php
    properties proto py rb rs sh sql swift toml ts tsx xml yaml yml
""".split())
SOURCE_NAMES = {"dockerfile", "makefile", "readme"}
SKIP_PARTS = {"build", "dist", "node_modules", "target", "vendor"}
STOP_WORDS = set("""
    a an and are as at be by can could did do does for from get had has have
    how i in is it me of on or our should that the their them then there these
    they this to us was we were what when where which who why will with would you
""".split())


def terms(text):
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return re.findall(r"[^\W_]+", text.casefold())


def code_lines(content):
    text = content.decode("utf-8")
    return tuple(text.replace("\r\n", "\n").removesuffix("\n").split("\n"))


def indexable(path):
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if any(part.startswith(".") or part.casefold() in SKIP_PARTS for part in parts):
        return False
    name = parts[-1].casefold()
    if re.search(r"(?:^|[._-])(?:credentials?|secrets?|lock|bundle|min)(?:[._-]|$)", name):
        return False
    return name in SOURCE_NAMES or PurePosixPath(name).suffix[1:] in SOURCE_TYPES


@dataclass(frozen=True)
class Snapshot:
    name: str
    revision: str
    files: dict[str, tuple[str, ...]]
    demo: bool = False
    limited: bool = False


@dataclass(frozen=True)
class Evidence:
    repo: str
    revision: str
    path: str
    start: int
    end: int
    code: str

    def citation(self):
        return {
            "repo": self.repo, "revision": self.revision, "path": self.path,
            "start": self.start, "end": self.end,
        }


def git(root, *args, data=None):
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], input=data, capture_output=True,
        check=True, timeout=45, env=env,
    ).stdout


def load_git(name, directory):
    root = directory.expanduser().resolve(strict=True)
    if Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").removesuffix(b"\n"))).resolve() != root:
        raise ValueError(f"{directory} must be the root of a Git checkout")
    revision = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    selected = []
    total_bytes = 0
    limited = False
    for entry in git(root, "ls-tree", "-r", "-z", "-l", revision).split(b"\0"):
        if not entry:
            continue
        meta, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id, size = meta.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not indexable(path):
            continue
        size = int(size)
        if size > MAX_FILE_BYTES or total_bytes + size > MAX_REPO_BYTES or len(selected) >= MAX_FILES:
            limited = True
            continue
        selected.append((path, object_id))
        total_bytes += size

    files = {}
    if selected:
        requests = b"".join(object_id + b"\n" for _, object_id in selected)
        blobs = BytesIO(git(root, "cat-file", "--batch", data=requests))
        for path, expected_id in selected:
            header = blobs.readline().split()
            if len(header) != 3 or header[:2] != [expected_id, b"blob"]:
                raise ValueError(f"Git could not read a committed blob in {name}")
            content = blobs.read(int(header[2]))
            if blobs.read(1) != b"\n":
                raise ValueError(f"Git returned an incomplete blob in {name}")
            if b"\0" not in content:
                try:
                    files[path] = code_lines(content)
                except UnicodeDecodeError:
                    continue
    return Snapshot(name, revision, files, limited=limited)


def load_demos(root):
    snapshots = []
    for name in ("storefront-web", "orders-api", "orders-db", "notifications-worker"):
        directory = root / name
        digest = sha256()
        files = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative_path = path.relative_to(directory).as_posix()
                content = path.read_bytes()
                files[relative_path] = code_lines(content)
                digest.update(relative_path.encode("utf-8") + b"\0" + content + b"\0")
        snapshots.append(Snapshot(name, digest.hexdigest(), files, demo=True))
    return snapshots


class CodeIndex:
    def __init__(self, snapshots):
        self.snapshots = {snapshot.name: self._filtered(snapshot) for snapshot in snapshots}
        self.indexed_at = datetime.now(timezone.utc).isoformat()
        self.lock = Lock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("""
            CREATE VIRTUAL TABLE chunks USING fts5(
                repo UNINDEXED, path UNINDEXED, start UNINDEXED, end UNINDEXED,
                path_terms, code_terms, tokenize='porter unicode61'
            )
        """)
        with self.db:
            self.db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)", self.chunks())

    @staticmethod
    def _filtered(snapshot, exclusions=()):
        from .indexing.safety import excluded, decode
        files = {path: lines for path, lines in snapshot.files.items()
                 if not excluded(path, exclusions) and decode('\n'.join(lines).encode()) is not None}
        return Snapshot(snapshot.name, snapshot.revision, files, snapshot.demo, snapshot.limited)

    def chunks(self, snapshots=None):
        for snapshot in self.snapshots.values() if snapshots is None else snapshots:
            for path, lines in snapshot.files.items():
                path_terms = " ".join(terms(path))
                for start in range(0, len(lines), 9):
                    end = min(start + EXCERPT_LINES, len(lines))
                    code = "\n".join(lines[start:end])
                    if code.strip() and len(code) <= MAX_EXCERPT_CHARS:
                        yield snapshot.name, path, start + 1, end, path_terms, " ".join(terms(code))
                    if end == len(lines):
                        break

    def list_repos(self):
        with self.lock:
            snapshots = list(self.snapshots.values())
        return [{
            "name": snapshot.name,
            "revision": snapshot.revision,
            "files": len(snapshot.files),
            "indexed_at": self.indexed_at,
            "demo": snapshot.demo,
            "limited": snapshot.limited,
        } for snapshot in snapshots]

    def purge(self, repos):
        """Remove workspace mirrors as well as their searchable FTS payloads."""
        with self.lock:
            with self.db:
                for repo in repos:
                    self.db.execute("DELETE FROM chunks WHERE repo=?", (repo,))
                    self.snapshots.pop(repo, None)

    def restore(self, snapshot, *, exclusions=()):
        """Rebuild one explicitly enabled mirror from a trusted, filtered snapshot."""
        snapshot = self._filtered(snapshot, exclusions)
        with self.lock:
            with self.db:
                self.db.execute("DELETE FROM chunks WHERE repo=?", (snapshot.name,))
                self.db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)", self.chunks([snapshot]))
            self.snapshots[snapshot.name] = snapshot

    def search(self, repos, question, previous="", *, allowed_path=None):
        query_terms = [term for term in terms(question) if term not in STOP_WORDS]
        context_terms = [term for term in terms(previous) if term not in STOP_WORDS]
        search_terms = list(dict.fromkeys(query_terms[:16] + context_terms[:8]))[:20]
        if not search_terms:
            return []
        match = " OR ".join(f'"{term}"' for term in search_terms)
        by_repo = []
        with self.lock:
            for repo in repos:
                hits = self.db.execute("""
                    SELECT repo, path, start, end, bm25(chunks, 0, 0, 0, 0, 2, 1) AS score
                    FROM chunks WHERE chunks MATCH ? AND repo = ?
                    ORDER BY score, rowid LIMIT 14
                """, (match, repo)).fetchall()
                if hits:
                    by_repo.append(hits)

        # Cover the selected repositories first, then favor new files over overlapping excerpts.
        ranked = sorted((hits[0] for hits in by_repo), key=lambda hit: hit[4])
        remaining = sorted((hit for hits in by_repo for hit in hits[1:]), key=lambda hit: hit[4])
        covered = {(repo, path) for repo, path, *_ in ranked}
        overlaps = []
        for hit in remaining:
            key = hit[:2]
            if key in covered:
                overlaps.append(hit)
            else:
                covered.add(key)
                ranked.append(hit)
        ranked.extend(overlaps)
        evidence = []
        file_counts = {}
        for repo, path, start, end, _ in ranked:
            key = (repo, path)
            if file_counts.get(key, 0) >= 2:
                continue
            if allowed_path is not None and not allowed_path(repo, path):
                continue
            with self.lock:
                snapshot = self.snapshots.get(repo)
            if snapshot is None or path not in snapshot.files:
                continue
            code = "\n".join(snapshot.files[path][start - 1:end])
            evidence.append(Evidence(repo, snapshot.revision, path, start, end, code))
            file_counts[key] = file_counts.get(key, 0) + 1
            if len(evidence) == 10:
                break
        return evidence

    def source(self, repo, revision, path, start, end):
        snapshot = self.snapshots[repo]
        if snapshot.revision != revision:
            raise ValueError("This revision is no longer indexed. Ask again to cite the current snapshot.")
        lines = snapshot.files[path]
        if not 1 <= start <= end <= len(lines) or end - start >= EXCERPT_LINES:
            raise ValueError("Invalid source line range.")
        first = max(1, start - 5)
        last = min(len(lines), end + 5)
        return {"first": first, "lines": lines[first - 1:last], "total": len(lines)}
