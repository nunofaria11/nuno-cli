#!/usr/bin/env python3
"""stack-rebase: deterministic engine for rebasing a stack of dependent branches.

The mechanical parts of a stacked rebase (chain discovery, --onto arithmetic, ref
bookkeeping, replay of known resolutions, generated-file classes, line-disjoint
three-way merges) are decided here. Only genuine semantic conflicts are handed
back to the caller.

Exit codes:
    0   nothing left to do (stack rebased, or already up to date)
    10  paused: conflicts need a judgement call (report printed on stdout)
    20  precondition failed / operator input required

All git behaviour tweaks are passed per invocation with `git -c`, plus a private
attributes file under $GIT_COMMON_DIR/stack-rebase/. Nothing in the repository
configuration or working tree is mutated, so a crashed run leaves no footprint
beyond the rerere cache and our own state directory.
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

DRIVER = "stack-take-theirs"
STATE_VERSION = 1
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
    def __init__(self) -> None:
        self.top = self._bare(["rev-parse", "--show-toplevel"])
        self.common = Path(
            self._bare(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        )
        self.gitdir = Path(self._bare(["rev-parse", "--path-format=absolute", "--git-dir"]))
        self.state_dir = self.common / "stack-rebase"
        self.attrs_file = self.state_dir / "attributes"
        self.state_file = self.state_dir / "state.json"
        self.driver_log = self.state_dir / "driver.log"
        # A run can span several processes (pause, resolve, resume), so what was
        # resolved and what owes a regeneration has to outlive one of them.
        self.journal = self.state_dir / "journal.tsv"

    # -- plumbing ----------------------------------------------------------
    def _bare(self, args: list[str]) -> str:
        p = subprocess.run(["git", *args], capture_output=True, text=True)
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
            die(f"git {' '.join(args)}\n{p.stdout}{p.stderr}")
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

    def default_base(self) -> str:
        head = self.out("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if head:
            name = head.rsplit("/", 1)[-1]
            if self.sha(name):
                return name
        for cand in ("master", "main"):
            if self.sha(cand):
                return cand
        die("cannot determine the default base branch; pass --base")

    def worktree_of(self, branch: str) -> str | None:
        path = None
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line == f"branch refs/heads/{branch}":
                return path
        return None


def die(msg: str, code: int = 20) -> None:
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


def reflog_shas(g: Git, ref: str) -> list[str]:
    seen: list[str] = []
    for line in g.out("reflog", "show", "--format=%H", ref).splitlines():
        sha = line.strip()
        if sha and sha not in seen:
            seen.append(sha)
    return seen


def reflog_anchor(g: Git, ref: str, descendant: str) -> str | None:
    """The most recent tip `ref` had that `descendant` still contains."""
    cur = g.sha(ref)
    for sha in reflog_shas(g, ref):
        if sha != cur and g.is_ancestor(sha, descendant):
            return sha
    return None


def resolve_chain(g: Git, branches: list[str], base: str | None, tip: str | None) -> dict:
    base = base or g.default_base()
    if not g.sha(base):
        die(f"base branch {base!r} does not exist")

    if branches:
        chain = branches
    else:
        tip = tip or g.out("rev-parse", "--abbrev-ref", "HEAD")
        if tip == "HEAD":
            die("detached HEAD: name the stack explicitly (stack_rebase.py plan b1 b2 b3)")
        # A rewritten parent is no longer an ancestor of the tip, so containment
        # alone cannot find it. Anchor each candidate on the newest tip it had
        # that the stack tip still contains.
        cands = [
            b for b in g.out(
                "for-each-ref", "--format=%(refname:short)", "refs/heads"
            ).splitlines()
            if b not in (tip, base) and not g.is_ancestor(b, base)
        ]
        below = []
        for b in cands:
            anchor = g.sha(b) if g.is_ancestor(b, tip) else reflog_anchor(g, b, tip)
            if anchor and g.count(f"{base}..{anchor}") > 0:
                below.append((g.count(f"{base}..{anchor}"), b))
        below.sort()
        chain = [b for _, b in below] + [tip]

    missing = [b for b in chain if not g.sha(b)]
    if missing:
        die(f"unknown branch(es): {', '.join(missing)}")

    steps = []
    for parent, child in zip([base, *chain], chain):
        contained = g.is_ancestor(parent, child)
        anchor = None if contained else reflog_anchor(g, parent, child)
        steps.append({
            "parent": parent,
            "branch": child,
            "sha": g.sha(child),
            "own_commits": g.count(f"{anchor or parent}..{child}"),
            "up_to_date": contained,
        })
    return {"base": base, "chain": chain, "steps": steps}


def derive_old_tip(g: Git, parent: str, child: str, override: str | None) -> tuple[str | None, str]:
    """The tip `parent` had when `child` was built on it."""
    if override:
        sha = g.sha(override)
        if not sha:
            die(f"--old-tip {override!r} does not resolve")
        return sha, "operator supplied"

    cands: list[tuple[int, str, str]] = []
    for ref in (f"refs/stack-backup/{parent}", f"backup/{parent}"):
        sha = g.sha(ref)
        if sha and g.is_ancestor(sha, child):
            cands.append((g.count(f"{sha}..{child}"), sha, f"backup ref {ref}"))
    anchor = reflog_anchor(g, parent, child)
    if anchor:
        cands.append((g.count(f"{anchor}..{child}"), anchor, f"reflog of {parent}"))
    mb = g.out("merge-base", parent, child)
    if mb:
        cands.append((g.count(f"{mb}..{child}"), mb, f"merge-base with {parent}"))
    if not cands:
        return None, "not found"
    # The tightest anchor replays the fewest commits, so it is the correct one.
    n, sha, how = min(cands)
    return sha, f"{how}, replays {n} commit(s)"


def plan(g: Git, args) -> dict:
    p = resolve_chain(g, args.branches, args.base, args.tip)
    first = next((s for s in p["steps"] if not s["up_to_date"]), None)
    if first is None:
        p["action"] = None
        return p
    old, how = derive_old_tip(g, first["parent"], first["branch"], args.old_tip)
    p["action"] = {
        "onto": first["parent"],
        "onto_sha": g.sha(first["parent"]),
        "old_tip": old,
        "old_tip_source": how,
        "tip": p["chain"][-1],
        "moves": [s["branch"] for s in p["steps"] if not s["up_to_date"] or
                  p["chain"].index(s["branch"]) > p["chain"].index(first["branch"])],
    }
    return p


def print_plan(g: Git, p: dict) -> None:
    print(f"base: {p['base']}")
    for s in p["steps"]:
        mark = "ok " if s["up_to_date"] else "!! "
        print(f"  {mark}{s['parent']} -> {s['branch']} "
              f"({s['own_commits']} commit(s), {g.short(s['sha'])})"
              + ("" if s["up_to_date"] else "  <- diverged: parent was rewritten"))
    a = p["action"]
    if not a:
        print("\nnothing to do: every branch already contains its parent")
        return
    print(f"\none-pass rebase, all intermediate refs move with --update-refs:")
    print(f"  git rebase --onto {a['onto']} {g.short(a['old_tip']) if a['old_tip'] else '<OLD-TIP>'} "
          f"{a['tip']} --update-refs")
    print(f"  onto      {a['onto']} @ {g.short(a['onto_sha'])}")
    print(f"  old tip   {g.short(a['old_tip']) if a['old_tip'] else 'UNKNOWN'} "
          f"({a['old_tip_source']})")
    print(f"  moves     {', '.join(a['moves'])}")


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


def save_state(g: Git, p: dict) -> None:
    g.state_dir.mkdir(parents=True, exist_ok=True)
    g.state_file.write_text(json.dumps({"version": STATE_VERSION, "started": time.time(), **p}, indent=1))


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


def preconditions(g: Git, p: dict) -> None:
    dirty = g.out("status", "--porcelain", "--untracked-files=no")
    if dirty:
        die("working tree is not clean:\n" + dirty)
    for b in p["chain"]:
        wt = g.worktree_of(b)
        if wt and Path(wt).resolve() != Path(g.top).resolve():
            die(f"branch {b} is checked out in another worktree ({wt}); "
                f"run stack-rebase from there")


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
    print("\nResolve in the working tree, `git add` each path, then run the rebase again.")


def note_replays(g: Git, p: subprocess.CompletedProcess) -> None:
    """Record the paths git resolved from the rerere cache rather than by merging."""
    for m in RERERE_RE.finditer(p.stdout + p.stderr):
        journal_add(g, "rerere", m.group("path"))


def do_run(g: Git, args) -> int:
    policy = load_policy(g)
    _, generated = install_policy(g, policy)

    if not g.rebase_active():
        p = plan(g, args)
        if not p["action"]:
            print_plan(g, p)
            return 0
        a = p["action"]
        if not a["old_tip"]:
            print_plan(g, p)
            die(f"cannot derive the old tip of {a['onto']}; pass --old-tip <sha> "
                f"(see `git reflog show {a['onto']}`)")
        preconditions(g, p)
        made = backup(g, p["chain"])
        save_state(g, p)
        if not args.quiet:
            print_plan(g, p)
            if made:
                print("\nbackups: " + ", ".join(made))
        if args.dry_run:
            return 0
        g.driver_log.unlink(missing_ok=True)
        g.journal.unlink(missing_ok=True)
        # --empty=stop: a commit whose content already landed below is a decision,
        # not something to drop silently.
        note_replays(g, g.run("rebase", "--onto", a["onto"], a["old_tip"], a["tip"],
                              "--update-refs", "--empty=stop"))

    for _ in range(MAX_STEPS):
        if not g.rebase_active():
            break
        files = unmerged(g)
        if not files:
            if g.sha("REBASE_HEAD") and g.ok("diff-index", "--quiet", "HEAD"):
                print("=" * 72)
                print("PAUSED: this commit is empty here -- its change already "
                      "landed in the history below")
                print(g.out("log", "-1", "--format=%h %s", "REBASE_HEAD"))
                print(g.out("show", "--stat", "--format=", "REBASE_HEAD"))
                print("Confirm, then one of\n"
                      "  git rebase --skip                        # drop the redundant commit\n"
                      "  git commit --allow-empty -C REBASE_HEAD  # keep it as an empty commit\n"
                      "then run the rebase again.")
                return 10
            # rerere replayed it, a driver resolved it, or the caller staged a fix.
            note_replays(g, g.run("rebase", "--continue"))
            continue

        label = g.out("log", "-1", "--format=%h (%s)", "REBASE_HEAD")
        pending: list[dict] = []
        progress = False
        for path, stages in sorted(files.items()):
            e = classify(g, path, stages, policy, generated, label)
            if e["kind"] in ("auto", "regenerate"):
                (Path(g.top) / path).write_text(e["merged"])
                g.must("add", "--", path)
                journal_add(g, "auto" if e["kind"] == "auto" else "regen",
                            path, e["reason"])
                progress = True
                continue
            if e.get("merged"):
                # Narrow the markers to the lines that actually clash.
                (Path(g.top) / path).write_text(e["merged"])
            pending.append(e)
        if pending:
            for title, kind in (("replayed from a previous resolution", "rerere"),
                                ("auto-resolved so far", "auto"),
                                ("generated, resolved but owed a regeneration", "regen")):
                lines = journal(g, kind)
                if lines:
                    print(f"{title}:")
                    for line in lines:
                        print(f"  {line}")
                    print()
            owed = driver_resolved(g) + journal(g, "regen", with_reason=False)
            report(g, pending, owed, regen_commands(policy, owed))
            return 10
        if not progress:
            die("no progress possible; inspect `git status` by hand")

    if g.rebase_active():
        die("rebase still in progress after the step limit; inspect `git status`")

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
    state = json.loads(g.state_file.read_text()) if g.state_file.exists() else {}
    chain = args.branches or state.get("chain") or []
    if not chain:
        die("no recorded stack; pass the branches explicitly")
    print("\nverification")
    bad = False
    for b in chain:
        old = g.sha(f"refs/stack-backup/{b}")
        if not old:
            print(f"  {b}: no backup ref, skipped")
            continue
        new = g.sha(b)
        if old == new:
            print(f"  {b}: unchanged ({g.short(new)})")
            continue
        base = state.get("base", "HEAD")
        n_old, n_new = g.count(f"{base}..{old}"), g.count(f"{base}..{b}")
        # A high creation factor makes range-diff pair commits even when the
        # propagated change rewrote much of them; unpaired lines then really mean
        # a commit was dropped or invented.
        rd = g.out("range-diff", "--no-color", "--creation-factor=100", f"{old}...{b}")
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
    if g.rebase_active():
        print(g.out("rebase", "--abort") or "rebase aborted")
    state = json.loads(g.state_file.read_text()) if g.state_file.exists() else {}
    for s in state.get("steps", []):
        b, was = s["branch"], s["sha"]
        if g.sha(b) != was:
            g.must("update-ref", f"refs/heads/{b}", was)
            print(f"restored {b} -> {g.short(was)}")
    return 0


def do_cleanup(g: Git, args) -> int:
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
    if not g.rebase_active():
        print("no rebase in progress")
        return 0
    policy = load_policy(g)
    _, generated = install_policy(g, policy)
    files = unmerged(g)
    if not files:
        print("no unmerged files; rerun `run` to continue")
        return 0
    label = g.out("log", "-1", "--format=%h (%s)", "REBASE_HEAD")
    entries = [classify(g, p, s, policy, generated, label) for p, s in sorted(files.items())]
    driver = driver_resolved(g)
    report(g, entries, driver, regen_commands(policy, driver + list(files)))
    return 10


def main() -> int:
    ap = argparse.ArgumentParser(prog="stack_rebase.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "run", "report", "verify", "abort", "cleanup"])
    ap.add_argument("branches", nargs="*", help="stack order, bottom first (default: inferred)")
    ap.add_argument("--base", help="branch the stack forks from (default: origin HEAD)")
    ap.add_argument("--tip", help="top of the stack when inferring (default: current branch)")
    ap.add_argument("--old-tip", help="pre-rewrite tip of the diverged parent")
    ap.add_argument("--dry-run", action="store_true", help="plan, back up, but do not rebase")
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
