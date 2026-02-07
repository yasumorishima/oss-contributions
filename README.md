# OSS Contributions

My open source contribution history across various projects.

## Summary

| Project | Language | PRs | Merged | Open | Closed |
|---|---|---|---|---|---|
| [action-board](https://github.com/team-mirai-volunteer/action-board) | TypeScript / Next.js | 13 | 11 | 0 | 2 |
| [post-checker](https://github.com/team-mirai-volunteer/post-checker) | TypeScript | 1 | 0 | 1 | 0 |
| [fact-checker](https://github.com/team-mirai-volunteer/fact-checker) | TypeScript | 6 | 0 | 0 | 6 |
| [pybaseball](https://github.com/jldbc/pybaseball) | Python | 7 | 0 | 7 | 0 |
| [line-bot-mcp-server](https://github.com/line/line-bot-mcp-server) | TypeScript | 1 | 0 | 1 | 0 |
| **Total** | | **28** | **11** | **9** | **8** |

## Contributions by Project

### [line/line-bot-mcp-server](https://github.com/line/line-bot-mcp-server)

MCP server for LINE Messaging API integration with AI agents. Built with TypeScript by LY Corporation (LINE). 500+ stars.

| # | PR | Status | Description |
|---|---|---|---|
| 1 | [#369](https://github.com/line/line-bot-mcp-server/pull/369) | Open | Add get_follower_ids tool to retrieve follower user IDs |

### [team-mirai-volunteer/action-board](https://github.com/team-mirai-volunteer/action-board)

Civic tech platform for citizen participation in Japan. Built with Next.js, TypeScript, Supabase.

| # | PR | Status | Description |
|---|---|---|---|
| 13 | [#1969](https://github.com/team-mirai-volunteer/action-board/pull/1969) | Merged | Add pure function unit tests (48 tests across 4 files) |
| 12 | [#1918](https://github.com/team-mirai-volunteer/action-board/pull/1918) | Merged | Disable Supabase Image Transformation to fix broken images |
| 11 | [#1914](https://github.com/team-mirai-volunteer/action-board/pull/1914) | Merged | Block deletion of shapes with XP to prevent infinite XP exploit |
| 10 | [#1906](https://github.com/team-mirai-volunteer/action-board/pull/1906) | Merged | Refactor achieveMissionAction: extract type-specific logic into helpers |
| 9 | [#1869](https://github.com/team-mirai-volunteer/action-board/pull/1869) | Merged | Supabase RPC function tests for develop branch |
| 8 | [#1868](https://github.com/team-mirai-volunteer/action-board/pull/1868) | Merged | Fix posting count display: show sheets instead of times |
| 7 | [#1867](https://github.com/team-mirai-volunteer/action-board/pull/1867) | Merged | Show error toast on poster mission failure |
| 6 | [#1859](https://github.com/team-mirai-volunteer/action-board/pull/1859) | Merged | Add Supabase RPC function tests |
| 5 | [#1856](https://github.com/team-mirai-volunteer/action-board/pull/1856) | Merged | Update video mission description text |
| 4 | [#1855](https://github.com/team-mirai-volunteer/action-board/pull/1855) | Closed | Street speech map link (closed as duplicate) |
| 3 | [#1849](https://github.com/team-mirai-volunteer/action-board/pull/1849) | Merged | Add breadcrumb navigation to 8 pages |
| 2 | [#1845](https://github.com/team-mirai-volunteer/action-board/pull/1845) | Merged | Fix prefecture cache invalidation |
| 1 | [#69 comment](https://github.com/team-mirai-volunteer/fact-checker/issues/69#issuecomment-3811711591) | Done | X API v2 engagement filtering investigation report |

### [team-mirai-volunteer/post-checker](https://github.com/team-mirai-volunteer/post-checker)

Social media post verification tool. Built with TypeScript, Vitest.

| # | PR | Status | Description |
|---|---|---|---|
| 1 | [#34](https://github.com/team-mirai-volunteer/post-checker/pull/34) | Open | Fix timezone-dependent date parsing in filename generation |

### [team-mirai-volunteer/fact-checker](https://github.com/team-mirai-volunteer/fact-checker)

Fact-checking automation tool. **Project is now inactive** (succeeded by post-checker).

| # | PR | Status | Description |
|---|---|---|---|
| 6 | [#88](https://github.com/team-mirai-volunteer/fact-checker/pull/88) | Closed | Slack same-thread reply for completion reports |
| 5 | [#87](https://github.com/team-mirai-volunteer/fact-checker/pull/87) | Closed | Deduplicate tweets using start_time filter |
| 4 | [#86](https://github.com/team-mirai-volunteer/fact-checker/pull/86) | Closed | Unit tests for Note markdown utilities (33 tests) |
| 3 | [#85](https://github.com/team-mirai-volunteer/fact-checker/pull/85) | Closed | Slack button env-based branching for dev preview |
| 2 | [#84](https://github.com/team-mirai-volunteer/fact-checker/pull/84) | Closed | Disable Twitter posting in non-prod environments |
| 1 | [#83](https://github.com/team-mirai-volunteer/fact-checker/pull/83) | Closed | Client-side engagement filtering for tweets |

### [jldbc/pybaseball](https://github.com/jldbc/pybaseball)

Python library for pulling baseball statistics (Statcast, Baseball Reference, FanGraphs). 1.5k+ stars.

| # | PR | Status | Description |
|---|---|---|---|
| 7 | [#504](https://github.com/jldbc/pybaseball/pull/504) | Open | Fix team_ids returning empty data for seasons after 2021 |
| 6 | [#503](https://github.com/jldbc/pybaseball/pull/503) | Open | Fix team_batting_bref/team_pitching_bref for updated Baseball Reference HTML |
| 5 | [#502](https://github.com/jldbc/pybaseball/pull/502) | Open | Add input validation to team_fielding_bref |
| 4 | [#501](https://github.com/jldbc/pybaseball/pull/501) | Open | Fix deprecated GitHub authentication in retrosheet.py |
| 3 | [#500](https://github.com/jldbc/pybaseball/pull/500) | Open | Fix FutureWarning in team_results.py |
| 2 | [#499](https://github.com/jldbc/pybaseball/pull/499) | Open | Replace deprecated `errors='ignore'` with explicit try/except |
| 1 | [#498](https://github.com/jldbc/pybaseball/pull/498) | Open | Fix function name typo in statcast_pitcher_spin.md |

#### Issue Comments

| Issue | Comment | Description |
|---|---|---|
| [#470](https://github.com/jldbc/pybaseball/issues/470#issuecomment-3846980849) | Verified | schedule_and_record works on current master (curl_cffi migration) |
| [#477](https://github.com/jldbc/pybaseball/issues/477#issuecomment-3847380960) | Verified | pitching/batting_stats_range works on current master |
| [#481](https://github.com/jldbc/pybaseball/issues/481#issuecomment-3846988830) | Verified | Empty GH_TOKEN issue fixed by PR #501 |
