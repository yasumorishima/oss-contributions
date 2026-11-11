"""Scan all yasumorishima repos for stale `claude/*` branches, and forks for
leftover work branches of any name, and update a single tracker Issue with
the candidate list.

Usage:
    python scripts/scan_stale_branches.py            # update tracker issue
    python scripts/scan_stale_branches.py --print    # print only, no issue update
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
ISSUE_LABEL = "auto-detect:stale-branches"
ISSUE_TITLE = "🧹 [自動検知] 消し忘れブランチ棚卸し（claude/* と fork の作業ブランチ）"
TRACKER_REPO = "yasumorishima/oss-contributions"
STALE_DAYS_THRESHOLD = 60  # 最終更新からN日以上経過（かつオープンPRなし）なら削除推奨
# A fork work branch whose last commit is the head of a merged upstream PR is
# safe to delete: the content lives on upstream. Wait a few days in case the
# branch is about to be reused.
FORK_MERGED_GRACE_DAYS = 7


def run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: {' '.join(cmd[:5])}...", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"FAIL: {' '.join(cmd[:5])}...", file=sys.stderr)
        if r.stderr:
            print(r.stderr[:500], file=sys.stderr)
        return None
    return r.stdout.strip()


def list_repos(author: str) -> list[dict]:
    """Return [{name, fork}] for non-archived repos."""
    raw = run([
        "gh", "repo", "list", author, "--no-archived", "--limit", "200",
        "--json", "name,isFork",
    ])
    if not raw:
        return []
    return [{"name": r["name"], "fork": r["isFork"]} for r in json.loads(raw)]


def list_claude_branches(author: str, repo: str) -> list[dict]:
    """Return [{name, sha, date}] for claude/* branches."""
    raw = run([
        "gh", "api", "--paginate", f"repos/{author}/{repo}/branches",
        "--jq", '.[] | select(.name | startswith("claude/")) | {name: .name, sha: .commit.sha}',
    ])
    if not raw:
        return []
    branches = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        # Get commit date
        date_raw = run([
            "gh", "api", f"repos/{author}/{repo}/commits/{b['sha']}",
            "--jq", ".commit.committer.date",
        ])
        b["date"] = (date_raw or "")[:10]
        branches.append(b)
    return branches


def list_prs_for_repo(repo_full: str, author: str) -> list[dict]:
    """List all PRs (any state) authored by `author` in `repo_full` (owner/name).
    Returns [{url, state, headRefName, number}].
    """
    raw = run([
        "gh", "pr", "list", "--repo", repo_full, "--author", author,
        "--state", "all", "--limit", "200",
        "--json", "url,state,headRefName,number,headRefOid,mergedAt",
    ])
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def find_pr_for_branch(prs: list[dict], branch: str) -> dict | None:
    """Find PR(s) matching a branch. Prefer most actionable one (open > merged > closed)."""
    matches = [p for p in prs if p.get("headRefName") == branch]
    if not matches:
        return None
    state_priority = {"OPEN": 0, "MERGED": 1, "CLOSED": 2}
    matches.sort(key=lambda p: state_priority.get(p.get("state", "").upper(), 3))
    return matches[0]


def list_branch_names(repo_full: str) -> set[str] | None:
    """All branch names of `repo_full`, or None when they cannot be read."""
    raw = run(["gh", "api", "--paginate", f"repos/{repo_full}/branches?per_page=100", "--jq", ".[].name"])
    if raw is None:
        return None
    return {line for line in raw.splitlines() if line.strip()}


def list_fork_work_branches(author: str, repo: str, upstream_names: set[str],
                            default_branch: str) -> list[dict]:
    """Return [{name, sha, date}] for branches the owner made in a fork.

    Skipped: claude/* (scanned by list_claude_branches), the default branch,
    names that also exist upstream, and branches whose last commit was not
    written by the owner (copied from upstream when the fork was made).
    """
    raw = run([
        "gh", "api", "--paginate", f"repos/{author}/{repo}/branches?per_page=100",
        "--jq", '.[] | {name: .name, sha: .commit.sha}',
    ])
    if not raw:
        return []
    branches = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        name = b["name"]
        if name.startswith("claude/") or name == default_branch or name in upstream_names:
            continue
        info = run([
            "gh", "api", f"repos/{author}/{repo}/commits/{b['sha']}",
            "--jq", '[.commit.committer.date, (.author.login // ""), (.committer.login // "")] | @tsv',
        ])
        if info is None:
            continue
        date_s, author_login, committer_login = (info.split("\t") + ["", "", ""])[:3]
        if author not in (author_login, committer_login):
            continue
        b["date"] = date_s[:10]
        branches.append(b)
    return branches


def get_fork_info(author: str, repo: str) -> tuple[str | None, str]:
    """For a fork, return (upstream `owner/name` or None, default branch)."""
    raw = run([
        "gh", "api", f"repos/{author}/{repo}",
        "--jq", '{parent: (.parent.full_name // ""), default: (.default_branch // "")}',
    ])
    if not raw:
        return None, ""
    info = json.loads(raw)
    return (info["parent"] or None), info["default"]


def get_upstream_full(author: str, repo: str) -> str | None:
    """For a fork, return upstream `owner/name`. None if not a fork or no parent."""
    raw = run([
        "gh", "api", f"repos/{author}/{repo}",
        "--jq", ".parent.full_name // empty",
    ])
    return raw if raw else None


def age_in_days(branch_date: str) -> int | None:
    """Days since the branch's last commit, or None if the date is unknown."""
    if not branch_date:
        return None
    try:
        bdate = datetime.strptime(branch_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - bdate).days


def classify(branch_date: str, pr: dict | None) -> tuple[str, str]:
    """Return (action, reason).

    A merged or closed PR alone is not enough: cloud sessions reuse one branch
    for many PRs, so a branch whose last PR merged today may already carry the
    next round of work. Every branch must also be idle for STALE_DAYS_THRESHOLD.
    """
    age = age_in_days(branch_date)
    if pr:
        state = pr.get("state", "").upper()
        url = pr.get("url", "")
        pr_num = url.rsplit("/", 1)[-1] if url else "?"
        if state == "OPEN":
            return ("残置", f"オープンPR #{pr_num}")
        label = {"MERGED": "マージ済PR", "CLOSED": "クローズPR"}.get(state, "PR")
        if age is None:
            return ("要確認", f"{label} #{pr_num}・日付不明")
        if age >= STALE_DAYS_THRESHOLD:
            return ("削除推奨", f"{label} #{pr_num}・{age}日経過")
        return ("残置", f"{label} #{pr_num}・{age}日経過（使用中の可能性）")
    # No PR
    if age is None:
        return ("要確認", "PR未作成・日付不明")
    if age >= STALE_DAYS_THRESHOLD:
        return ("削除推奨", f"PR未作成・{age}日経過")
    return ("残置", f"PR未作成・{age}日経過")


def pr_ref(pr: dict) -> str:
    """`owner/repo#N` from a PR url."""
    url = pr.get("url", "")
    parts = url.split("/")
    return f"{parts[3]}/{parts[4]}#{parts[6]}" if len(parts) >= 7 else "?"


def classify_fork_work(branch_sha: str, branch_date: str, prs: list[dict],
                       name: str = "") -> tuple[str, str, dict | None]:
    """Return (action, reason, chosen PR) for a fork work branch (not claude/*).

    `prs` are all PRs (upstream and fork) whose head is this branch.
    """
    if "screenshot" in name.lower():
        # PR descriptions load their images from these branches; deleting one breaks them
        return ("残置", "画像の置き場（PR の説明が参照）", None)
    age = age_in_days(branch_date)
    age_s = f"{age}日経過" if age is not None else "日付不明"
    open_prs = [x for x in prs if x.get("state", "").upper() == "OPEN"]
    if open_prs:
        # delete-branches.yml refuses a branch with any open PR, so never suggest it
        return ("残置", f"オープンPR {pr_ref(open_prs[0])}", open_prs[0])
    merged_here = [x for x in prs if x.get("state", "").upper() == "MERGED"
                   and x.get("headRefOid") == branch_sha]
    if merged_here:
        pr = merged_here[0]
        merged_age = age_in_days((pr.get("mergedAt") or "")[:10])
        if merged_age is not None and merged_age >= FORK_MERGED_GRACE_DAYS:
            return ("削除推奨", f"マージ済PR {pr_ref(pr)}・PR の後のコミットなし・merge から{merged_age}日", pr)
        return ("残置", f"マージ済PR {pr_ref(pr)}・merge から{merged_age}日（{FORK_MERGED_GRACE_DAYS}日待ち）", pr)
    pr = None
    if prs:
        order = {"MERGED": 0, "CLOSED": 1}
        pr = sorted(prs, key=lambda x: order.get(x.get("state", "").upper(), 2))[0]
        state = pr.get("state", "").upper()
        label = ("マージ済PR" if state == "MERGED" else "クローズPR") + f" {pr_ref(pr)}"
        if state == "MERGED":
            label += "・PR の後に新しいコミット"
    else:
        label = "PR未作成"
    if age is not None and age >= STALE_DAYS_THRESHOLD:
        return ("要確認", f"{label}・{age_s}（消す前に SHA を記録）", pr)
    return ("残置", f"{label}・{age_s}", pr)


def build_report(rows: list[dict]) -> str:
    """Build markdown report grouped by fork/own."""
    fork_rows = [r for r in rows if r["fork"]]
    own_rows = [r for r in rows if not r["fork"]]

    def render_table(group_rows: list[dict]) -> str:
        if not group_rows:
            return "_(該当なし)_\n"
        lines = ["| リポ | branch | SHA | 最終更新 | PR | 推奨 | 理由 | 削除コマンド |",
                 "|---|---|---|---|---|---|---|---|"]
        # Sort: 削除推奨 first, then by repo, then by date
        priority = {"削除推奨": 0, "要確認": 1, "残置": 2}
        for r in sorted(group_rows, key=lambda x: (priority.get(x["action"], 9), x["repo"], x["date"])):
            del_cmd = f"`gh api -X DELETE repos/{r['author']}/{r['repo']}/git/refs/heads/{r['branch']}`"
            lines.append(
                f"| `{r['repo']}` | `{r['branch']}` | `{r.get('sha', '')[:8]}` | {r['date']} | {r['pr_label']} | **{r['action']}** | {r['reason']} | {del_cmd} |"
            )
        return "\n".join(lines) + "\n"

    # the runner clock is UTC, so convert explicitly instead of astimezone()
    now_jst = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M JST")
    summary_counts = {}
    for r in rows:
        summary_counts[r["action"]] = summary_counts.get(r["action"], 0) + 1
    summary = " / ".join(f"{k}: {v}" for k, v in summary_counts.items()) or "なし"

    return (
        f"# 消し忘れブランチ Tracker（claude/* と fork の作業ブランチ）\n\n"
        f"最終スキャン: {now_jst}\n"
        f"閾値（claude/*）: オープンPRなしかつ最終更新から {STALE_DAYS_THRESHOLD} 日以上経過 → 削除推奨（マージ済PRがあっても同じ）\n"
        f"閾値（fork の作業ブランチ＝upstream に同名が無く、最後のコミットが自分のもの）: "
        f"マージ済PRの head がブランチの先と同じで merge から {FORK_MERGED_GRACE_DAYS} 日以上・開いた PR なし → 削除推奨／"
        f"PR なし・クローズ・PR の後に新しいコミットで {STALE_DAYS_THRESHOLD} 日以上 → 要確認\n\n"
        f"**集計**: {summary}（合計 {len(rows)} ブランチ）\n\n"
        f"## ⚠️ フォーク内 (上流誤PRリスクあり、優先削除候補)\n\n"
        f"{render_table(fork_rows)}\n"
        f"## 自リポ内\n\n"
        f"{render_table(own_rows)}\n"
        f"---\n"
        f"_自動更新: `scan_stale_branches.py` by GitHub Actions_\n"
        f"_候補に同意したら該当行の「削除コマンド」をコピペ実行、または一括削除を依頼してください_\n\n"
        f"_cloud から消すとき: git push も Actions の手動起動も 403 になるので、"
        f"`requests/delete-branches.txt` に「repo branch」を 1 行ずつ書いて PR を作り merge する。"
        f"main に入ると `delete-branches.yml` が消す（default ブランチと、オープン PR（fork なら fork 元の PR も）があるブランチは消さない）。"
        f"消す前に、merge 済みでもブランチに新しいコミットが無いか確かめる_\n"
    )


def find_tracker_issue() -> int | None:
    raw = run([
        "gh", "issue", "list", "--repo", TRACKER_REPO,
        "--label", ISSUE_LABEL, "--state", "open",
        "--json", "number", "--limit", "5",
    ])
    if not raw:
        return None
    issues = json.loads(raw)
    return issues[0]["number"] if issues else None


def upsert_issue(body: str) -> None:
    issue_num = find_tracker_issue()
    body_path = Path("/tmp/stale_branches_body.md") if Path("/tmp").exists() else ROOT / "_stale_body.tmp.md"
    body_path.write_text(body, encoding="utf-8")
    if issue_num is None:
        # Ensure label exists
        run(["gh", "label", "create", ISSUE_LABEL, "--repo", TRACKER_REPO,
             "--color", "EDEDED", "--description", "Auto-detect: stale claude/* and fork work branches",
             "--force"])
        result = run([
            "gh", "issue", "create", "--repo", TRACKER_REPO,
            "--title", ISSUE_TITLE, "--label", ISSUE_LABEL,
            "--body-file", str(body_path),
        ])
        if result is None:
            sys.exit("Failed to create the tracker issue")
        print(f"Created tracker issue: {result}")
    else:
        result = run([
            "gh", "issue", "edit", str(issue_num), "--repo", TRACKER_REPO,
            "--title", ISSUE_TITLE, "--body-file", str(body_path),
        ])
        if result is None:
            sys.exit(f"Failed to update tracker issue #{issue_num}")
        print(f"Updated tracker issue #{issue_num}")
    if body_path.exists() and body_path.name == "_stale_body.tmp.md":
        body_path.unlink()


def main() -> None:
    print_only = "--print" in sys.argv
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    author = config.get("author", "yasumorishima")

    print(f"Listing repos for {author}...")
    repos = list_repos(author)
    print(f"  {len(repos)} repos")

    rows: list[dict] = []
    for repo in repos:
        branches = [dict(b, kind="claude") for b in list_claude_branches(author, repo["name"])]
        upstream_full = None
        if repo["fork"]:
            upstream_full, default_branch = get_fork_info(author, repo["name"])
            upstream_names = list_branch_names(upstream_full) if upstream_full else None
            if upstream_names is None:
                # Without the upstream list every inherited branch would look like ours.
                print(f"  {repo['name']}: upstream branches unreadable, fork work branches skipped",
                      file=sys.stderr)
            else:
                branches += [dict(b, kind="fork-work") for b in
                             list_fork_work_branches(author, repo["name"], upstream_names, default_branch)]
        if not branches:
            continue
        print(f"  {repo['name']}: {len(branches)} branches to check")
        # Query PRs in own repo
        own_full = f"{author}/{repo['name']}"
        prs_own = list_prs_for_repo(own_full, author)
        # For forks, also query upstream (PRs from fork branches usually go to upstream)
        prs_upstream: list[dict] = []
        if upstream_full:
            prs_upstream = list_prs_for_repo(upstream_full, author)

        for b in branches:
            if b["kind"] == "fork-work":
                matches = [x for x in prs_upstream + prs_own if x.get("headRefName") == b["name"]]
                action, reason, pr = classify_fork_work(b["sha"], b["date"], matches, b["name"])
            else:
                pr = find_pr_for_branch(prs_own, b["name"]) or find_pr_for_branch(prs_upstream, b["name"])
                action, reason = classify(b["date"], pr)
            pr_label = "なし"
            if pr:
                num = pr.get("url", "").rsplit("/", 1)[-1]
                pr_label = f"{pr.get('state','')} #{num}"
            rows.append({
                "author": author,
                "repo": repo["name"],
                "fork": repo["fork"],
                "branch": b["name"],
                "sha": b.get("sha", ""),
                "date": b["date"],
                "pr_label": pr_label,
                "action": action,
                "reason": reason,
            })

    body = build_report(rows)
    if print_only:
        sys.stdout.reconfigure(encoding="utf-8")  # avoid Windows cp932 errors
        print(body)
        return
    upsert_issue(body)


if __name__ == "__main__":
    main()
