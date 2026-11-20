"""End-to-end tests for lib/stack_rebase.py against throwaway repositories.

Each test builds an origin, a clone, and a stack of ticket branches, then drives the
engine the way `nu rb` does. Run with: python3 -m unittest discover tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "lib" / "stack_rebase.py"

B1, B2 = "ab-1-first", "ab-2-second"


class StackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="stack-rebase-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "gitconfig").write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n"
            "[init]\n\tdefaultBranch = master\n"
            "[advice]\n\tdetachedHead = false\n"
        )
        self.env = {
            **os.environ,
            "HOME": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git(self.tmp, "init", "-q", "--bare", "origin.git")
        self.git(self.tmp, "clone", "-q", "origin.git", "main")
        self.main = self.tmp / "main"
        self.write(self.main, "shared.txt", "one\ntwo\nthree\n")
        self.commit(self.main, "base")
        self.git(self.main, "push", "-q", "-u", "origin", "master")
        self.git(self.main, "remote", "set-head", "origin", "master")
        self.git(self.tmp, "clone", "-q", "origin.git", "other")
        self.other = self.tmp / "other"

    # -- fixture helpers ---------------------------------------------------
    def git(self, cwd: Path, *args: str) -> str:
        p = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        if p.returncode:
            raise AssertionError(f"git {' '.join(args)} failed:\n{p.stdout}{p.stderr}")
        return p.stdout.strip()

    def write(self, cwd: Path, path: str, text: str) -> None:
        (cwd / path).write_text(text)
        self.git(cwd, "add", "--", path)

    def commit(self, cwd: Path, subject: str) -> None:
        self.git(cwd, "commit", "-q", "--allow-empty", "-m", subject)

    def branch(self, name: str, start: str, subjects: list[str]) -> None:
        self.git(self.main, "switch", "-q", "-c", name, start)
        for i, subject in enumerate(subjects):
            self.write(self.main, f"{name}-{i}.txt", f"{subject}\n")
            self.commit(self.main, subject)
        self.git(self.main, "switch", "-q", "master")

    def stack(self) -> None:
        self.branch(B1, "master", ["AB-1 first", "AB-1 second"])
        self.branch(B2, B1, ["AB-2 only"])

    def advance_origin(self, path: str = "upstream.txt", text: str = "upstream\n") -> str:
        self.git(self.other, "pull", "-q", "--ff-only")
        self.write(self.other, path, text)
        self.commit(self.other, "XY-9 upstream change")
        self.git(self.other, "push", "-q", "origin", "master")
        return self.git(self.other, "rev-parse", "HEAD")

    def land(self, branch: str) -> None:
        """Squash-merge `branch` into origin/master, the way GitLab does."""
        self.git(self.main, "push", "-q", "origin", branch)
        self.git(self.other, "fetch", "-q", "origin")
        self.git(self.other, "reset", "-q", "--hard", "origin/master")
        self.git(self.other, "merge", "-q", "--squash", f"origin/{branch}")
        self.commit(self.other, f"{branch} (squashed)")
        self.git(self.other, "push", "-q", "origin", "master")

    def worktree(self, branch: str) -> Path:
        path = self.tmp / f"wt-{branch}"
        self.git(self.main, "worktree", "add", "-q", str(path), branch)
        return path

    def rb(self, cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ENGINE), *args], cwd=cwd, env=self.env,
                              capture_output=True, text=True)

    def ok(self, p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def sha(self, rev: str) -> str:
        return self.git(self.main, "rev-parse", rev)

    def count(self, rng: str) -> int:
        return int(self.git(self.main, "rev-list", "--count", rng))

    def assert_stacked(self, upstream: str) -> None:
        """B1 carries its 2 commits on `upstream`, B2 its 1 commit on B1."""
        self.git(self.main, "merge-base", "--is-ancestor", upstream, B1)
        self.git(self.main, "merge-base", "--is-ancestor", B1, B2)
        self.assertEqual(self.count(f"{upstream}..{B1}"), 2)
        self.assertEqual(self.count(f"{B1}..{B2}"), 1)

    # -- tests -------------------------------------------------------------
    def test_a_leading_base_is_the_base_not_a_branch_to_rewrite(self) -> None:
        self.stack()
        master = self.sha("master")
        new = self.advance_origin()
        self.ok(self.rb(self.main, "run", "master", B1, B2))
        self.assert_stacked(new)
        self.assertEqual(self.sha("master"), master, "the base itself must never be rewritten")
        self.assertEqual(self.git(self.main, "branch", "--show-current"), "master")

    def test_a_base_inside_the_stack_is_rejected(self) -> None:
        self.stack()
        p = self.rb(self.main, "plan", B1, "master", B2)
        self.assertEqual(p.returncode, 20, p.stdout + p.stderr)
        self.assertIn("master", p.stderr)

    def test_the_default_base_is_the_fetched_remote_not_the_stale_local_copy(self) -> None:
        self.stack()
        new = self.advance_origin()
        self.ok(self.rb(self.main, "run", B1, B2))
        self.assert_stacked(new)

    def test_a_bare_run_restacks_the_recorded_stack_from_any_branch_in_it(self) -> None:
        self.stack()
        self.ok(self.rb(self.main, "run", "master", B1, B2))
        # A reflog entry of the upper branch pointing into the lower one used to
        # flip the inferred order.
        tip = self.sha(B2)
        self.git(self.main, "update-ref", "-m", "reset: trap", f"refs/heads/{B2}", f"{B1}~1")
        self.git(self.main, "update-ref", "-m", "reset: back", f"refs/heads/{B2}", tip)
        for start in (B1, B2):
            with self.subTest(start=start):
                new = self.advance_origin(f"{start}.up", start)
                self.git(self.main, "switch", "-q", start)
                self.ok(self.rb(self.main, "run"))
                self.assert_stacked(new)
                self.git(self.main, "switch", "-q", "master")

    def test_a_squash_merged_parent_rewritten_after_the_child_was_cut_is_dropped(self) -> None:
        self.stack()
        self.git(self.main, "switch", "-q", B1)
        self.git(self.main, "commit", "-q", "--amend", "-m", "AB-1 second, reworded")
        self.git(self.main, "switch", "-q", "master")
        self.land(B1)
        p = self.ok(self.rb(self.main, "run", "master", B1, B2))
        self.assertIn("squash-merged", p.stdout)
        self.assertEqual(self.count(f"origin/master..{B2}"), 1, "only AB-2's own commit replays")

    def test_a_deleted_merged_parent_is_cut_at_its_last_ticket_commit(self) -> None:
        self.stack()
        self.land(B1)
        self.git(self.main, "branch", "-q", "-D", B1)
        p = self.ok(self.rb(self.main, "run", "master", B2))
        self.assertIn("AB-1", p.stdout)
        self.assertEqual(self.count(f"origin/master..{B2}"), 1, "only AB-2's own commit replays")

    def test_the_ticket_guard_refuses_to_replay_another_tickets_commits(self) -> None:
        self.branch(B2, "master", ["AB-2 one", "AB-7 stray", "AB-2 three"])
        self.advance_origin()
        p = self.rb(self.main, "run", B2)
        self.assertEqual(p.returncode, 20, p.stdout + p.stderr)
        self.assertIn("AB-7", p.stdout + p.stderr)
        self.ok(self.rb(self.main, "run", "--no-ticket-guard", B2))
        self.assertEqual(self.count(f"origin/master..{B2}"), 3)

    def test_every_branch_in_its_own_worktree_is_rebased_in_place(self) -> None:
        self.stack()
        wt1, wt2 = self.worktree(B1), self.worktree(B2)
        new = self.advance_origin()
        self.ok(self.rb(wt1, "run", "master", B1, B2))
        self.assert_stacked(new)
        for wt, branch in ((wt1, B1), (wt2, B2)):
            self.assertEqual(self.git(wt, "branch", "--show-current"), branch)
            self.assertEqual(self.git(wt, "status", "--porcelain"), "")
            self.assertTrue((wt / "upstream.txt").exists(), f"{wt} working tree not updated")

    def _conflict_in_the_upper_worktree(self) -> tuple[Path, Path, dict[str, str]]:
        self.branch(B1, "master", ["AB-1 first", "AB-1 second"])
        self.git(self.main, "switch", "-q", "-c", B2, B1)
        self.write(self.main, "shared.txt", "one\ntwo from AB-2\nthree\n")
        self.commit(self.main, "AB-2 only")
        self.git(self.main, "switch", "-q", "master")
        before = {b: self.sha(b) for b in (B1, B2)}
        wt1, wt2 = self.worktree(B1), self.worktree(B2)
        self.advance_origin("shared.txt", "one\ntwo from upstream\nthree\n")
        p = self.rb(wt1, "run", "master", B1, B2)
        self.assertEqual(p.returncode, 10, p.stdout + p.stderr)
        self.assertIn(str(wt2), p.stdout)
        return wt1, wt2, before

    def test_a_conflict_pauses_in_its_worktree_and_resumes_from_another(self) -> None:
        wt1, wt2, _ = self._conflict_in_the_upper_worktree()
        self.write(wt2, "shared.txt", "one\ntwo from both\nthree\n")
        self.ok(self.rb(wt1, "run"))
        self.assertEqual(self.count(f"origin/master..{B1}"), 2)
        self.assertEqual(self.count(f"{B1}..{B2}"), 1)
        self.git(self.main, "merge-base", "--is-ancestor", "origin/master", B1)
        self.git(self.main, "merge-base", "--is-ancestor", B1, B2)
        self.assertEqual(self.git(wt2, "branch", "--show-current"), B2)

    def test_abort_restores_every_branch_and_worktree(self) -> None:
        wt1, wt2, before = self._conflict_in_the_upper_worktree()
        self.ok(self.rb(self.main, "abort"))
        for b, sha in before.items():
            self.assertEqual(self.sha(b), sha, f"{b} not restored")
        for wt, branch in ((wt1, B1), (wt2, B2)):
            self.assertEqual(self.git(wt, "branch", "--show-current"), branch)
            self.assertEqual(self.git(wt, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
