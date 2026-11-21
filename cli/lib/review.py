#!/usr/bin/env python3
"""review: prepare a branch or merge request for review, and check the review an agent wrote.

prep    Resolve the target (current branch, branch name, GitLab MR, GitHub PR), fetch
        it, check it out in a worktree next to the repository, and write meta.json and
        diff.patch to the review directory. Prints the size, the simple/complex
        verdict and a first reading order.
check   Validate the review.json an agent wrote against the captured patch, then
        write bundle.json next to it: the review plus what a presenter derives from
        it (finding codes, diff excerpts, fix prompts, IDE links, counts). How the
        review is shown is up to the agent and its harness.

The diff is always taken against the merge request's target branch when there is
one, so a stacked merge request shows only its own change.

Exit codes:
    0   done
    2   review.json is invalid (every error printed on stderr)
    20  precondition failed
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

# A review is "complex", and is worth a page where the harness can show one, past any of these.
MAX_FILES = 8
MAX_LINES = 400
MAX_AREAS = 1


TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def die(msg: str, code: int = 20) -> NoReturn:
    print(f"nu: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str) -> None:
    print(f"{paint('33', 'warn:')} {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- shell


def attempt(args: list[str], cwd: Path | None = None, stdin: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True)
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    return p.returncode, p.stdout, p.stderr


def must(args: list[str], cwd: Path | None = None, stdin: str | None = None) -> str:
    rc, out, err = attempt(args, cwd, stdin)
    if rc != 0:
        die(f"`{' '.join(args)}` failed: {(err or out).strip()}")
    return out


def git(repo: Path, *args: str) -> str:
    return must(["git", "-C", str(repo), "-c", "core.quotePath=false", *args])


def git_ok(repo: Path, *args: str) -> bool:
    return attempt(["git", "-C", str(repo), *args])[0] == 0


def rev(repo: Path, ref: str) -> str | None:
    rc, out, _ = attempt(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return out.strip() if rc == 0 else None


# --------------------------------------------------------------------------- repo


REMOTE_PATTERNS = (
    re.compile(r"^(?:ssh://)?[^@/\s]+@(?P<host>[^:/]+)(?::\d+)?[:/](?P<path>.+?)(?:\.git)?/?$"),
    re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"),
)


def origin(main: Path) -> tuple[str, str, str | None]:
    """(host, project path, forge) of the origin remote; forge is gitlab, github or None."""
    rc, out, _ = attempt(["git", "-C", str(main), "remote", "get-url", "origin"])
    if rc != 0:
        die("the repository has no 'origin' remote")
    url = out.strip()
    for pattern in REMOTE_PATTERNS:
        m = pattern.match(url)
        if m:
            host, path = m["host"].lower(), m["path"]
            forge = "github" if "github" in host else "gitlab" if "gitlab" in host else None
            return host, path, forge
    return "", url, None


def main_worktree() -> Path:
    if not git_ok(Path.cwd(), "rev-parse", "--is-inside-work-tree"):
        die("not inside a git repository")
    out = git(Path.cwd(), "worktree", "list", "--porcelain")
    return Path(out.split("\n", 1)[0].removeprefix("worktree "))


def worktree_for(main: Path, branch: str) -> Path | None:
    path = None
    for line in git(main, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[9:])
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def default_branch(main: Path) -> str:
    rc, out, _ = attempt(["git", "-C", str(main), "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if rc == 0:
        return out.strip().removeprefix("refs/remotes/origin/")
    for candidate in ("main", "master"):
        if rev(main, f"origin/{candidate}") or rev(main, candidate):
            return candidate
    die("cannot determine the default branch")


def clean(worktree: Path) -> bool:
    return not git(worktree, "status", "--porcelain", "--untracked-files=no").strip()


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", a, b)


# --------------------------------------------------------------------------- forges


MR_URL = re.compile(r"^https?://(?P<host>[^/]+)/(?P<path>.+?)/-/merge_requests/(?P<n>\d+)")
PR_URL = re.compile(r"^https?://(?P<host>[^/]*github[^/]*)/(?P<path>[^/]+/[^/]+)/pull/(?P<n>\d+)")
NUMBER = re.compile(r"^[!#]?(?P<n>\d+)$")

GH_FIELDS = "number,title,body,url,author,isDraft,state,headRefName,baseRefName,headRefOid,isCrossRepository"


def from_gitlab(d: dict) -> dict:
    return {
        "forge": "gitlab",
        "number": d["iid"],
        "ref": f"!{d['iid']}",
        "url": d.get("web_url"),
        "title": d.get("title") or "",
        "description": d.get("description") or "",
        "author": (d.get("author") or {}).get("username"),
        "draft": bool(d.get("draft")),
        "state": d.get("state"),
        "source_branch": d["source_branch"],
        "target_branch": d["target_branch"],
        "head_sha": d.get("sha"),
        "cross_repo": d.get("source_project_id") != d.get("target_project_id"),
    }


def from_github(d: dict) -> dict:
    return {
        "forge": "github",
        "number": d["number"],
        "ref": f"#{d['number']}",
        "url": d.get("url"),
        "title": d.get("title") or "",
        "description": d.get("body") or "",
        "author": (d.get("author") or {}).get("login"),
        "draft": bool(d.get("isDraft")),
        "state": (d.get("state") or "").lower(),
        "source_branch": d["headRefName"],
        "target_branch": d["baseRefName"],
        "head_sha": d.get("headRefOid"),
        "cross_repo": bool(d.get("isCrossRepository")),
    }


def fetch_mr(forge: str, project: str, number: int) -> dict:
    if forge == "gitlab":
        out = must(["glab", "mr", "view", str(number), "-R", project, "--output", "json"])
        return from_gitlab(json.loads(out))
    out = must(["gh", "pr", "view", str(number), "-R", project, "--json", GH_FIELDS])
    return from_github(json.loads(out))


def find_mr(forge: str | None, project: str, branch: str) -> dict | None:
    """The open merge request whose source is `branch`, or None. Never fatal."""
    if forge == "gitlab":
        cmd = ["glab", "mr", "list", "--source-branch", branch, "-R", project, "--output", "json"]
    elif forge == "github":
        cmd = ["gh", "pr", "list", "--head", branch, "-R", project, "--json", GH_FIELDS, "--limit", "1"]
    else:
        return None
    rc, out, err = attempt(cmd)
    if rc != 0:
        warn(f"could not look up a merge request for {branch}: {(err or out).strip()}")
        return None
    try:
        found = json.loads(out or "[]")
    except json.JSONDecodeError:
        warn(f"unexpected output from {cmd[0]}; reviewing without a merge request")
        return None
    if not found:
        return None
    return from_gitlab(found[0]) if forge == "gitlab" else from_github(found[0])


# --------------------------------------------------------------------------- classification


LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "bun.lockb", "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "Gemfile.lock",
    "composer.lock", "pubspec.lock", "go.sum", "gradle.lockfile", "verification-metadata.xml",
}

# Patterns match against "/" + path, and fnmatch's * spans directories.
NOISE = {
    "i18n": ("*/i18n/*.json", "*.arb", "*/l10n/*", "*/locales/*.json"),
    "changelog": ("/.changelog/*", "/CHANGELOG*"),
    "generated": ("*/generated/*", "*/gen/*", "*.g.dart", "*.freezed.dart", "*.gr.dart",
                  "*.pb.go", "*_pb2.py", "*/__snapshots__/*", "*.snap", "*.min.js", "*.min.css"),
}
KINDS = (
    ("test", ("*Test.kt", "*Tests.kt", "*IT.kt", "*Test.java", "*Tests.java", "*IT.java",
              "*.spec.ts", "*.test.ts", "*.spec.tsx", "*.test.tsx", "*.spec.js", "*.test.js",
              "*_test.dart", "*_test.go", "*/test_*.py", "*_test.py", "*/src/test/*",
              "*/src/integrationTest/*", "*/src/testFixtures/*", "*/e2e/*", "*/tests/*",
              "*/test/*", "*/__tests__/*")),
    ("migration", ("*/db/migration/*", "*/migrations/*", "*.sql")),
    ("spec", ("*/api/*.yaml", "*/api/*.yml", "*/api/*.json", "*openapi*.yaml", "*openapi*.yml",
              "*openapi*.json", "*.proto", "*.graphql", "*.graphqls")),
    ("config", ("*.gradle", "*.gradle.kts", "*/gradle.properties", "*/package.json", "*Makefile",
                "*Dockerfile*", "*.gitlab-ci.yml", "*/application*.yml", "*/application*.yaml",
                "*/application*.properties", "*/values*.yaml", "*/Chart.yaml", "*/tsconfig*.json",
                "*/project.json", "*/nx.json", "*/angular.json", "*.toml", "*/pubspec.yaml",
                "/.github/*", "*/install.sh")),
    ("docs", ("*.md", "*.mdx", "*.adoc", "*/docs/*")),
)
ENTRY = ("*Controller.kt", "*Controller.java", "*Delegate*.kt", "*Resource.kt", "*Handler.kt",
         "*Listener.kt", "*Consumer.kt", "*Job.kt", "*Scheduler.kt", "*/adapter/incoming/*",
         "*.component.ts", "*.page.ts", "*.routes.ts", "*routing*.ts", "*/main.ts", "*/main.dart",
         "*/main.py", "*/__main__.py", "*/bin/*", "*/libexec/*", "*/cmd/*", "*_screen.dart",
         "*_page.dart", "*/router*.dart")
OUTBOUND = ("*Repository.kt", "*Repository.java", "*/persistence/*", "*/adapter/outgoing/*",
            "*Entity.kt", "*Dao.kt", "*.store.ts", "*.reducer.ts", "*.effects.ts", "*/client/*")

READING_ORDER = (
    ("Contracts", ("spec",)),
    ("Entry points", ("entry",)),
    ("Core logic", ("source",)),
    ("Persistence and outbound", ("outbound", "migration")),
    ("Wiring and config", ("config",)),
    ("Tests", ("test",)),
    ("Docs", ("docs",)),
    ("Skim or skip", ("lockfile", "i18n", "changelog", "generated")),
)


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    p = "/" + path
    return any(fnmatch.fnmatchcase(p, pat) for pat in patterns)


def classify(path: str, generated: set[str]) -> str:
    if Path(path).name in LOCKFILES:
        return "lockfile"
    if path in generated:
        return "generated"
    for kind, patterns in NOISE.items():
        if matches(path, patterns):
            return kind
    for kind, patterns in KINDS:
        if matches(path, patterns):
            return kind
    if matches(path, ENTRY):
        return "entry"
    if matches(path, OUTBOUND):
        return "outbound"
    return "source"


NOISE_KINDS = {"lockfile", "i18n", "changelog", "generated"}


def declared_generated(worktree: Path, paths: list[str]) -> set[str]:
    """Paths the repository marks generated in .gitattributes."""
    if not paths:
        return set()
    out = must(["git", "-C", str(worktree), "check-attr", "-z", "--stdin",
                "linguist-generated", "gitlab-generated"], stdin="\0".join(paths) + "\0")
    toks = out.split("\0")
    found = set()
    for i in range(0, len(toks) - 2, 3):
        if toks[i + 2] in ("set", "true"):
            found.add(toks[i])
    return found


def area(path: str) -> str | None:
    head, sep, _ = path.partition("/")
    return head if sep and not head.startswith(".") else None


# --------------------------------------------------------------------------- prep


def review_dir(repo_name: str, branch: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "nu" / "rv" / repo_name / branch.replace("/", "-")


def ticket_of(*texts: str) -> str | None:
    for text in texts:
        m = re.match(r"^([a-z]{2,10})-(\d+)(?:-|$)", text or "", re.I)
        if m:
            return f"{m[1].upper()}-{m[2]}"
    for text in texts:
        m = re.search(r"\b([A-Z]{2,10}-\d+)\b", text or "")
        if m:
            return m[1]
    return None


def parse_target(arg: str | None, host: str, project: str, forge: str | None) -> tuple[str, object]:
    """('current', None) | ('branch', name) | ('mr', (forge, number))."""
    if not arg:
        return "current", None
    for pattern, kind in ((MR_URL, "gitlab"), (PR_URL, "github")):
        m = pattern.match(arg)
        if m:
            if m["host"].lower() != host or m["path"].lower() != project.lower():
                die(f"{arg} belongs to {m['host']}/{m['path']}, but origin is {host}/{project}; "
                    "run this from a checkout of that repository")
            return "mr", (kind, int(m["n"]))
    m = NUMBER.match(arg)
    if m:
        if not forge:
            die(f"'{arg}' looks like a merge request number, but origin is not GitLab or GitHub")
        return "mr", (forge, int(m["n"]))
    return "branch", arg.removeprefix("origin/")


def fetch(main: Path, refspecs: list[str]) -> None:
    if refspecs:
        print(paint("2", f"fetching {', '.join(r.split(':')[0].removeprefix('+') for r in refspecs)}"),
              file=sys.stderr)
        git(main, "fetch", "--quiet", "--no-tags", "origin", *refspecs)


def tracking(branch: str) -> str:
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


def sync_branch(main: Path, branch: str, want: str | None, notes: list[str]) -> Path:
    """Put `branch` at `want` (fast-forward only) and return the worktree that holds it."""
    path = worktree_for(main, branch)
    local = rev(main, f"refs/heads/{branch}")

    if want and local and local != want:
        if is_ancestor(main, local, want):
            if path is None:
                git(main, "branch", "-f", branch, want)
            elif clean(path):
                git(path, "merge", "--ff-only", "--quiet", want)
            else:
                notes.append(f"{path.name} has uncommitted changes, so it was not updated to "
                             f"{want[:9]}; reviewing the local branch")
        elif is_ancestor(main, want, local):
            notes.append(f"local {branch} is ahead of the remote; reviewing the local branch")
        else:
            notes.append(f"local {branch} and the remote have diverged; reviewing the local branch")
    elif want and not local and not rev(main, f"refs/remotes/origin/{branch}"):
        git(main, "branch", branch, want)
    elif not local and not rev(main, f"refs/remotes/origin/{branch}"):
        die(f"no local or fetched copy of {branch}; drop --no-fetch")

    if path is None:
        libexec = os.environ.get("NU_LIBEXEC")
        if not libexec:
            die("run this through `nu rv`; it needs `nu wt` to create the worktree")
        rc, out, err = attempt([f"{libexec}/nu-wt", "add", "--no-cd", branch], cwd=main)
        # Checkout progress is tens of thousands of \r-separated updates; an agent reads all of it.
        kept = [l for l in re.split(r"[\r\n]+", out + err) if l.strip() and not l.startswith("Updating files:")]
        print("\n".join(kept), file=sys.stderr)
        if rc != 0:
            die(f"nu wt add {branch} failed")
        path = worktree_for(main, branch)
        if path is None:
            die(f"nu wt add did not produce a worktree for {branch}")
    return path


def numstat(repo: Path, base: str, head: str) -> list[dict]:
    status: dict[str, str] = {}
    toks = git(repo, "diff", "--name-status", "-z", "-M", base, head).split("\0")
    i = 0
    while i < len(toks) and toks[i]:
        letter = toks[i][0]
        if letter in "RC":
            status[toks[i + 2]] = letter
            i += 3
        else:
            status[toks[i + 1]] = letter
            i += 2

    files = []
    toks = git(repo, "diff", "--numstat", "-z", "-M", base, head).split("\0")
    i = 0
    while i < len(toks) and toks[i]:
        added, deleted, path = toks[i].split("\t", 2)
        if path:
            old = path
            i += 1
        else:
            old, path = toks[i + 1], toks[i + 2]
            i += 3
        binary = added == "-"
        files.append({
            "path": path,
            "old_path": old if old != path else None,
            "status": status.get(path, "M"),
            "added": 0 if binary else int(added),
            "deleted": 0 if binary else int(deleted),
            "binary": binary,
        })
    return files


def prep(args: argparse.Namespace) -> int:
    main = main_worktree()
    host, project, forge = origin(main)
    kind, value = parse_target(args.target, host, project, forge)
    notes: list[str] = []
    refspecs: list[str] = []

    mr = None
    want = None
    if kind == "current":
        worktree = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        branch = git(worktree, "branch", "--show-current").strip()
        if not branch:
            die("HEAD is detached; name the branch or merge request to review")
        mr = find_mr(forge, project, branch)
    elif kind == "branch":
        branch = str(value)
        mr = find_mr(forge, project, branch)
        if not (mr and mr["cross_repo"]):
            remote_has = attempt(["git", "-C", str(main), "ls-remote", "--exit-code", "--heads",
                                  "origin", branch])[0] == 0 if not args.no_fetch else False
            if remote_has:
                refspecs.append(tracking(branch))
            elif not rev(main, f"refs/heads/{branch}") and not rev(main, f"refs/remotes/origin/{branch}"):
                die(f"no branch '{branch}' locally or on origin")
    else:
        mr_forge, number = value  # type: ignore[misc]
        mr = fetch_mr(mr_forge, project, number)
        branch = mr["source_branch"]
        if mr["cross_repo"]:
            branch = f"{'mr' if mr_forge == 'gitlab' else 'pr'}-{number}-{branch}"
        else:
            refspecs.append(tracking(mr["source_branch"]))

    if args.base:
        base_ref = args.base
    elif mr:
        base_ref = f"origin/{mr['target_branch']}"
    else:
        parent = attempt(["git", "-C", str(main), "config", f"branch.{branch}.nuParent"])[1].strip()
        base_ref = parent if parent and rev(main, f"refs/heads/{parent}") else f"origin/{default_branch(main)}"
    if base_ref.startswith("origin/"):
        refspecs.append(tracking(base_ref.removeprefix("origin/")))

    if not args.no_fetch:
        fetch(main, refspecs)
        if mr and mr["cross_repo"]:
            pull_ref = (f"refs/merge-requests/{mr['number']}/head" if mr["forge"] == "gitlab"
                        else f"refs/pull/{mr['number']}/head")
            git(main, "fetch", "--quiet", "--no-tags", "origin", pull_ref)
            want = rev(main, "FETCH_HEAD")

    if kind == "current":
        if git(worktree, "status", "--porcelain", "--untracked-files=no").strip():
            notes.append("uncommitted changes are not part of the review")
    else:
        if want is None and not (mr and mr["cross_repo"]):
            want = rev(main, f"refs/remotes/origin/{mr['source_branch'] if mr else branch}")
        worktree = sync_branch(main, branch, want, notes)

    base_sha = rev(main, base_ref) or die(f"base '{base_ref}' does not resolve to a commit")
    head_sha = rev(worktree, "HEAD") or die(f"{worktree} has no HEAD")
    merge_base = git(main, "merge-base", base_sha, head_sha).strip() or die(
        f"{branch} shares no history with {base_ref}")
    if merge_base == head_sha:
        die(f"{branch} has no commits that are not in {base_ref}; nothing to review")
    if mr and mr["head_sha"] and mr["head_sha"] != head_sha:
        notes.append(f"reviewing {head_sha[:9]}, but {mr['ref']} points at {mr['head_sha'][:9]}")

    files = numstat(main, merge_base, head_sha)
    generated = declared_generated(worktree, [f["path"] for f in files])
    for f in files:
        f["kind"] = classify(f["path"], generated)
        f["noise"] = f["kind"] in NOISE_KINDS

    log = git(main, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", f"{merge_base}..{head_sha}")
    commits = []
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
            commits.append({"sha": sha, "subject": subject, "body": body.strip()})

    reviewed = [f for f in files if not f["noise"]]
    review_lines = sum(f["added"] + f["deleted"] for f in reviewed)
    areas = sorted({a for f in reviewed if f["kind"] != "docs" and (a := area(f["path"]))})
    reasons = []
    if len(reviewed) > MAX_FILES:
        reasons.append(f"{len(reviewed)} files to review (> {MAX_FILES})")
    if review_lines > MAX_LINES:
        reasons.append(f"{review_lines} changed lines (> {MAX_LINES})")
    if len(areas) > MAX_AREAS:
        reasons.append(f"{len(areas)} areas: {', '.join(areas)}")

    order = []
    for title, kinds in READING_ORDER:
        group = sorted((f for f in files if f["kind"] in kinds), key=lambda f: -(f["added"] + f["deleted"]))
        if group:
            order.append({"title": title, "files": [f["path"] for f in group]})

    out_dir = review_dir(main.name, branch)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("review.json", "bundle.json", "page.html", "review.html"):
        (out_dir / stale).unlink(missing_ok=True)
    patch = git(main, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M",
                "--src-prefix=a/", "--dst-prefix=b/", merge_base, head_sha)
    (out_dir / "diff.patch").write_text(patch)

    meta = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": {"name": main.name, "main": str(main), "host": host, "project": project, "forge": forge},
        "worktree": str(worktree),
        "in_place": kind == "current",
        "branch": branch,
        "ticket": ticket_of(mr["source_branch"] if mr else branch, mr["title"] if mr else ""),
        "mr": mr,
        "base": {"ref": base_ref, "sha": base_sha, "merge_base": merge_base},
        "head": {"sha": head_sha},
        "commits": commits,
        "files": files,
        "stats": {
            "files": len(files),
            "added": sum(f["added"] for f in files),
            "deleted": sum(f["deleted"] for f in files),
            "review_files": len(reviewed),
            "review_lines": review_lines,
            "areas": areas,
        },
        "complexity": {"verdict": "complex" if reasons else "simple", "reasons": reasons},
        "reading_order": order,
        "notes": notes,
        "paths": {
            "dir": str(out_dir),
            "meta": str(out_dir / "meta.json"),
            "patch": str(out_dir / "diff.patch"),
            "review_json": str(out_dir / "review.json"),
            "bundle": str(out_dir / "bundle.json"),
            "page": str(out_dir / "page.html"),
        },
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    report(meta)
    cd_file = os.environ.get("NU_CD_FILE")
    if cd_file and not args.no_cd and kind != "current":
        Path(cd_file).write_text(str(worktree) + "\n")
    return 0


def tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def report(meta: dict) -> None:
    mr, s, cx = meta["mr"], meta["stats"], meta["complexity"]
    title = mr["title"] if mr else meta["branch"]
    print(f"{paint('34', '==>')} {paint('1', title)}")
    if mr:
        print(f"    {mr['ref']}  {mr['url']}" + ("  (draft)" if mr["draft"] else ""))
    rows = [
        ("worktree", tilde(meta["worktree"]) + ("  (in place)" if meta["in_place"] else "")),
        ("diff", f"{meta['base']['ref']} ({meta['base']['merge_base'][:9]}) .. {meta['head']['sha'][:9]}, "
                 f"{len(meta['commits'])} commit(s)"),
        ("size", f"{s['files']} files (+{s['added']} -{s['deleted']}); "
                 f"{s['review_files']} to review, {s['review_lines']} lines"
                 + (f"; areas: {', '.join(s['areas'])}" if s["areas"] else "")),
        ("verdict", cx["verdict"] + (f" ({'; '.join(cx['reasons'])})" if cx["reasons"] else "")),
        ("meta", tilde(meta["paths"]["meta"])),
    ]
    for key, value in rows:
        print(f"    {paint('2', f'{key:<9}')}{value}")
    by_path = {f["path"]: f for f in meta["files"]}
    print(f"\n{paint('1', 'reading order')}")
    for group in meta["reading_order"]:
        print(f"  {group['title']}")
        for path in group["files"]:
            f = by_path[path]
            churn = "binary" if f["binary"] else f"+{f['added']} -{f['deleted']}"
            print(f"    {path}  {paint('2', churn)}")
    for note in meta["notes"]:
        warn(note)


# --------------------------------------------------------------------------- patch


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch(text: str) -> dict[str, dict]:
    """Per-file diffs keyed by the path meta.json uses (the old path for deletions)."""
    files: list[dict] = []
    cur: dict | None = None
    hunk: dict | None = None
    old = new = 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = {"path": None, "old_path": None, "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
            m = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old_path"], cur["path"] = m[1], m[2]
            continue
        if cur is None:
            continue
        m = HUNK_RE.match(line)
        if m:
            old, new = int(m[1]), int(m[3])
            hunk = {"header": line, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            if line.startswith("rename from "):
                cur["old_path"] = line[12:]
            elif line.startswith("rename to "):
                cur["path"] = line[10:]
            elif line.startswith("--- a/"):
                cur["old_path"] = line[6:].rstrip("\t")
            elif line.startswith("+++ b/"):
                cur["path"] = line[6:].rstrip("\t")
            elif line.startswith("deleted file mode"):
                cur["deleted"] = True
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                cur["binary"] = True
            continue
        if line.startswith("+"):
            hunk["lines"].append(("add", None, new, line[1:]))
            new += 1
        elif line.startswith("-"):
            hunk["lines"].append(("del", old, None, line[1:]))
            old += 1
        elif line.startswith(" "):
            hunk["lines"].append(("ctx", old, new, line[1:]))
            old += 1
            new += 1
        elif line.startswith("\\"):
            hunk["lines"].append(("note", None, None, line))
    return {(f["old_path"] if f.get("deleted") else f["path"]): f for f in files}


# --------------------------------------------------------------------------- check: validation


SEVERITIES = ("critical", "high", "medium", "low")
SECTIONS = {"bugs": "Bug", "behavior": "Unintended behavior", "standards": "Standards"}
DECISIONS = {"merge": "Safe to merge", "fix": "Merge after fixes", "block": "Do not merge"}
CRITERIA = ("met", "partial", "missing", "unclear")
COMMENT_RE = re.compile(
    r"^(praise|nitpick|suggestion|issue|todo|question|thought|chore|note)"
    r"(?: \((?:blocking|non-blocking|if-minor|[a-z-]+)(?:, ?[a-z-]+)*\))?: \S"
)


def validate(review: dict, meta: dict, diffs: dict[str, dict]) -> list[str]:
    errors: list[str] = []

    def text(obj: dict, key: str, where: str, required: bool = True) -> None:
        value = obj.get(key)
        if value is None and not required:
            return
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{where}.{key}: required non-empty string")

    verdict = review.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("decision") not in DECISIONS:
        errors.append(f"verdict.decision: one of {', '.join(DECISIONS)}")
    else:
        text(verdict, "text", "verdict")
    text(review, "summary", "review")

    clean = review.get("clean")
    if clean is not None and (not isinstance(clean, list)
                              or not all(isinstance(c, str) and c.strip() for c in clean)):
        errors.append("clean: list of non-empty strings")

    intent = review.get("intent")
    if intent is not None:
        if not isinstance(intent, dict) or not isinstance(intent.get("criteria"), list):
            errors.append("intent: object with a criteria list")
        else:
            for i, c in enumerate(intent["criteria"]):
                if not isinstance(c, dict) or c.get("status") not in CRITERIA:
                    errors.append(f"intent.criteria[{i}].status: one of {', '.join(CRITERIA)}")
                else:
                    text(c, "text", f"intent.criteria[{i}]")

    steps = review.get("steps")
    seen: dict[str, int] = {}
    if not isinstance(steps, list) or not steps:
        errors.append("steps: non-empty list")
        steps = []
    for i, s in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: object")
            continue
        text(s, "title", where)
        text(s, "explanation", where)
        text(s, "diagram", where, required=False)
        files = s.get("files")
        if not isinstance(files, list) or not files:
            errors.append(f"{where}.files: non-empty list of changed paths")
            continue
        for path in files:
            if path not in diffs:
                errors.append(f"{where}.files: '{path}' is not a file in the diff")
            elif path in seen:
                errors.append(f"{where}.files: '{path}' is already in steps[{seen[path]}]")
            else:
                seen[path] = i
    noise = {f["path"] for f in meta["files"] if f["noise"]}
    for path in diffs:
        if path not in seen and path not in noise:
            errors.append(f"steps: '{path}' is not in any step")

    findings = review.get("findings")
    if not isinstance(findings, list):
        errors.append("findings: list (empty when nothing was found)")
        findings = []
    for i, f in enumerate(findings):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where}: object")
            continue
        for key in ("title", "body", "where", "why", "comment"):
            text(f, key, where)
        text(f, "fix", where, required=False)
        if f.get("severity") not in SEVERITIES:
            errors.append(f"{where}.severity: one of {', '.join(SEVERITIES)}")
        if f.get("section") not in SECTIONS:
            errors.append(f"{where}.section: one of {', '.join(SECTIONS)}")
        if isinstance(f.get("comment"), str) and not COMMENT_RE.match(f["comment"]):
            errors.append(f"{where}.comment: must start with a Conventional Comments label, "
                          "e.g. 'issue (blocking): ...' or 'suggestion (non-blocking): ...'")
        if f.get("side", "new") not in ("new", "old"):
            errors.append(f"{where}.side: 'new' or 'old'")
        for key in ("line", "end_line"):
            if key in f and (not isinstance(f[key], int) or f[key] < 1):
                errors.append(f"{where}.{key}: positive integer")
        if "line" in f and "file" not in f:
            errors.append(f"{where}.line: needs file")
        if "end_line" in f and isinstance(f.get("line"), int) and isinstance(f["end_line"], int) \
                and f["end_line"] < f["line"]:
            errors.append(f"{where}.end_line: must not be before line")
        if "file" in f and f["file"] not in diffs \
                and not (Path(meta["worktree"]) / str(f["file"])).exists():
            errors.append(f"{where}.file: '{f['file']}' is neither in the diff nor in the worktree")
    return errors


# --------------------------------------------------------------------------- check: bundle


EXCERPT_CONTEXT = 3


JVM = {".kt", ".kts", ".java", ".gradle", ".groovy", ".scala"}
WEB = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".scss", ".css", ".vue"}


def ide_prefix(worktree: Path, path: str) -> str:
    """URL that opens `path` in the right IDE once a line number is appended."""
    full = worktree / path
    ext = full.suffix
    ide = "idea" if ext in JVM else "vscode" if ext in WEB else None
    d = full.parent
    while ide is None:
        if any((d / m).exists() for m in ("build.gradle.kts", "build.gradle", "settings.gradle.kts",
                                          "settings.gradle", "pom.xml")):
            ide = "idea"
        elif (d / "package.json").exists() or (d / "tsconfig.json").exists() or d == worktree \
                or d == d.parent:
            ide = "vscode"
        d = d.parent
    if ide == "idea":
        return f"idea://open?file={quote(str(full))}&line="
    return f"vscode://file{quote(str(full))}:"


def finding_codes(findings: list[dict]) -> list[str]:
    """C1, H1, H2, M1, …: the severity's letter and its count among the findings of that severity."""
    seen = dict.fromkeys(SEVERITIES, 0)
    codes = []
    for f in findings:
        seen[f["severity"]] += 1
        codes.append(f"{f['severity'][0].upper()}{seen[f['severity']]}")
    return codes


def fix_prompt(code: str, f: dict, meta: dict, review_path: Path) -> str:
    """A prompt that lets an agent with no other context fix this one finding."""
    where = f["where"]
    if f.get("file"):
        loc = f["file"]
        if "line" in f:
            end = f.get("end_line", f["line"])
            loc += f", line {f['line']}" if end == f["line"] else f", lines {f['line']}-{end}"
            if f.get("side") == "old":
                loc += " of the old file"
        where += f" ({loc})"
    lines = [
        f"Fix review finding {code} in the worktree {meta['worktree']} (branch {meta['branch']}).",
        "",
        f"{code}: {f['title']} [{f['severity'].title()}, {SECTIONS[f['section']]}]",
        f["body"],
        f"Where: {where}",
        f"Why: {f['why']}",
    ]
    if f.get("fix"):
        lines.append(f"Fix: {f['fix']}")
    lines += ["", f"The full review is in {review_path}. Fix only this finding, in that worktree. Do not push."]
    return "\n".join(lines)


def on_side(kind: str, side: str) -> bool:
    return kind != "del" if side == "new" else kind != "add"


def excerpt(f: dict, diff: dict | None) -> dict | None:
    """The diff rows of a finding's lines plus some context, when the diff displays its last line."""
    if diff is None or "line" not in f:
        return None
    side = f.get("side", "new")
    first, last = f["line"], f.get("end_line", f["line"])
    for h in diff["hunks"]:
        rows = h["lines"]
        number = [(n if side == "new" else o) if on_side(kind, side) else None for kind, o, n, _ in rows]
        if last not in number:
            continue
        hits = {i for i, x in enumerate(number) if x is not None and first <= x <= last}
        lo = max(0, min(hits) - EXCERPT_CONTEXT)
        hi = min(len(rows), max(hits) + EXCERPT_CONTEXT + 1)
        return {
            "side": side,
            "hunk": h["header"],
            "rows": [{"kind": kind, "old": o, "new": n, "text": text, "hit": i in hits}
                     for i, (kind, o, n, text) in enumerate(rows[lo:hi], start=lo)],
        }
    return None


def build_bundle(review: dict, meta: dict, diffs: dict[str, dict], review_path: Path) -> tuple[dict, list[str]]:
    """Everything a presenter needs, derived once: codes, excerpts, fix prompts, IDE links, counts."""
    worktree = Path(meta["worktree"])
    files_meta = {f["path"]: f for f in meta["files"]}
    findings = review["findings"]
    notes = []

    ide_cache: dict[str, str | None] = {}

    def ide(path: str) -> str | None:
        if path not in ide_cache:
            gone = files_meta.get(path, {}).get("status") == "D" or not (worktree / path).exists()
            ide_cache[path] = None if gone else ide_prefix(worktree, path)
        return ide_cache[path]

    codes = finding_codes(findings)
    out_findings = []
    for i, f in enumerate(findings):
        path = f.get("file")
        diff = diffs.get(path) if path else None
        ex = excerpt(f, diff)
        placement = "diff" if ex else "file" if diff is not None else "outside"
        if placement == "file" and "line" in f:
            notes.append(f"finding {codes[i]} ({path}, line {f['line']}) is not on a line the diff shows")
        prefix = ide(path) if path else None
        out_findings.append({
            "code": codes[i],
            **f,
            "side": f.get("side", "new") if "line" in f else None,
            "placement": placement,
            "excerpt": ex,
            "ide_url": prefix + str(f.get("line", 1)) if prefix else None,
            "fix_prompt": fix_prompt(codes[i], f, meta, review_path),
        })

    steps = [dict(s) for s in review["steps"]]
    covered = {p for s in steps for p in s["files"]}
    rest = [p for p in diffs if p not in covered]
    if rest:
        steps.append({"title": "Generated files, lockfiles, translations and changelog",
                      "explanation": "Listed for completeness. Nothing here was written by hand.",
                      "files": rest, "noise": True})
    by_file: dict[str, list[str]] = {}
    for f in out_findings:
        if f.get("file"):
            by_file.setdefault(f["file"], []).append(f["code"])
    out_steps = []
    for s in steps:
        files = []
        for path in s["files"]:
            m = files_meta[path]
            prefix = ide(path)
            files.append({
                "path": path,
                "old_path": m.get("old_path"),
                "status": m["status"],
                "kind": m["kind"],
                "noise": m["noise"],
                "added": m["added"],
                "deleted": m["deleted"],
                "binary": m["binary"],
                "ide_url": prefix + "1" if prefix else None,
                "findings": by_file.get(path, []),
            })
        out_steps.append({
            "title": s["title"],
            "explanation": s["explanation"],
            "diagram": s.get("diagram"),
            "noise": bool(s.get("noise")),
            "files": files,
        })

    bundle = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": meta["repo"],
        "worktree": meta["worktree"],
        "branch": meta["branch"],
        "ticket": meta.get("ticket"),
        "mr": meta["mr"],
        "base": meta["base"],
        "head": meta["head"],
        "commits": len(meta["commits"]),
        "stats": meta["stats"],
        "complexity": meta["complexity"],
        "notes": meta.get("notes", []),
        "verdict": review["verdict"],
        "summary": review["summary"],
        "intent": review.get("intent"),
        "clean": review.get("clean", []),
        "counts": {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES},
        "findings": out_findings,
        "steps": out_steps,
        "paths": {**meta["paths"], "review_json": str(review_path)},
    }
    return bundle, notes


def check(args: argparse.Namespace) -> int:
    review_path = Path(args.review).resolve()
    out_dir = review_path.parent
    meta_path, patch_path = out_dir / "meta.json", out_dir / "diff.patch"
    if not meta_path.exists() or not patch_path.exists():
        die(f"{out_dir} has no meta.json and diff.patch; run `nu rv prep` first")
    try:
        review = json.loads(review_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"{review_path}: {e}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text())
    diffs = parse_patch(patch_path.read_text())
    missing = {f["path"] for f in meta["files"]} - set(diffs)
    if missing:
        die(f"diff.patch and meta.json disagree on {', '.join(sorted(missing))}; run `nu rv prep` again")

    errors = validate(review, meta, diffs) if isinstance(review, dict) else ["review: JSON object"]
    if errors:
        print(f"{review_path} is invalid:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 2

    bundle, notes = build_bundle(review, meta, diffs, review_path)
    out = out_dir / "bundle.json"
    out.write_text(json.dumps(bundle, indent=2) + "\n")
    for note in notes:
        warn(note)
    print(out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nu rv")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("target", nargs="?")
    p.add_argument("--base")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--no-cd", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("review")
    args = parser.parse_args()
    return prep(args) if args.cmd == "prep" else check(args)


if __name__ == "__main__":
    sys.exit(main())
