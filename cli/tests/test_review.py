"""End-to-end tests for `nu rv` (lib/review.py) against throwaway repositories.

Each test builds an origin with a feature branch that only exists remotely, then drives
`bin/nu rv` the way the branch-review skill does. Run with: python3 -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

NU = Path(__file__).resolve().parent.parent / "bin" / "nu"
BRANCH = "ab-7-add-greeting"


class ReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nu-rv-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = master\n"
        )
        self.env = {
            **{k: v for k, v in os.environ.items() if k != "NU_CD_FILE"},
            "HOME": str(self.tmp),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "NO_COLOR": "1",
        }
        self.git(self.tmp, "init", "-q", "--bare", "origin.git")
        self.git(self.tmp, "clone", "-q", "origin.git", "repo")
        self.repo = self.tmp / "repo"
        self.write(self.repo, "src/app.py", "def main():\n    return 1\n")
        self.commit(self.repo, "base")
        self.git(self.repo, "push", "-q", "origin", "master")

        # The author's branch lives in a second clone, so the reviewer only has it remotely.
        self.git(self.tmp, "clone", "-q", "origin.git", "author")
        self.author = self.tmp / "author"
        self.git(self.author, "checkout", "-q", "-b", BRANCH)
        self.write(self.author, "src/app.py", "def main():\n    return greet()\n\n\ndef greet():\n    return 2\n")
        self.write(self.author, "tests/test_app.py", "from src.app import greet\n\nassert greet() == 2\n")
        self.write(self.author, "package-lock.json", "{}\n")
        self.commit(self.author, "AB-7 add greeting")
        self.git(self.author, "push", "-q", "origin", BRANCH)

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, text=True, check=True,
                              capture_output=True).stdout

    def write(self, root: Path, path: str, text: str) -> None:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)

    def commit(self, root: Path, msg: str) -> None:
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", msg)

    def nu(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(NU), "rv", *args], cwd=self.repo, env=self.env, text=True,
                              capture_output=True)

    def prep(self) -> dict:
        p = self.nu("prep", BRANCH, "--no-cd")
        self.assertEqual(p.returncode, 0, p.stderr)
        meta_path = self.tmp / "cache" / "nu" / "rv" / "repo" / BRANCH / "meta.json"
        return json.loads(meta_path.read_text())

    def test_prep_checks_out_a_remote_branch_beside_the_repo(self) -> None:
        meta = self.prep()

        self.assertEqual(Path(meta["worktree"]), self.tmp / "repo.worktrees" / BRANCH)
        self.assertEqual(meta["base"]["ref"], "origin/master")
        self.assertEqual(meta["ticket"], "AB-7")
        kinds = {f["path"]: (f["kind"], f["noise"]) for f in meta["files"]}
        self.assertEqual(kinds, {
            "src/app.py": ("source", False),
            "tests/test_app.py": ("test", False),
            "package-lock.json": ("lockfile", True),
        })
        # Two areas (src, tests) is past the threshold of one.
        self.assertEqual(meta["complexity"]["verdict"], "complex")

    def test_prep_fast_forwards_a_stale_review_worktree(self) -> None:
        first = self.prep()
        self.write(self.author, "src/app.py", "def main():\n    return greet()\n\n\ndef greet():\n    return 3\n")
        self.commit(self.author, "AB-7 change greeting")
        self.git(self.author, "push", "-q", "origin", BRANCH)

        second = self.prep()

        head = self.git(self.author, "rev-parse", "HEAD").strip()
        self.assertEqual(second["head"]["sha"], head)
        self.assertEqual(self.git(Path(second["worktree"]), "rev-parse", "HEAD").strip(), head)
        self.assertNotEqual(first["head"]["sha"], second["head"]["sha"])

    def review(self, **overrides) -> dict:
        review = {
            "verdict": {"decision": "fix", "text": "`greet` returns the wrong value."},
            "summary": "Adds `greet` and calls it from `main`.",
            "steps": [
                {"title": "Entry point", "explanation": "`main` now delegates.", "files": ["src/app.py"]},
                {"title": "Tests", "explanation": "One assertion.", "files": ["tests/test_app.py"]},
            ],
            "findings": [{
                "title": "greet returns 2",
                "severity": "high",
                "section": "bugs",
                "body": "The caller expects 1.",
                "where": "`app.py`, in `greet()`",
                "why": "`main` returned 1 before this change.",
                "file": "src/app.py",
                "line": 6,
                "comment": "issue (blocking): `greet` returns 2, but `main` used to return 1.",
            }],
        }
        review.update(overrides)
        return review

    def render(self, review: dict) -> subprocess.CompletedProcess:
        path = Path(self.prep()["paths"]["review_json"])
        path.write_text(json.dumps(review))
        return self.nu("render", str(path), "--no-open")

    def test_render_anchors_a_finding_under_its_line(self) -> None:
        p = self.render(self.review())

        self.assertEqual(p.returncode, 0, p.stderr)
        page = Path(p.stdout.strip()).read_text()
        row = page.index('data-l="6">6</td>')
        card = page.index('id="H1"')
        self.assertLess(row, card, "the finding card must follow the line it is about")
        self.assertIn('data-copy="issue (blocking):', page)
        self.assertIn("Fix review finding H1 in the worktree", page)
        # The lockfile was in no step, so it lands in the catch-all step instead of failing.
        self.assertIn("Generated files, lockfiles, translations and changelog", page)

    def test_render_rejects_a_review_that_skips_a_changed_file(self) -> None:
        review = self.review(steps=[{"title": "Entry", "explanation": "x", "files": ["src/app.py"]}])
        review["findings"][0]["comment"] = "Please fix this."

        p = self.render(review)

        self.assertEqual(p.returncode, 2)
        self.assertIn("'tests/test_app.py' is not in any step", p.stderr)
        self.assertIn("Conventional Comments label", p.stderr)


if __name__ == "__main__":
    unittest.main()
