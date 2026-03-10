#!/usr/bin/env python3
"""Sync OSS stats and PR statuses from oss-contributions README to profile README.

Runs as part of the Refresh Contributions workflow. Updates:
1. OSS_STATS markers (total PRs / merged / repo count)
2. TEAM_MIRAI_STATS markers (subtotals)
3. PR statuses in profile team-mirai table
4. New team-mirai PRs not yet in profile

Usage:
    python scripts/sync_profile_readme.py /path/to/profile/README.md
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OSS_README = ROOT / "README.md"

TEAM_MIRAI_REPOS = {
    "team-mirai-volunteer/action-board": "action-board",
    "team-mirai-volunteer/fact-checker": "fact-checker",
    "team-mirai/marumie": "marumie",
    "team-mirai-volunteer/post-checker": "post-checker",
}


def parse_summary_table(text: str) -> list[dict]:
    """Parse the summary table from oss-contributions README."""
    entries = []
    for m in re.finditer(
        r"\| \[(\w[\w-]*)\]\(https://github\.com/([^)]+)\) \|"
        r" ([^|]+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|",
        text,
    ):
        entries.append(
            {
                "short": m.group(1),
                "repo": m.group(2),
                "language": m.group(3).strip(),
                "total": int(m.group(4)),
                "merged": int(m.group(5)),
                "open": int(m.group(6)),
                "closed": int(m.group(7)),
            }
        )
    return entries


def parse_pr_statuses(text: str) -> dict[str, str]:
    """Build URL -> status mapping from all PR rows in oss-contributions README."""
    statuses = {}
    for m in re.finditer(
        r"\[#\d+\]\((https://github\.com/[^)]+/pull/\d+)\)"
        r" \| (Merged|Open|Closed|Done)",
        text,
    ):
        statuses[m.group(1)] = m.group(2)
    return statuses


def parse_team_mirai_prs(text: str) -> list[dict]:
    """Extract team-mirai PRs (not issue comments) from oss-contributions README."""
    prs = []
    current_repo = None

    for line in text.splitlines():
        hdr = re.match(r"### \[([^\]]+)\]", line)
        if hdr:
            current_repo = hdr.group(1)
            continue

        if current_repo not in TEAM_MIRAI_REPOS:
            continue

        # Match PR rows (skip issue comment rows containing /issues/)
        pr_m = re.match(
            r"\| \d+ \| \[#(\d+)\]\((https://github\.com/[^)]+/pull/\d+)\)"
            r" \| (Merged|Open|Closed) \| (.+?) \|",
            line,
        )
        if pr_m:
            prs.append(
                {
                    "pr_number": pr_m.group(1),
                    "url": pr_m.group(2),
                    "repo_short": TEAM_MIRAI_REPOS[current_repo],
                    "status": pr_m.group(3),
                    "desc": pr_m.group(4).strip(),
                }
            )
    return prs


def update_profile(profile_path: Path, oss_text: str) -> bool:
    """Update the profile README. Returns True if changed."""
    profile = profile_path.read_text(encoding="utf-8")
    original = profile

    summary = parse_summary_table(oss_text)
    if not summary:
        print(
            "ERROR: Could not parse oss-contributions summary table",
            file=sys.stderr,
        )
        return False

    # --- 1. OSS_STATS + repo count ---
    total_prs = sum(e["total"] for e in summary)
    total_merged = sum(e["merged"] for e in summary)
    repo_count = len(summary)

    profile = re.sub(
        r"<!-- OSS_STATS_START -->.*?<!-- OSS_STATS_END -->"
        r" across \d+ repositories",
        f"<!-- OSS_STATS_START -->({total_prs} PRs / {total_merged} Merged)"
        f"<!-- OSS_STATS_END --> across {repo_count} repositories",
        profile,
    )

    # --- 2. TEAM_MIRAI_STATS ---
    tm = {"total": 0, "merged": 0, "open": 0, "closed": 0}
    for e in summary:
        if e["repo"] in TEAM_MIRAI_REPOS:
            for k in tm:
                tm[k] += e[k]

    profile = re.sub(
        r"<!-- TEAM_MIRAI_STATS_START -->.*?<!-- TEAM_MIRAI_STATS_END -->",
        f'<!-- TEAM_MIRAI_STATS_START -->{tm["total"]} PRs'
        f' ({tm["merged"]} Merged / {tm["open"]} Open'
        f' / {tm["closed"]} Closed)<!-- TEAM_MIRAI_STATS_END -->',
        profile,
    )

    # --- 3. Update PR statuses in profile ---
    pr_statuses = parse_pr_statuses(oss_text)
    for url, new_status in pr_statuses.items():
        for old_status in ["Open", "Closed", "Merged", "Done"]:
            if old_status != new_status:
                profile = profile.replace(
                    f"]({url}) | {old_status} |",
                    f"]({url}) | {new_status} |",
                )

    # --- 4. Add missing team-mirai PRs ---
    oss_tm_prs = parse_team_mirai_prs(oss_text)
    existing_urls = set(
        re.findall(r"https://github\.com/team-mirai[^)]+/pull/\d+", profile)
    )
    new_prs = [p for p in oss_tm_prs if p["url"] not in existing_urls]

    if new_prs:
        lines = profile.splitlines()
        # Find team-mirai table: rows like "| N | **repo** | ..."
        insert_idx = None
        max_num = 0
        for i, line in enumerate(lines):
            rm = re.match(r"\| (\d+) \| \*\*", line)
            if rm and any(repo in line for repo in TEAM_MIRAI_REPOS.values()):
                if insert_idx is None:
                    insert_idx = i
                num = int(rm.group(1))
                if num > max_num:
                    max_num = num

        if insert_idx is not None:
            # Insert newest first (at top of table, before existing first row)
            new_prs.sort(key=lambda p: int(p["pr_number"]), reverse=True)
            for pr in new_prs:
                max_num += 1
                row = (
                    f"| {max_num} | **{pr['repo_short']}** "
                    f"| [#{pr['pr_number']}]({pr['url']}) "
                    f"| {pr['status']} | {pr['desc']} |"
                )
                lines.insert(insert_idx, row)
            profile = "\n".join(lines)

    if profile != original:
        profile_path.write_text(profile, encoding="utf-8")
        return True
    return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python sync_profile_readme.py <profile-README-path>")
        sys.exit(1)

    profile_path = Path(sys.argv[1])
    if not profile_path.exists():
        print(f"Not found: {profile_path}", file=sys.stderr)
        sys.exit(1)

    oss_text = OSS_README.read_text(encoding="utf-8")
    if update_profile(profile_path, oss_text):
        print("Profile README updated.")
    else:
        print("Profile README already up to date.")


if __name__ == "__main__":
    main()
