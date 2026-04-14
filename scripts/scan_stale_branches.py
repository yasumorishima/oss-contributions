"""Scan all yasumorishima repos for stale `claude/*` branches and update
a single tracker Issue with the candidate list.

Usage:
    python scripts/scan_stale_branches.py            # update tracker issue
    python scripts/scan_stale_branches.py --print    # print only, no issue update
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
ISSUE_LABEL = "auto-detect:stale-branches"
ISSUE_TITLE = "🧹 [自動検知] Stale claude/* branches 棚卸し"
TRACKER_REPO = "yasumorishima/oss-contributions"
STALE_DAYS_THRESHOLD = 60  # PR未作成かつN日以上経過なら削除推奨


def run(cmd: list[str]) -> str | None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
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
        "--json", "url,state,headRefName,number",
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


def get_upstream_full(author: str, repo: str) -> str | None:
    """For a fork, return upstream `owner/name`. None if not a fork or no parent."""
    raw = run([
        "gh", "api", f"repos/{author}/{repo}",
        "--jq", ".parent.full_name // empty",
    ])
    return raw if raw else None


def classify(branch_date: str, pr: dict | None) -> tuple[str, str]:
    """Return (action, reason)."""
    if pr:
        state = pr.get("state", "").upper()
        url = pr.get("url", "")
        pr_num = url.rsplit("/", 1)[-1] if url else "?"
        if state == "OPEN":
            return ("残置", f"オープンPR #{pr_num}")
        if state == "MERGED":
            return ("削除推奨", f"マージ済PR #{pr_num}")
        if state == "CLOSED":
            return ("削除推奨", f"クローズPR #{pr_num}")
    # No PR
    if not branch_date:
        return ("要確認", "PR未作成・日付不明")
    try:
        bdate = datetime.strptime(branch_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - bdate).days
    except ValueError:
        return ("要確認", "PR未作成・日付パース失敗")
    if age_days >= STALE_DAYS_THRESHOLD:
        return ("削除推奨", f"PR未作成・{age_days}日経過")
    return ("残置", f"PR未作成・{age_days}日経過")


def build_report(rows: list[dict]) -> str:
    """Build markdown report grouped by fork/own."""
    fork_rows = [r for r in rows if r["fork"]]
    own_rows = [r for r in rows if not r["fork"]]

    def render_table(group_rows: list[dict]) -> str:
        if not group_rows:
            return "_(該当なし)_\n"
        lines = ["| リポ | branch | 最終更新 | PR | 推奨 | 削除コマンド |",
                 "|---|---|---|---|---|---|"]
        # Sort: 削除推奨 first, then by repo, then by date
        priority = {"削除推奨": 0, "要確認": 1, "残置": 2}
        for r in sorted(group_rows, key=lambda x: (priority.get(x["action"], 9), x["repo"], x["date"])):
            del_cmd = f"`gh api -X DELETE repos/{r['author']}/{r['repo']}/git/refs/heads/{r['branch']}`"
            lines.append(
                f"| `{r['repo']}` | `{r['branch']}` | {r['date']} | {r['pr_label']} | **{r['action']}** | {del_cmd} |"
            )
        return "\n".join(lines) + "\n"

    now_jst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M JST")
    summary_counts = {}
    for r in rows:
        summary_counts[r["action"]] = summary_counts.get(r["action"], 0) + 1
    summary = " / ".join(f"{k}: {v}" for k, v in summary_counts.items()) or "なし"

    return (
        f"# Stale claude/* Branches Tracker\n\n"
        f"最終スキャン: {now_jst}\n"
        f"閾値: PR未作成かつ {STALE_DAYS_THRESHOLD} 日以上経過 → 削除推奨\n\n"
        f"**集計**: {summary}（合計 {len(rows)} ブランチ）\n\n"
        f"## ⚠️ フォーク内 (上流誤PRリスクあり、優先削除候補)\n\n"
        f"{render_table(fork_rows)}\n"
        f"## 自リポ内\n\n"
        f"{render_table(own_rows)}\n"
        f"---\n"
        f"_自動更新: `scan_stale_branches.py` by GitHub Actions_\n"
        f"_候補に同意したら該当行の「削除コマンド」をコピペ実行、または一括削除を依頼してください_\n"
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
             "--color", "EDEDED", "--description", "Auto-detect: stale claude/* branches",
             "--force"])
        result = run([
            "gh", "issue", "create", "--repo", TRACKER_REPO,
            "--title", ISSUE_TITLE, "--label", ISSUE_LABEL,
            "--body-file", str(body_path),
        ])
        print(f"Created tracker issue: {result}")
    else:
        run([
            "gh", "issue", "edit", str(issue_num), "--repo", TRACKER_REPO,
            "--body-file", str(body_path),
        ])
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
        branches = list_claude_branches(author, repo["name"])
        if not branches:
            continue
        print(f"  {repo['name']}: {len(branches)} claude/* branches")
        # Query PRs in own repo
        own_full = f"{author}/{repo['name']}"
        prs_own = list_prs_for_repo(own_full, author)
        # For forks, also query upstream (PRs from fork branches usually go to upstream)
        prs_upstream: list[dict] = []
        if repo["fork"]:
            upstream_full = get_upstream_full(author, repo["name"])
            if upstream_full:
                prs_upstream = list_prs_for_repo(upstream_full, author)

        for b in branches:
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
