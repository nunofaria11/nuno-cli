#!/usr/bin/env python3
"""stack-rebase: deterministic engine for rebasing a stack of dependent branches.

The mechanical parts of a stacked rebase (the stack's recorded parents, the cut
point below each branch, one rebase per branch in the worktree that holds it,
replay of known resolutions, generated-file classes, line-disjoint three-way
merges) are decided here. Only genuine semantic conflicts are handed back to the
caller.

Exit codes:
    0   nothing left to do (stack rebased, or already up to date)
    10  paused: conflicts need a judgement call (report printed on stdout)
    20  precondition failed / operator input required

All git behaviour tweaks are passed per invocation with `git -c`, plus a private
attributes file under $GIT_COMMON_DIR/stack-rebase/. The only repository
configuration written is each branch's stack parent (branch.<name>.nuParent and
nuParentTip), recorded when a run finishes.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

DRIVER = "stack-take-theirs"
STATE_VERSION = 2
MAX_STEPS = 500
MAX_DIFF_LINES = 20_000

LOCKFILES = (
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock",
    "Gemfile.lock", "composer.lock", "pubspec.lock", "go.sum", "gradle.lockfile",
    "verification-metadata.xml",
)

CONFLICT_RE = re.compile(
    r"^<<<<<<< (?P<ours>.*)\n(?P<obody>(?:.*\n)*?)"
    r"(?:\|\|\|\|\|\|\| (?P<baselabel>.*)\n(?P<bbody>(?:.*\n)*?))?"
    r"^=======\n(?P<tbody>(?:.*\n)*?)^>>>>>>> (?P<theirs>.*)$",
    re.MULTILINE,
)

# "Staged" with rerere.autoupdate, "Resolved" without it.
RERERE_RE = re.compile(r"(?:Resolved|Staged) '(?P<path>[^']+)' using previous resolution\.")


# --------------------------------------------------------------------------- git


class Git:
    """One worktree of the repository; a run's state is shared by all of them."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.top = self._bare(["rev-parse", "--show-toplevel"])
        self.common = Path(
            self._bare(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        )
        self.gitdir = Path(self._bare(["rev-parse", "--path-format=absolute", "--git-dir"]))
        self.state_dir = self.common / "stack-rebase"
        self.attrs_file = self.state_dir / "attributes"
        self.state_file = self.state_dir / "state.json"
        # Present while a run is unfinished, so any worktree can resume or abort it.
        self.active = self.state_dir / "active"
        self.driver_log = self.state_dir / "driver.log"
        # A run can span several processes (pause, resolve, resume), so what was
        # resolved and what owes a regeneration has to outlive one of them.
        self.journal = self.state_dir / "journal.tsv"

    # -- plumbing ----------------------------------------------------------
    def _bare(self, args: list[str]) -> str:
        p = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True)
        if p.returncode:
            sys.stderr.write(p.stderr)
            sys.exit(20)
        return p.stdout.strip()

    def _prelude(self) -> list[str]:
        return [
            "-c", "rerere.enabled=true",
            "-c", "rerere.autoupdate=true",
            "-c", "merge.conflictStyle=zdiff3",
            "-c", f"core.attributesFile={self.attrs_file}",
            "-c", f"merge.{DRIVER}.name=stack-rebase: keep the replayed branch side",
            # The log is the only record of what the driver silently resolved.
            "-c", f"merge.{DRIVER}.driver=cp '%B' '%A' && echo '%P' >> '{self.driver_log}'",
        ]

    def run(self, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true", LC_ALL="C")
        return subprocess.run(
            ["git", *self._prelude(), *args],
            cwd=cwd or self.top, capture_output=True, text=True, env=env,
        )

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0

    def must(self, *args: str) -> str:
        p = self.run(*args)
        if p.returncode:
            die(f"git {' '.join(args)} (in {self.top})\n{p.stdout}{p.stderr}")
        return p.stdout.strip()

    # -- queries -----------------------------------------------------------
    def sha(self, rev: str) -> str:
        return self.out("rev-parse", "--verify", "--quiet", rev)

    def short(self, rev: str) -> str:
        return self.out("rev-parse", "--short", rev)

    def subject(self, rev: str) -> str:
        return self.out("log", "-1", "--format=%s", rev)

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def count(self, rng: str) -> int:
        v = self.out("rev-list", "--count", rng)
        return int(v) if v.isdigit() else 0

    def rebase_active(self) -> bool:
        return (self.gitdir / "rebase-merge").exists() or (self.gitdir / "rebase-apply").exists()

    def current(self) -> str:
        """The checked-out branch, or "" when HEAD is detached."""
        return self.out("symbolic-ref", "--quiet", "--short", "HEAD")

    def worktrees(self) -> dict[str, str]:
        """Branch -> path of the worktree that has it checked out."""
        out: dict[str, str] = {}
        path = ""
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line.startswith("branch refs/heads/"):
                out[line[18:]] = path
        return out


def die(msg: str, code: int = 20) -> NoReturn:
    print(f"stack-rebase: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------- attributes


def generated_patterns(g: Git) -> list[str]:
    """Patterns the repository itself declares as generated.

    Any .gitattributes entry carrying an attribute whose name contains
    "generated" counts, so repositories keep owning that classification.
    """
    patterns: list[str] = []
    for rel in g.out("ls-files", "--", ".gitattributes", "*/.gitattributes").splitlines():
        prefix = os.path.dirname(rel)
        blob = g.out("show", f"HEAD:{rel}") if not (Path(g.top) / rel).exists() else \
            (Path(g.top) / rel).read_text(errors="replace")
        for line in blob.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = shlex.split(line) if '"' in line else line.split()
            if len(parts) < 2:
                continue
            pattern, attrs = parts[0], parts[1:]
            if not any("generated" in a and not a.startswith(("-", "!")) for a in attrs):
                continue
            if prefix and not pattern.startswith("/"):
                pattern = f"{prefix}/{pattern.lstrip('/')}"
            patterns.append(pattern.lstrip("/"))
    return patterns


def install_policy(g: Git, policy: dict) -> tuple[list[str], list[str]]:
    """Return (driver patterns, generated patterns) and write the attributes file.

    Only lockfiles get the take-the-branch-side driver: they are never mergeable
    line by line, they are huge, and the answer is always to re-resolve them with
    the dependency tool. Generated files are left to conflict so that a clean
    three-way merge still wins, with take-theirs as the fallback.
    """
    g.state_dir.mkdir(parents=True, exist_ok=True)
    inherited = ""
    prev = g.out("config", "--global", "--get", "core.attributesFile")
    default = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "git" / "attributes"
    src = Path(os.path.expanduser(prev)) if prev else default
    if src.exists() and src.resolve() != g.attrs_file.resolve():
        inherited = src.read_text(errors="replace").rstrip() + "\n"

    driver = ([f"**/{name}" for name in LOCKFILES] + list(LOCKFILES)
              + list(policy.get("takeTheirs", [])))
    g.attrs_file.write_text(
        "# generated by stack-rebase; safe to delete\n" + inherited
        + "".join(f"{p} merge={DRIVER}\n" for p in driver)
    )
    return driver, generated_patterns(g)


def load_policy(g: Git) -> dict:
    path = Path(g.top) / ".stack-rebase.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"{path}: {exc}")
    return {}


def matches(path: str, patterns) -> bool:
    return any(
        fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.replace("**/", ""))
        or (p.endswith("/**") and path.startswith(p[:-3] + "/"))
        for p in patterns
    )


# ------------------------------------------------------------------------ plan

# Per-branch stack metadata in the repository config: the parent a branch sits
# on, and that parent's tip when it last did. Written by every finished run and
# by `nu wt add --base <branch>`.
PARENT, PARENT_TIP = "nuParent", "nuParentTip"
DEFAULT_TICKET = r"[A-Za-z]+-[0-9]+"


def recorded(g: Git) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for line in g.out("config", "--get-regexp", r"^branch\..*\.nuparent(tip)?$").splitlines():
        key, _, value = line.partition(" ")
        name, _, var = key[len("branch."):].rpartition(".")
        out.setdefault(name, {})["tip" if var == PARENT_TIP.lower() else "parent"] = value
    return out


def record(g: Git, p: dict) -> None:
    for s in p["steps"]:
        tip = p["base_sha"] if s["parent"] == p["base"] else g.sha(f"refs/heads/{s['parent']}")
        g.must("config", f"branch.{s['branch']}.{PARENT}", s["parent"])
        g.must("config", f"branch.{s['branch']}.{PARENT_TIP}", tip)


def default_branch(g: Git) -> str:
    head = g.out("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for cand in ("master", "main"):
        if g.sha(f"refs/remotes/origin/{cand}") or g.sha(f"refs/heads/{cand}"):
            return cand
    die("cannot determine the default branch; name the base first (nu rb master b1 b2)")


def is_base(g: Git, name: str, default: str) -> bool:
    """A trunk or a remote-tracking ref: what a stack sits on, never part of it."""
    return (name in (default, f"origin/{default}") or name.startswith("refs/remotes/")
            or bool(g.sha(f"refs/remotes/{name}")))


def resolve_base(g: Git, name: str, fetch: bool) -> str:
    # The local copy of a trunk is usually behind; rebasing onto it lands the
    # stack on a stale base.
    ref = f"origin/{name}" if g.sha(f"refs/remotes/origin/{name}") else name
    remote, _, branch = ref.partition("/")
    if fetch and branch and g.sha(f"refs/remotes/{ref}"):
        if g.run("fetch", "--quiet", remote, branch).returncode:
            print(f"stack-rebase: fetching {ref} failed; using the last fetched one",
                  file=sys.stderr)
    if not g.sha(ref):
        die(f"base {name!r} does not resolve")
    return ref


def resolve_chain(g: Git, args, rec: dict, default: str) -> tuple[str, list[str]]:
    names, base = list(args.branches), args.base
    if names and is_base(g, names[0], default):
        if base:
            die(f"two bases given: --base {base} and {names[0]}")
        base = names.pop(0)
    for n in names:
        if is_base(g, n, default):
            die(f"{n} is a base, not part of the stack: put it first (nu rb {n} b1 b2) "
                f"or pass --base {n}")
        if not g.sha(f"refs/heads/{n}"):
            die(f"unknown branch {n!r}")
    chain = names or infer_chain(g, rec, default)
    if not base:
        parent = rec.get(chain[0], {}).get("parent")
        base = parent if parent and is_base(g, parent, default) else default
    return resolve_base(g, base, not args.no_fetch), chain


def infer_chain(g: Git, rec: dict, default: str) -> list[str]:
    """The current branch plus its recorded parents below and children above."""
    cur = g.current()
    if not cur:
        die("detached HEAD: name the stack (nu rb master b1 b2)")
    if is_base(g, cur, default):
        die(f"{cur} is a base: name the stack (nu rb {cur} b1 b2)")

    def local(b: str | None) -> bool:
        return bool(b) and bool(g.sha(f"refs/heads/{b}"))

    chain = [cur]
    while True:
        parent = rec.get(chain[0], {}).get("parent")
        if not local(parent) or parent in chain or is_base(g, parent, default):
            break
        chain.insert(0, parent)
    while True:
        kids = sorted(b for b, r in rec.items()
                      if r.get("parent") == chain[-1] and b not in chain and local(b))
        if len(kids) > 1:
            die(f"the stack forks above {chain[-1]} ({', '.join(kids)}): name the branches")
        if not kids:
            return chain
        chain.append(kids[0])


def reflog_shas(g: Git, ref: str) -> list[str]:
    seen: list[str] = []
    for line in g.out("reflog", "show", "--format=%H", ref).splitlines():
        sha = line.strip()
        if sha and sha not in seen:
            seen.append(sha)
    return seen


def squash_merged(g: Git, branch: str, base: str, since: str) -> bool:
    """True when `branch`'s change since `since` already sits in `base` as a single commit."""
    tree = g.sha(f"{branch}^{{tree}}")
    if not since or tree == g.sha(f"{since}^{{tree}}"):
        return False
    # A dangling commit carrying the branch as one patch, so `git cherry` can
    # match it against the squash commit by patch-id.
    probe = g.out("commit-tree", tree, "-p", since, "-m", "stack-rebase squash probe")
    return bool(probe) and g.out("cherry", base, probe, since).startswith("-")


def stack_commits(g: Git, branch: str, base: str) -> list[tuple[str, str]]:
    """(sha, subject) of the commits `branch` has over `base`, newest first."""
    out = []
    for line in g.out("log", "--topo-order", "--format=%H %s", branch, "--not", base).splitlines():
        sha, _, subject = line.partition(" ")
        out.append((sha, subject))
    return out


def ticket_pattern(g: Git) -> re.Pattern | None:
    p = g.run("config", "--get", "nu.rb.ticketPattern")
    raw = p.stdout.strip() if p.returncode == 0 else DEFAULT_TICKET
    return re.compile(raw) if raw else None


def ticket(pattern: re.Pattern | None, text: str) -> str | None:
    m = pattern.match(text) if pattern else None
    return m.group(0).upper() if m else None


def derive_cut(g: Git, branch: str, olds: list[str], rec_tip: str | None, base: str,
               commits: list[tuple[str, str]], pattern: re.Pattern | None) -> tuple[str, str]:
    """The commit `branch` was built on: everything above it is the branch's own."""
    # Index in `commits` (newest first) = how many commits a cut there replays.
    pos = {sha: i for i, (sha, _) in enumerate(commits)}
    exact: list[tuple[int, str, str]] = []
    if rec_tip in pos:
        exact.append((pos[rec_tip], rec_tip, "recorded parent tip"))
    for old in olds:
        tip = g.sha(f"refs/heads/{old}")
        if tip in pos:
            exact.append((pos[tip], tip, f"tip of {old}"))
            continue
        # A rewritten or squash-merged parent: the newest tip it had that the
        # branch still contains.
        hit = next((s for s in reflog_shas(g, f"refs/heads/{old}") if s in pos), None)
        if hit:
            exact.append((pos[hit], hit, f"reflog of {old}"))
    if exact:
        _, sha, how = min(exact)
        return sha, how

    # Nothing remembers the parent (deleted after its squash merge, never
    # recorded): the oldest commits tagged with other tickets are its leftovers.
    mine = ticket(pattern, branch)
    if mine:
        below: list[tuple[str, str]] = []
        for sha, subject in reversed(commits):
            t = ticket(pattern, subject)
            if t is None or t == mine:
                break
            below.append((sha, t))
        if below and len(below) < len(commits):
            tags = ", ".join(sorted({t for _, t in below}))
            return below[-1][0], f"below {mine}: {len(below)} commit(s) tagged {tags}"
    return g.out("merge-base", base, branch), f"merge-base with {base}"


def plan(g: Git, args) -> dict:
    rec = recorded(g)
    default = default_branch(g)
    base, chain = resolve_chain(g, args, rec, default)
    base_sha = g.sha(base)
    pattern = ticket_pattern(g)
    holders = g.worktrees()
    merged: list[dict] = []
    steps: list[dict] = []
    below = None
    for b in chain:
        commits = stack_commits(g, b, base)
        r = rec.get(b, {})
        olds = [o for o in dict.fromkeys((below, r.get("parent")))
                if o and not is_base(g, o, default)]
        cut, how = derive_cut(g, b, olds, r.get("tip"), base, commits, pattern)
        below = b
        # A squash-merged branch shares no commit with the base, so only a
        # patch-id probe tells it has landed. Only the bottom of a stack lands.
        if not steps and (g.is_ancestor(b, base) or squash_merged(g, b, base, cut)):
            merged.append({"branch": b, "sha": g.sha(b)})
            continue
        if not steps and args.old_tip:
            cut = g.sha(args.old_tip)
            if not cut or not g.is_ancestor(cut, b):
                die(f"--old-tip {args.old_tip!r} is not an ancestor of {b}")
            how = "operator supplied"
        parent = steps[-1]["branch"] if steps else base
        parent_tip = base_sha if parent == base else g.sha(f"refs/heads/{parent}")
        own = commits[:next((i for i, (sha, _) in enumerate(commits) if sha == cut), len(commits))]
        mine = ticket(pattern, b)
        foreign: dict[str, int] = {}
        for _, subject in own if mine else ():
            t = ticket(pattern, subject)
            if t and t != mine:
                foreign[t] = foreign.get(t, 0) + 1
        steps.append({
            "branch": b, "parent": parent, "cut": cut, "cut_source": how,
            "commits": len(own), "old_sha": g.sha(b), "worktree": holders.get(b),
            "stale": cut != parent_tip or any(s["stale"] for s in steps),
            "foreign": foreign,
        })
    return {"base": base, "base_sha": base_sha, "merged": merged, "steps": steps,
            "home": g.top, "home_ref": g.current() or g.sha("HEAD")}


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def print_plan(g: Git, p: dict) -> None:
    print(f"base: {p['base']} @ {g.short(p['base_sha'])}")
    for m in p["merged"]:
        print(f"  -- {m['branch']}  squash-merged into {p['base']}, dropped")
    for s in p["steps"]:
        print(f"  {'!!' if s['stale'] else 'ok'} {s['branch']}  "
              f"{s['commits']} commit(s) on {s['parent']}")
        if s["stale"]:
            print(f"       cut {g.short(s['cut'])}  {s['cut_source']}")
    stale = [s for s in p["steps"] if s["stale"]]
    if not stale:
        print("\nnothing to do: every branch already sits on its parent")
        return
    print("\none rebase per branch, bottom first, in the worktree that holds it:")
    for s in stale:
        print(f"  {tilde(s['worktree'] or p['home'])}$ "
              f"git rebase --onto {s['parent']} {g.short(s['cut'])} {s['branch']}")
    for s in stale:
        if s["foreign"]:
            tags = ", ".join(f"{n} commit(s) tagged {t}" for t, n in sorted(s["foreign"].items()))
            print(f"\nticket guard: {s['branch']} would replay {tags}")
            print("  pass --old-tip <sha> to cut below them, or --no-ticket-guard if they belong")


# -------------------------------------------------------------- conflict model


def stage_blob(g: Git, path: str, stage: int) -> str | None:
    p = g.run("cat-file", "-p", f":{stage}:{path}")
    return p.stdout if p.returncode == 0 else None


def unmerged(g: Git) -> dict[str, set[int]]:
    files: dict[str, set[int]] = {}
    for line in g.out("ls-files", "-u", "-z").split("\0"):
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        files.setdefault(path, set()).add(int(meta.split()[-1]))
    return files


def split_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def edits(base: list[str], side: list[str]) -> list[tuple[int, int, list[str]]]:
    out: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=base, b=side,
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1 > 1:
            # One edit per line: two adjacent but independent line rewrites must
            # not drag each other into the same conflict region.
            out += [(i1 + k, i1 + k + 1, [side[j1 + k]]) for k in range(i2 - i1)]
        else:
            out.append((i1, i2, side[j1:j2]))
    return out


def _clashes(a, b) -> bool:
    (a1, a2, atxt), (b1, b2, btxt) = a, b
    if (a1, a2, atxt) == (b1, b2, btxt):
        return False                              # both sides made the same edit
    ins_a, ins_b = a1 == a2, b1 == b2
    if ins_a and ins_b:
        return a1 == b1                           # same insertion point, different text
    if ins_a:
        return b1 < a1 < b2                       # insertion inside a rewritten region
    if ins_b:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2                    # overlapping rewrites


def _apply(base: list[str], side: list, lo: int, hi: int) -> list[str]:
    out, pos = [], lo
    for i1, i2, txt in sorted(side, key=lambda e: (e[0], e[1])):
        if i2 < lo or i1 > hi:
            continue
        out.extend(base[pos:i1])
        out.extend(txt)
        pos = max(pos, i2)
    out.extend(base[pos:hi])
    return out


def _marker(out: list[str], text: str) -> None:
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    out.append(text)


def three_way(base: str, ours: str, theirs: str, label: str = "replayed commit"):
    """Line-granular three-way merge, narrowed to the lines that actually clash.

    git conflicts whole hunks, so an insertion next to a changed line collides even
    though the two edits touch different base lines. That is the dominant shape of a
    stacked-rebase conflict and it has exactly one sane resolution. Edits that do
    clash are re-emitted as a conflict block spanning only their own base range, so
    the question handed back is the decision and nothing else.

    Returns (text, reason, remaining conflict blocks).
    """
    if ours == theirs:
        return ours, "both sides identical", 0
    b, o, t = split_lines(base), split_lines(ours), split_lines(theirs)
    eo, et = edits(b, o), edits(b, t)
    if not eo:
        return theirs, "unchanged below; taken from the replayed branch", 0
    if not et:
        return ours, "unchanged in the replayed branch; kept as below", 0

    nodes = [("o", e) for e in eo] + [("t", e) for e in et]
    group = list(range(len(nodes)))

    def find(i: int) -> int:
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i, (si, ei) in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            sj, ej = nodes[j]
            if si != sj and _clashes(ei, ej):
                group[find(i)] = find(j)

    clusters: dict[int, list] = {}
    for i, node in enumerate(nodes):
        clusters.setdefault(find(i), []).append(node)

    units = []
    for members in clusters.values():
        lo = min(e[0] for _, e in members)
        hi = max(e[1] for _, e in members)
        one_sided = len({s for s, _ in members}) == 1
        identical = len({(e[0], e[1], tuple(e[2])) for _, e in members}) == 1
        units.append((lo, hi, "apply" if one_sided or identical else "clash", members))

    merged: list[str] = []
    pos = clashes = applied = 0
    for lo, hi, kind, members in sorted(units, key=lambda u: (u[0], u[1])):
        lo, hi = max(lo, pos), max(hi, pos)
        merged.extend(b[pos:lo])
        if kind == "apply":
            merged.extend(_apply(b, [e for _, e in members], lo, hi))
            applied += 1
        else:
            _marker(merged, "<<<<<<< HEAD\n")
            merged.extend(_apply(b, [e for s, e in members if s == "o"], lo, hi))
            _marker(merged, "||||||| base\n")
            merged.extend(b[lo:hi])
            _marker(merged, "=======\n")
            merged.extend(_apply(b, [e for s, e in members if s == "t"], lo, hi))
            _marker(merged, f">>>>>>> {label}\n")
            clashes += 1
        pos = hi
    merged.extend(b[pos:])
    reason = (f"{clashes} region(s) clash, {applied} disjoint edit(s) applied around them"
              if clashes else "edits touch disjoint base lines")
    return "".join(merged), reason, clashes


def conflict_hunks(g: Git, path: str, limit: int = 80) -> str:
    full = Path(g.top) / path
    try:
        text = full.read_text()
    except (OSError, UnicodeDecodeError):
        return "(binary or unreadable)"
    out = []
    for m in CONFLICT_RE.finditer(text):
        body = text[m.start():m.end()].splitlines()
        if len(body) > limit:
            body = body[:limit] + [f"... ({len(body) - limit} more lines)"]
        out.append("\n".join(body))
    return "\n\n".join(out) or "(no conflict markers: index-level conflict)"


HINTS = {
    frozenset({1, 2}): ("the replayed branch deleted it, the history below modified it",
                        "git rm -- {p}                      # honour the deletion\n"
                        "    git checkout --ours -- {p} && git add -- {p}   # keep the modified file"),
    frozenset({1, 3}): ("the history below deleted it, the replayed branch modified it",
                        "git rm -- {p}                      # honour the deletion\n"
                        "    git checkout --theirs -- {p} && git add -- {p} # keep the branch's file"),
    frozenset({2, 3}): ("added on both sides with different content",
                        "merge the two sides by hand, then git add -- {p}"),
}


def classify(g: Git, path: str, stages: set[int], policy: dict, generated: list[str],
             label: str = "replayed commit") -> dict:
    entry = {"path": path, "stages": sorted(stages)}
    if matches(path, policy.get("askAlways", [])):
        entry.update(kind="ask", reason="matched askAlways policy")
        return entry
    if stages != {1, 2, 3}:
        reason, hint = HINTS.get(frozenset(stages), ("unusual stage combination", ""))
        entry.update(kind="ask", reason=reason, hint=hint.format(p=shlex.quote(path)))
        return entry

    base, ours, theirs = (stage_blob(g, path, i) for i in (1, 2, 3))
    if base is None or ours is None or theirs is None:
        entry.update(kind="ask", reason="could not read all three stages")
        return entry
    if any("\0" in blob for blob in (base, ours, theirs)):
        entry.update(kind="ask", reason="binary file",
                     hint="git checkout --ours|--theirs -- {p} && git add -- {p}".format(
                         p=shlex.quote(path)))
        return entry

    gen = matches(path, generated)
    if max(blob.count("\n") for blob in (base, ours, theirs)) > MAX_DIFF_LINES:
        # A line-by-line merge of a file this size costs seconds and buys nothing
        # for machine-written content.
        if gen:
            entry.update(kind="regenerate", merged=theirs, clashes=0,
                         reason=f"generated and over {MAX_DIFF_LINES} lines; took the "
                                "branch side, must be regenerated")
        else:
            entry.update(kind="ask",
                         reason=f"over {MAX_DIFF_LINES} lines; not merged line by line")
        return entry

    merged, why, clashes = three_way(base, ours, theirs, label)
    entry.update(reason=why, merged=merged, clashes=clashes)
    if clashes == 0:
        entry["kind"] = "regenerate" if gen else "auto"
        if gen:
            entry["reason"] = f"{why}; generated, so regenerate to be sure"
    elif gen:
        entry.update(kind="regenerate", merged=theirs, clashes=0,
                     reason="generated and not mechanically mergeable; took the branch "
                            "side, must be regenerated")
    else:
        entry["kind"] = "semantic"
    return entry


# -------------------------------------------------------------------- the loop


def dump_sides(g: Git, path: str) -> dict[str, str]:
    out = {}
    slug = path.replace("/", "%")
    for stage, name in ((1, "base"), (2, "ours"), (3, "theirs")):
        blob = stage_blob(g, path, stage)
        if blob is None:
            continue
        dest = g.state_dir / "sides" / f"{slug}.{name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(blob)
        out[name] = str(dest)
    return out


def journal_add(g: Git, kind: str, path: str, reason: str = "") -> None:
    g.state_dir.mkdir(parents=True, exist_ok=True)
    with g.journal.open("a") as fh:
        fh.write(f"{kind}\t{path}\t{reason}\n")


def journal(g: Git, kind: str, with_reason: bool = True) -> list[str]:
    seen: list[str] = []
    if not g.journal.exists():
        return seen
    for line in g.journal.read_text().splitlines():
        k, _, rest = line.partition("\t")
        if k != kind:
            continue
        path, _, reason = rest.partition("\t")
        entry = f"{path}: {reason}" if reason and with_reason else path
        if entry not in seen:
            seen.append(entry)
    return seen


def save_state(g: Git, state: dict) -> None:
    g.state_dir.mkdir(parents=True, exist_ok=True)
    g.state_file.write_text(json.dumps(state, indent=1))


def last_state(g: Git) -> dict:
    return json.loads(g.state_file.read_text()) if g.state_file.exists() else {}


def active_state(g: Git) -> dict | None:
    return last_state(g) if g.active.exists() else None


def backup(g: Git, branches: list[str]) -> list[str]:
    made = []
    for b in branches:
        ref = f"refs/stack-backup/{b}"
        cur, sha = g.sha(ref), g.sha(b)
        if cur == sha:
            continue
        if cur:
            g.must("update-ref", f"refs/stack-backup-prev/{b}/{cur[:12]}", cur)
        g.must("update-ref", ref, sha)
        made.append(f"{ref} -> {g.short(sha)}")
    return made


def preconditions(p: dict, guard: bool) -> None:
    stale = [s for s in p["steps"] if s["stale"]]
    for path in dict.fromkeys(s["worktree"] or p["home"] for s in stale):
        w = Git(path)
        if w.rebase_active():
            die(f"{w.top} already has a rebase in progress")
        dirty = w.out("status", "--porcelain", "--untracked-files=no")
        if dirty:
            die(f"{w.top} is not clean:\n{dirty}")
    if guard and any(s["foreign"] for s in stale):
        die("ticket guard: a branch would replay commits of other tickets (see the plan)")


def driver_resolved(g: Git) -> list[str]:
    """Paths the take-the-branch-side driver silently resolved during this run."""
    if not g.driver_log.exists():
        return []
    seen: list[str] = []
    for line in g.driver_log.read_text().splitlines():
        path = line.strip()
        if path and path not in seen:
            seen.append(path)
    return seen


def regen_commands(policy: dict, paths: list[str]) -> list[str]:
    return sorted({
        cmd for pat, cmd in policy.get("regen", {}).items()
        for p in paths if matches(p, [pat])
    })


def report(g: Git, entries: list[dict], driver: list[str], regen: list[str]) -> None:
    print("=" * 72)
    print(f"PAUSED: {len(entries)} file(s) need a decision")
    step, total = g.gitdir / "rebase-merge" / "msgnum", g.gitdir / "rebase-merge" / "end"
    if step.exists() and total.exists():
        print(f"rebase step {step.read_text().strip()}/{total.read_text().strip()}"
              f" -- replaying {g.out('log', '-1', '--format=%h %s', 'REBASE_HEAD')}")
    print("HEAD side = history already replayed below; other side = the commit being replayed")
    print("=" * 72)
    for e in entries:
        print(f"\n--- {e['path']}  [{e['kind']}] {e['reason']}")
        for name, path in dump_sides(g, e["path"]).items():
            print(f"    {name:7}{path}")
        for line in e.get("hint", "").splitlines():
            print(f"    fix    {line.strip()}" if line.strip() else "")
        print(conflict_hunks(g, e["path"]))
    if driver:
        print("\ngenerated/lock files touched, regenerate before pushing: " + ", ".join(driver))
    if regen:
        print("regeneration owed before the stack is pushed:")
        for c in regen:
            print(f"  {c}")
    print(f"\nResolve in {g.top}, `git add` each path, then run the rebase again "
          "from any worktree.")


def note_replays(g: Git, p: subprocess.CompletedProcess) -> None:
    """Record the paths git resolved from the rerere cache rather than by merging."""
    for m in RERERE_RE.finditer(p.stdout + p.stderr):
        journal_add(g, "rerere", m.group("path"))


def settle(w: Git, policy: dict, generated: list[str]) -> int:
    """Drive one rebase to its end: 0 once it finished, 10 when it needs a decision."""
    for _ in range(MAX_STEPS):
        if not w.rebase_active():
            return 0
        files = unmerged(w)
        if not files:
            if w.sha("REBASE_HEAD") and w.ok("diff-index", "--quiet", "HEAD"):
                print("=" * 72)
                print("PAUSED: this commit is empty here -- its change already "
                      "landed in the history below")
                print(w.out("log", "-1", "--format=%h %s", "REBASE_HEAD"))
                print(w.out("show", "--stat", "--format=", "REBASE_HEAD"))
                print(f"Confirm, then one of, in {w.top}:\n"
                      "  git rebase --skip                        # drop the redundant commit\n"
                      "  git commit --allow-empty -C REBASE_HEAD  # keep it as an empty commit\n"
                      "then run the rebase again.")
                return 10
            # rerere replayed it, a driver resolved it, or the caller staged a fix.
            note_replays(w, w.run("rebase", "--continue"))
            continue

        label = w.out("log", "-1", "--format=%h (%s)", "REBASE_HEAD")
        pending: list[dict] = []
        progress = False
        for path, stages in sorted(files.items()):
            e = classify(w, path, stages, policy, generated, label)
            if e["kind"] in ("auto", "regenerate"):
                (Path(w.top) / path).write_text(e["merged"])
                w.must("add", "--", path)
                journal_add(w, "auto" if e["kind"] == "auto" else "regen",
                            path, e["reason"])
                progress = True
                continue
            if e.get("merged"):
                # Narrow the markers to the lines that actually clash.
                (Path(w.top) / path).write_text(e["merged"])
            pending.append(e)
        if pending:
            for title, kind in (("replayed from a previous resolution", "rerere"),
                                ("auto-resolved so far", "auto"),
                                ("generated, resolved but owed a regeneration", "regen")):
                lines = journal(w, kind)
                if lines:
                    print(f"{title}:")
                    for line in lines:
                        print(f"  {line}")
                    print()
            owed = driver_resolved(w) + journal(w, "regen", with_reason=False)
            report(w, pending, owed, regen_commands(policy, owed))
            return 10
        if not progress:
            die(f"no progress possible; inspect `git status` in {w.top}")
    die(f"rebase still in progress after the step limit; inspect `git status` in {w.top}")


def restore_home(w: Git, ref: str) -> None:
    """Put back what the invoking worktree had checked out before it lent itself out."""
    if ref in (w.current(), w.sha("HEAD")):
        return
    if w.sha(f"refs/heads/{ref}"):
        w.must("switch", "--quiet", ref)
    else:
        w.must("checkout", "--quiet", "--detach", ref)


def do_run(g: Git, args) -> int:
    policy = load_policy(g)
    state = active_state(g)
    if state is None:
        p = plan(g, args)
        if not args.quiet:
            print_plan(g, p)
        stale = [s for s in p["steps"] if s["stale"]]
        if not stale:
            record(g, p)
            return 0
        preconditions(p, guard=not args.no_ticket_guard)
        if args.dry_run:
            return 0
        made = backup(g, [s["branch"] for s in stale])
        if made:
            print("\nbackups: " + ", ".join(made))
        g.driver_log.unlink(missing_ok=True)
        g.journal.unlink(missing_ok=True)
        state = {"version": STATE_VERSION, "started": time.time(), **p}
        save_state(g, state)
        g.active.touch()

    for s in state["steps"]:
        if not s["stale"] or s.get("done"):
            continue
        w = Git(s["worktree"] or state["home"])
        _, generated = install_policy(w, policy)
        if not w.rebase_active() and not s.get("onto"):
            onto = (state["base_sha"] if s["parent"] == state["base"]
                    else w.sha(f"refs/heads/{s['parent']}"))
            # Saved before rebasing: a step that has an onto was started, so a
            # resume never replays a branch that was finished by hand.
            s["onto"] = onto
            save_state(g, state)
            # --empty=stop: a commit whose content already landed below is a
            # decision, not something to drop silently.
            r = w.run("rebase", "--onto", onto, s["cut"], s["branch"], "--empty=stop")
            note_replays(w, r)
            if r.returncode and not w.rebase_active():
                die(f"git rebase failed in {w.top}:\n{r.stdout}{r.stderr}")
        rc = settle(w, policy, generated)
        if rc:
            print(f"\nworktree: {w.top}")
            return rc
        if not w.is_ancestor(s["onto"], s["branch"]):
            die(f"{s['branch']} does not sit on {s['parent']} after its rebase; "
                f"inspect {w.top}, then `nu rb abort` or finish by hand")
        if not s["worktree"]:
            restore_home(w, state["home_ref"])
        s["done"] = True
        save_state(g, state)

    record(g, state)
    g.active.unlink(missing_ok=True)
    for title, kind in (("replayed from a previous resolution (rerere cache)", "rerere"),
                        ("auto-resolved", "auto"),
                        ("generated files, resolved mechanically", "regen")):
        lines = journal(g, kind)
        if lines:
            print(f"{title}:")
            for line in lines:
                print(f"  {line}")
    owed = driver_resolved(g) + journal(g, "regen", with_reason=False)
    if owed:
        print("regenerate before pushing:")
        for path in owed:
            print(f"  {path}")
        for cmd in regen_commands(policy, owed):
            print(f"  $ {cmd}")
    print("\nrebase complete.")
    return do_verify(g, args)


def do_verify(g: Git, args) -> int:
    steps = [s for s in last_state(g).get("steps", []) if s.get("onto")]
    if not steps:
        die("no recorded run to verify")
    print("\nverification")
    bad = False
    for s in steps:
        b, old = s["branch"], s["old_sha"]
        new = g.sha(f"refs/heads/{b}")
        # Compare what was replayed: commits below the cut (a squash-merged
        # parent, an amended parent's old version) are not the branch's own.
        old_rng, new_rng = f"{s['cut']}..{old}", f"{s['onto']}..{new}"
        n_old, n_new = g.count(old_rng), g.count(new_rng)
        # A high creation factor makes range-diff pair commits even when the
        # propagated change rewrote much of them; unpaired lines then really mean
        # a commit was dropped or invented.
        rd = g.out("range-diff", "--no-color", "--creation-factor=100", old_rng, new_rng)
        unpaired = [l for l in rd.splitlines() if re.search(r"(<\s+-:|-:\s+-+\s+>)", l)]
        print(f"  {b}: {g.short(old)} -> {g.short(new)}  commits {n_old} -> {n_new}"
              + ("  COMMIT COUNT CHANGED" if n_old != n_new else ""))
        if n_old != n_new or unpaired:
            bad = True
        for line in unpaired:
            print(f"      unpaired: {line.strip()}")
    print("\nfull diff of any branch:  git range-diff refs/stack-backup/<branch>...<branch>")
    if bad:
        print("commits were dropped, invented, or renumbered: confirm before pushing.")
        return 10
    return 0


def do_abort(g: Git, args) -> int:
    state = active_state(g)
    if state is None:
        die("no run in progress")
    started = [s for s in state["steps"] if s.get("onto")]
    for path in dict.fromkeys(s["worktree"] or state["home"] for s in started):
        w = Git(path)
        if w.rebase_active():
            w.must("rebase", "--abort")
            print(f"aborted the rebase in {w.top}")
    holders = g.worktrees()
    for s in started:
        b, was = s["branch"], s["old_sha"]
        if g.sha(f"refs/heads/{b}") == was:
            continue
        # A checked-out branch has to move with its worktree, or that worktree's
        # index and files are left describing the rebased commit.
        if b in holders:
            Git(holders[b]).must("reset", "--quiet", "--keep", was)
        else:
            g.must("update-ref", f"refs/heads/{b}", was)
        print(f"restored {b} -> {g.short(was)}")
    if any(not s["worktree"] for s in started):
        restore_home(Git(state["home"]), state["home_ref"])
    g.active.unlink(missing_ok=True)
    return 0


def do_cleanup(g: Git, args) -> int:
    if g.active.exists():
        die("a run is still in progress: finish it or `nu rb abort` first")
    for line in g.out("for-each-ref", "--format=%(refname)",
                      "refs/stack-backup", "refs/stack-backup-prev").splitlines():
        g.must("update-ref", "-d", line)
        print(f"deleted {line}")
    for path in (g.attrs_file, g.state_file, g.driver_log, g.journal):
        if path.exists():
            path.unlink()
            print(f"deleted {path}")
    sides = g.state_dir / "sides"
    if sides.exists():
        for f in sides.iterdir():
            f.unlink()
        sides.rmdir()
    return 0


def do_report(g: Git, args) -> int:
    state = active_state(g)
    if state is None:
        print("no run in progress")
        return 0
    s = next((s for s in state["steps"] if s.get("onto") and not s.get("done")), None)
    w = Git(s["worktree"] or state["home"]) if s else g
    if not w.rebase_active():
        print("no rebase in progress; run again to continue")
        return 0
    policy = load_policy(g)
    _, generated = install_policy(w, policy)
    files = unmerged(w)
    if not files:
        print("no unmerged files; run again to continue")
        return 0
    label = w.out("log", "-1", "--format=%h (%s)", "REBASE_HEAD")
    entries = [classify(w, p, st, policy, generated, label) for p, st in sorted(files.items())]
    driver = driver_resolved(w)
    report(w, entries, driver, regen_commands(policy, driver + list(files)))
    return 10


def main() -> int:
    ap = argparse.ArgumentParser(prog="stack_rebase.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "run", "report", "verify", "abort", "cleanup"])
    ap.add_argument("branches", nargs="*",
                    help="[base] then the stack, bottom first (default: the recorded stack "
                         "of the current branch)")
    ap.add_argument("--base", help="what the stack sits on (default: origin/<default branch>)")
    ap.add_argument("--old-tip", help="cut point below the lowest branch to rebase")
    ap.add_argument("--no-fetch", action="store_true", help="do not fetch the base first")
    ap.add_argument("--no-ticket-guard", action="store_true",
                    help="replay commits tagged with other tickets without refusing")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and check preconditions, change nothing")
    ap.add_argument("--quiet", action="store_true",
                    help="skip the plan echo (the caller already showed it)")
    args = ap.parse_args()

    g = Git()
    if args.command == "plan":
        print_plan(g, plan(g, args))
        return 0
    return {"run": do_run, "report": do_report, "verify": do_verify,
            "abort": do_abort, "cleanup": do_cleanup}[args.command](g, args)


if __name__ == "__main__":
    sys.exit(main())
