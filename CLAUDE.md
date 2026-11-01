# CLAUDE.md — oss-contributions

OSS への PR の記録（README）と、全リポジトリを見張る GitHub Actions を置く repo です。報告は日本語・です/ます。件数やバージョンのように変わるデータはここに書かず、README や issue を見ます。

## 自動検知 issue

`[自動検知]` で始まる issue は Actions が毎日書き換えます。本文を手で直さず、スクリプト（`scripts/`）を直します。

- `🧹 Stale claude/* branches 棚卸し`：`scripts/scan_stale_branches.py`（`scan-stale-branches.yml`）が作る。オープン PR が無く、最終更新から 60 日以上のブランチが「削除推奨」。

## claude/* ブランチを消す

- cloud の git push は自分の作業ブランチ以外に書けず、Actions の手動起動（workflow_dispatch）も 403 になる。
- 代わりに `requests/delete-branches.txt` に「repo branch」を 1 行ずつ書き（`#` 行はメモ）、PR を作って merge する。main に入ると `delete-branches.yml` が `FORK_ADMIN_PAT` で消す。`claude/*` 以外とオープン PR ありは消さない。もう無いブランチは SKIP。
- 消す前に確かめる：cloud セッションは 1 本のブランチから何度も PR を出す。merge 済みでも、最後の PR の後に新しいコミットがある・最近使われているブランチは消さない。
- PR が無いブランチ（特に fork）は、消す前に SHA を記録してユーザーに伝える。

## main への変更

main へは PR を作って merge する（直接 push しない）。
