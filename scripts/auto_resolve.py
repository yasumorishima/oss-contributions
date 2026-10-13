"""Bring a long-lived fork PR back to MERGEABLE when upstream moves under it.

Only conflicts of a kind already resolved by hand several times are handled, with the same steps;
anything else is reported in a tracker issue and nothing is pushed. The merge goes to a candidate
branch first and reaches the PR branch only after the fork's own CI has built it.

    python scripts/auto_resolve.py gyroflow                 # scheduled run
    python scripts/auto_resolve.py gyroflow --dry-run --start-sha <full sha> --upstream-sha <full sha>
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TRACKER = "yasumorishima/oss-contributions"
AUTHOR = ("yasumorishima", "fw_yasu11@hotmail.com")

PROJECTS = {
    "gyroflow": {
        "upstream": "gyroflow/gyroflow",
        "pr": 1143,
        "fork": "yasumorishima/gyroflow",
        "branch": "feature/ai-optical-flow",
        "upstream_branch": "master",
        # Cargo.lock conflicts were resolved by hand on 2026-06-23, 07-29, 08-28, 09-02, 09-05 and
        # 10-12 the same way: take upstream's lock and let cargo add back this PR's optional deps.
        "lockfiles": {"Cargo.lock": ".", "src/core/Cargo.lock": "src/core"},
        # crypto-common 0.1.7 pins generic-array =0.14.7 and pulls upstream's 0.14.9 down; 0.1.6 does
        # not (09-05, 10-12). Applied only when upstream's lock would otherwise lose a version.
        "pins": [("crypto-common", "0.1.6")],
        "must_contain": [("src/core/Cargo.toml", "use-burn"), ("src/controller.rs", "has_ai_optical_flow")],
        "workflow": "release.yml",
        # macOS/iOS jobs stop at "Install Mac certificates" in the fork (no Apple signing secrets).
        "required_jobs": ["Gyroflow for linux", "Gyroflow for windows", "Gyroflow for windows-arm",
                          "Gyroflow for android"],
        "ci_timeout_min": 70,  # runs take 20-35 min; a re-run gets one more hour
        "memo": "oss-gyroflow.md",
    },
    "d2": {
        "upstream": "d2lang/d2",
        "pr": 2742,
        "fork": "yasumorishima/d2",
        "branch": "feat/cycle-border-tracing",
        "upstream_branch": "master",
        # Resolved by hand on 2026-07-29 (txtar.txt), 09-08 and 10-12 (next.md): upstream's file plus
        # the lines this PR added. "section": back under the same changelog heading; "end": at the end.
        # Code conflicts (08-07 d2layouts.go, 09-06 d2svg.go) stop here.
        "append_files": {"ci/release/changelogs/next.md": "section", "e2etests/txtar.txt": "end"},
        "must_contain": [("d2layouts/d2cycle/layout.go", "func clipToShapeBorder"),
                         ("e2etests/txtar.txt", "-- cycle-self-loop --")],
        # CONTRIBUTING: "Please sign your commits"; the `signed` job fails on any unsigned commit.
        "sign": True,
        "workflow": "ci.yml",
        "dispatch": True,  # ci.yml runs on push to master only; it is dispatched on the candidate
        "required_jobs": ["ci", "signed"],
        "ci_timeout_min": 45,  # runs take 7-11 min
        "memo": "oss-d2-1578.md",
    },
}


def sh(cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.rstrip()[-3000:], flush=True)
    if r.stderr.strip():
        print(r.stderr.rstrip()[-3000:], flush=True)
    if check and r.returncode != 0:
        raise Stop(f"`{' '.join(cmd)}` exited {r.returncode}")
    return r


def gh_json(path: str, tries: int = 6) -> dict | list:
    """GitHub's API answers 502/504 now and then (a 502 ended the first dry run); retry those."""
    for i in range(tries):
        r = sh(["gh", "api", path], check=False)
        if r.returncode == 0:
            return json.loads(r.stdout)
        if i == tries - 1 or not re.search(r"HTTP 5\d\d|timeout|connection", r.stderr, re.I):
            raise Stop(f"`gh api {path}` failed: {r.stderr.strip()[-300:]}")
        time.sleep(30 * (i + 1))


class Stop(Exception):
    """Stop without pushing to the PR branch; the message goes to the tracker issue."""


def pr_state(p: dict) -> dict:
    for _ in range(8):  # GitHub computes mergeable lazily; null means "not yet"
        pr = gh_json(f"repos/{p['upstream']}/pulls/{p['pr']}")
        if pr["mergeable"] is not None or pr["state"] != "open":
            return pr
        time.sleep(20)
    return pr


def lock_set(text: str) -> set[tuple[str, str]]:
    return {(x["name"], x["version"]) for x in tomllib.loads(text).get("package", [])}


def vkey(v: str) -> tuple:
    """SemVer order: build metadata ignored, a prerelease sorts below its release."""
    core, _, pre = v.split("+")[0].partition("-")
    rel = tuple(int(x) for x in core.split("."))
    if not pre:
        return rel, 1, ()
    return rel, 0, tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))


def names(out: str) -> set[str]:
    return {x for x in out.split("\0") if x}


def lost(p: dict, work: Path, upstream_sha: str) -> dict[str, list]:
    """Upstream (name, version) pairs missing from our lock, per lockfile."""
    out = {}
    for f in p.get("lockfiles", {}):
        up = lock_set(sh(["git", "show", f"{upstream_sha}:{f}"], cwd=work).stdout)
        ours = lock_set((work / f).read_text())
        out[f] = sorted(up - ours)
    return out


def repair(p: dict, work: Path, missing: dict[str, list]) -> None:
    for f, pairs in missing.items():
        if not pairs:
            continue
        d = work / p["lockfiles"][f]
        ours = lock_set((work / f).read_text())
        for name, want in p["pins"]:
            for n, v in sorted(ours):
                if n == name and v != want:
                    sh(["cargo", "update", "-p", f"{n}@{v}", "--precise", want], cwd=d, check=False)
        ours = lock_set((work / f).read_text())
        for name, want in pairs:
            for n, v in sorted(ours):
                if n == name and vkey(v) < vkey(want):
                    sh(["cargo", "update", "-p", f"{n}@{v}", "--precise", want], cwd=d, check=False)


def split(text: str) -> list[str]:
    return text.split("\n")


def strip_blank(lines: list[str]) -> list[str]:
    i, j = 0, len(lines)
    while i < j and not lines[i].strip():
        i += 1
    while j > i and not lines[j - 1].strip():
        j -= 1
    return lines[i:j]


def added_blocks(f: str, base: list[str], ours: list[str]) -> list[tuple[int, list[str]]]:
    """The lines this PR added to f, as (position in ours, lines); any other change stops."""
    out = []
    for op, i1, _, j1, j2 in difflib.SequenceMatcher(a=base, b=ours, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "insert":
            raise Stop(f"{f}: this PR {op}s existing lines near line {i1 + 1}; only added lines are re-applied")
        block = strip_blank(ours[j1:j2])
        if block:
            out.append((j1, block))
    return out


def reapply(f: str, mode: str, base: str, ours: str, theirs: str) -> str:
    """Upstream's file with this PR's added lines put back, the way the hand merges did it."""
    if any("\r" in x for x in (base, ours, theirs)):
        raise Stop(f"{f}: CRLF line endings; not re-applied")
    o, t = split(ours), split(theirs)
    blocks = added_blocks(f, split(base), o)
    if not blocks:
        raise Stop(f"{f}: conflicted but this PR adds nothing to it")
    for j, block in blocks:
        if any(t[k:k + len(block)] == block for k in range(len(t))):
            raise Stop(f"{f}: upstream already has {block[0]!r}")
        if mode == "section":
            heading = next((o[k] for k in range(j - 1, -1, -1) if o[k].startswith("#")), None)
            if heading is None or heading not in t:
                raise Stop(f"{f}: section {heading!r} of {block[0]!r} is not in upstream's file")
            if o.count(heading) != 1 or t.count(heading) != 1:
                raise Stop(f"{f}: heading {heading!r} is not unique")
            h = t.index(heading)
            n = next((k for k in range(h + 1, len(t)) if t[k].startswith("#") or t[k].strip() == "---"), len(t))
            last = max((k for k in range(h + 1, n) if t[k].strip()), default=h)
            if last == h:  # empty section: heading, blank, items, blank, next heading
                t = t[:h + 1] + [""] + block + [""] + t[n:]
                at = h + 2
            else:
                t = t[:last + 1] + block + t[last + 1:]
                at = last + 1
            # the block is the section's last item: no heading before it, only blank lines after it
            end = at + len(block)
            nxt = next((k for k in range(end, len(t)) if t[k].startswith("#") or t[k].strip() == "---"), len(t))
            if (t[at:end] != block or any(x.startswith("#") or x.strip() == "---" for x in t[h + 1:at])
                    or any(x.strip() for x in t[end:nxt])):
                raise Stop(f"{f}: {block[0]!r} did not land at the end of {heading!r}")
        elif mode == "end":
            names_ = [x for x in block if re.match(r"^-- .+ --$", x)]
            clash = [x for x in names_ if x in t]
            if clash:
                raise Stop(f"{f}: upstream already has a case named {clash}")
            tail = t[len(t) - 1:] if t and t[-1] == "" else []
            t = strip_blank(t) + [""] + block + tail
        else:
            raise Stop(f"{f}: unknown mode {mode!r}")
    # the result differs from upstream's file by exactly the re-added lines
    added = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=split(theirs), b=t, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "insert":
            raise Stop(f"{f}: re-applying changed upstream's lines near line {i1 + 1}")
        added += [x for x in t[j1:j2] if x.strip()]
    want = [x for _, b in blocks for x in b if x.strip()]
    if sorted(added) != sorted(want):
        raise Stop(f"{f}: re-applied lines do not match this PR's additions")
    return "\n".join(t)


def setup_signing(work: Path) -> None:
    """Commits are signed with the key in SIGNING_KEY_FILE (an SSH key registered for signing)."""
    key = os.environ.get("SIGNING_KEY_FILE", "")
    if not key or not Path(key).is_file():
        raise Stop("this project needs signed commits, but SIGNING_KEY_FILE is not set")
    pub = sh(["ssh-keygen", "-y", "-f", key]).stdout.strip()
    allowed = Path(key).with_name(Path(key).name + ".allowed_signers")
    allowed.write_text(f'{AUTHOR[1]} namespaces="git" {pub}\n')
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", key), ("commit.gpgsign", "true"),
                 ("gpg.ssh.allowedSignersFile", str(allowed))):
        sh(["git", "config", k, v], cwd=work)


def verify_signature(p: dict, sha: str) -> None:
    """GitHub must verify the signature against the account's keys (%G? only shows it is signed).
    Re-read a few times when the commit is not visible yet or the verification service failed."""
    for i in range(4):
        r = sh(["gh", "api", f"repos/{p['fork']}/commits/{sha}"], check=False)
        if r.returncode == 0:
            v = json.loads(r.stdout)["commit"]["verification"]
            if v.get("verified"):
                return
            reason = v.get("reason")
            if reason not in ("gpgverify_unavailable", "gpgverify_error"):
                raise Stop(f"GitHub does not verify the merge signature ({reason}); "
                           "is the signing key still registered on the account?")
        elif not re.search(r"HTTP (404|422|5\d\d)|timeout|connection", r.stderr, re.I):
            raise Stop(f"reading commit {sha[:9]} failed: {r.stderr.strip()[-300:]}")
        time.sleep(20 * (i + 1))
    raise Stop(f"GitHub could not verify the signature of {sha[:9]} after 4 reads")


def wait_run(p: dict, sha: str, run_id: int | None, deadline: float, min_attempt: int = 1) -> dict:
    """Wait for the run to complete; after a re-run, only an attempt >= min_attempt counts, so the
    previous attempt's `completed` cannot be read as the result."""
    while time.time() < deadline:
        if run_id is None:
            event = "workflow_dispatch" if p.get("dispatch") else "push"
            runs = gh_json(f"repos/{p['fork']}/actions/workflows/{p['workflow']}/runs?head_sha={sha}&event={event}")
            run = runs["workflow_runs"][0] if runs["workflow_runs"] else None
        else:
            run = gh_json(f"repos/{p['fork']}/actions/runs/{run_id}")
        if run and run["status"] == "completed" and run.get("run_attempt", 1) >= min_attempt:
            return run
        time.sleep(60)
    raise Stop(f"fork CI for {sha[:9]} (attempt {min_attempt}) did not finish in time")


def wait_ci(p: dict, sha: str, candidate: str) -> str:
    """The required jobs must succeed. Failed jobs are re-run once: on 2026-10-12 the linux job of
    an identical tree failed only because the AppImage runtime download answered 504."""
    deadline = time.time() + p["ci_timeout_min"] * 60
    if p.get("dispatch"):
        sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/workflows/{p['workflow']}/dispatches",
            "-f", f"ref={candidate}"])
    run = wait_run(p, sha, None, deadline)
    for attempt in (1, 2):
        jobs = gh_json(f"repos/{p['fork']}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        got = {j["name"]: j["conclusion"] for j in jobs}
        bad = {n: got.get(n) for n in p["required_jobs"] if got.get(n) != "success"}
        print(f"fork CI {run['html_url']} attempt {attempt}: "
              + ", ".join(f"{n}={got.get(n)}" for n in p["required_jobs"]), flush=True)
        if not bad:
            return run["html_url"]
        if attempt == 2:
            break
        sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/runs/{run['id']}/rerun-failed-jobs"])
        time.sleep(30)
        run = wait_run(p, sha, run["id"], deadline + 60 * 60, run.get("run_attempt", 1) + 1)
    raise Stop(f"fork CI {run['html_url']} did not build the merge (after one re-run): {bad}")


def report(p: dict, key: str, ok: bool, text: str) -> None:
    title = f"🔀 [自動解消] {p['upstream']}#{p['pr']} の衝突"
    found = json.loads(sh(["gh", "issue", "list", "--repo", TRACKER, "--state", "open", "--limit", "1000",
                           "--json", "number,title"]).stdout)
    issue = next((i for i in found if i["title"] == title), None)
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{TRACKER}/actions/runs/" \
              f"{os.environ.get('GITHUB_RUN_ID', '?')}"
    if ok:
        if issue:
            sh(["gh", "issue", "close", str(issue["number"]), "--repo", TRACKER,
                "--comment", f"自動で解消しました: {text}\n\n{run_url}"])
        return
    body = (f"https://github.com/{p['upstream']}/pull/{p['pr']} の衝突を自動では解消できませんでした。"
            f"PR のブランチには何も push していません。\n\n**理由**: {text}\n\n{run_url}\n\n"
            f"手順は memory の `{p['memo']}`。")
    if issue:
        sh(["gh", "issue", "edit", str(issue["number"]), "--repo", TRACKER, "--body", body])
    else:
        sh(["gh", "issue", "create", "--repo", TRACKER, "--title", title, "--body", body])


def resolve(key: str, a) -> str:
    p = PROJECTS[key]
    if not a.dry_run:
        pr = pr_state(p)
        if pr["state"] != "open":
            return f"PR is {pr['state']}; nothing to do"
        if pr["mergeable"] is not False:
            return f"PR mergeable={pr['mergeable']} ({pr['mergeable_state']}); nothing to do"
        if (pr["head"]["repo"] or {}).get("full_name") != p["fork"] or pr["head"]["ref"] != p["branch"]:
            raise Stop(f"PR head is {pr['head']['label']}, not {p['fork']}:{p['branch']}")
        start = pr["head"]["sha"]
    else:
        start = a.start_sha
    work = Path("work") / key
    sh(["git", "clone", "-q", "--filter=blob:none", f"https://github.com/{p['fork']}.git", str(work)])
    sh(["git", "remote", "add", "upstream", f"https://github.com/{p['upstream']}.git"], cwd=work)
    sh(["git", "fetch", "-q", "--filter=blob:none", "upstream", p["upstream_branch"]], cwd=work)
    upstream_sha = a.upstream_sha or sh(["git", "rev-parse", f"upstream/{p['upstream_branch']}"], cwd=work).stdout.strip()
    if a.upstream_sha:
        sh(["git", "fetch", "-q", "--filter=blob:none", "upstream", upstream_sha], cwd=work)
    start = sh(["git", "rev-parse", start], cwd=work).stdout.strip()
    sh(["git", "checkout", "-q", "-B", "auto-merge", start], cwd=work)
    for k, v in (("user.name", AUTHOR[0]), ("user.email", AUTHOR[1])):
        sh(["git", "config", k, v], cwd=work)
    if p.get("sign"):
        setup_signing(work)
    base = sh(["git", "merge-base", start, upstream_sha], cwd=work).stdout.strip()
    files_before = names(sh(["git", "diff", "-z", "--name-only", f"{upstream_sha}...{start}"], cwd=work).stdout)

    merged = sh(["git", "merge", "--no-ff", "--no-commit", upstream_sha], cwd=work, check=False)
    conflicted = names(sh(["git", "diff", "-z", "--name-only", "--diff-filter=U"], cwd=work).stdout)
    if merged.returncode != 0 and not conflicted:
        raise Stop("git merge failed without listing conflicted files")
    lockfiles, appends = p.get("lockfiles", {}), p.get("append_files", {})
    other = conflicted - set(lockfiles) - set(appends)
    if other:
        raise Stop(f"conflicts of a kind not resolved here need a person: {sorted(other)} "
                   f"(also conflicted: {sorted(conflicted - other)})")
    for f in sorted(conflicted & set(appends)):
        show = lambda rev: sh(["git", "show", f"{rev}:{f}"], cwd=work).stdout  # noqa: E731
        (work / f).write_text(reapply(f, appends[f], show(base), show(start), show(upstream_sha)))
        sh(["git", "add", "--", f], cwd=work)
    for f in sorted(conflicted & set(lockfiles)):
        sh(["git", "checkout", "--theirs", "--", f], cwd=work)
    for f, d in lockfiles.items():
        sh(["cargo", "metadata", "--format-version", "1", "-q"], cwd=work / d)
    missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        print(f"upstream versions missing before repair: {missing}", flush=True)
        repair(p, work, missing)
        missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        raise Stop(f"our lock drops upstream versions even after the known pins: {missing}")
    for f, d in lockfiles.items():
        sh(["cargo", "metadata", "--format-version", "1", "--locked", "-q"], cwd=work / d)
        sh(["git", "add", "--", f], cwd=work)
    for f in names(sh(["git", "diff", "-z", "--cached", "--name-only"], cwd=work).stdout):
        path = work / f
        if path.is_file() and re.search(r"^(<<<<<<<|>>>>>>>) ", path.read_text(errors="replace"), re.M):
            raise Stop(f"conflict marker left in {f}")
    for f, needle in p["must_contain"]:
        if needle not in (work / f).read_text():
            raise Stop(f"{f} no longer contains {needle!r}")
    if sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work).stdout.strip():
        raise Stop("unmerged paths remain")
    sh(["git", "commit", "-q", "-m",
        f"Merge remote-tracking branch 'upstream/{p['upstream_branch']}' into {p['branch']}"], cwd=work)
    head = sh(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    if p.get("sign"):
        status = sh(["git", "log", "-1", "--format=%G?", head], cwd=work).stdout.strip()
        if status != "G":  # only says the commit is signed with SIGNING_KEY_FILE; GitHub checks the account
            raise Stop(f"the merge commit is not signed (%G? = {status!r})")
    files_after = names(sh(["git", "diff", "-z", "--name-only", f"{upstream_sha}...{head}"], cwd=work).stdout)
    if files_after != files_before:
        raise Stop(f"the PR's file set changed: added {sorted(files_after - files_before)}, "
                   f"dropped {sorted(files_before - files_after)}")
    if a.expect_tree_of:
        sh(["git", "fetch", "-q", "--filter=blob:none", "origin", a.expect_tree_of], cwd=work)
        d = sh(["git", "diff", "--stat", a.expect_tree_of, head], cwd=work).stdout.strip()
        print(f"tree vs {a.expect_tree_of}: {'identical' if not d else d}", flush=True)
        if d:
            raise Stop(f"result differs from {a.expect_tree_of}")

    candidate = f"auto-merge/{p['branch'].split('/')[-1]}"
    sh(["git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{candidate}"], cwd=work)
    try:
        if p.get("sign"):
            verify_signature(p, head)
        ci = wait_ci(p, head, candidate)
    finally:
        sh(["git", "push", "-q", "origin", "--delete", candidate], cwd=work, check=False)
    if a.dry_run:
        return f"dry run: {head[:9]} built ({ci}); PR branch untouched"
    # compare-and-swap: rejected unless the branch still points at `start` (HEAD descends from it)
    sh(["git", "push", "-q", f"--force-with-lease=refs/heads/{p['branch']}:{start}", "origin",
        f"HEAD:refs/heads/{p['branch']}"], cwd=work)
    return f"{start[:9]} -> {head[:9]} (upstream {upstream_sha[:9]}), fork CI {ci}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", choices=sorted(PROJECTS))
    ap.add_argument("--dry-run", action="store_true", help="never push to the PR branch or touch the issue")
    ap.add_argument("--start-sha", help="dry run: the PR head to start from")
    ap.add_argument("--upstream-sha", help="dry run: the upstream commit to merge")
    ap.add_argument("--expect-tree-of", help="dry run: fail unless the merge result has this commit's tree")
    a = ap.parse_args()
    if a.dry_run and not a.start_sha:
        ap.error("--dry-run needs --start-sha")
    p = PROJECTS[a.project]
    try:
        msg = resolve(a.project, a)
    except Exception as e:  # noqa: BLE001  any failure: nothing reached the PR branch; say so
        msg = str(e) if isinstance(e, Stop) else f"unexpected {type(e).__name__}: {e}"
        print(f"::error::{msg}", flush=True)
        if not a.dry_run:
            report(p, a.project, False, msg)
        return 1
    print(msg, flush=True)
    if not a.dry_run and ("->" in msg or "mergeable=True" in msg):  # also closes an issue fixed by hand
        report(p, a.project, True, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
