from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from opendevengine.answer import respond, validate_reply
from opendevengine.index import CodeIndex, load_demos, load_git


DEMO = Path(__file__).resolve().parents[1] / "opendevengine" / "examples"
CHECKOUT = "Trace checkout from the storefront through the orders API, database, and confirmation email."


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.index = CodeIndex(load_demos(DEMO))
        self.addCleanup(self.index.db.close)

    def test_checkout_cites_all_four_repositories(self):
        repos = list(self.index.snapshots)
        result = respond(self.index, None, repos, CHECKOUT, "")
        self.assertEqual({citation["repo"] for citation in result["sources"]}, set(repos))
        self.assertEqual({match["literal"] for match in result["matches"]}, {
            "/api/orders", "orders", "order.created",
        })
        for citation in result["sources"]:
            repo, revision, path, start, end = (
                citation[key] for key in ("repo", "revision", "path", "start", "end")
            )
            source = self.index.source(repo, revision, path, start, end)
            start_offset = start - source["first"]
            excerpt = source["lines"][start_offset:start_offset + end - start + 1]
            self.assertEqual(excerpt, self.index.snapshots[repo].files[path][start - 1:end])

    def test_excluded_repo_contributes_no_results_or_matches(self):
        result = respond(self.index, None, ["orders-api", "orders-db"], CHECKOUT, "")
        self.assertEqual({cite["repo"] for cite in result["sources"]}, {"orders-api", "orders-db"})
        self.assertEqual([match["literal"] for match in result["matches"]], ["orders"])

    def test_unknown_model_citation_is_rejected(self):
        reply = {"sections": [{"heading": "Step", "text": "A claim", "sources": [2]}], "gap": ""}
        with self.assertRaises(ValueError):
            validate_reply(reply, 1)

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    def test_git_source_is_pinned_to_the_indexed_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git = ["git", "-C", directory]
            subprocess.run([*git, "init", "-q"], check=True)
            path = root / "Order.java"
            path.write_text("class CommittedOrder {}\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=do-not-index\n", encoding="utf-8")
            subprocess.run([*git, "add", "."], check=True)
            subprocess.run([
                *git, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                "-c", "commit.gpgsign=false", "commit", "-qm", "fixture",
            ], check=True)
            path.write_text("class UncommittedOrder {}\n", encoding="utf-8")
            snapshot = load_git("example", root)
            self.assertEqual(snapshot.files, {"Order.java": ("class CommittedOrder {}",)})
            index = CodeIndex([snapshot])
            self.addCleanup(index.db.close)
            with self.assertRaises(ValueError):
                index.source("example", "older-revision", "Order.java", 1, 1)


if __name__ == "__main__":
    unittest.main()
