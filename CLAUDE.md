# CLAUDE.md — oss-contributions

OSS への PR の記録（README）と、全リポジトリを見張る GitHub Actions を置く repo です。報告は日本語・です/ます。件数やバージョンのように変わるデータはここに書かず、README や issue を見ます。

## 自動検知 issue

`[自動検知]` で始まる issue は Actions が毎日書き換えます。本文を手で直さず、スクリプト（`scripts/`）を直します。

- `🧹 Stale claude/* branches 棚卸し`：`scripts/scan_stale_branches.py`（`scan-stale-branches.yml`）が作る。オープン PR が無く、最終更新から 60 日以上のブランチが「削除推奨」。

## ブランチを消す

- cloud の git push は自分の作業ブランチ以外に書けず、Actions の手動起動（workflow_dispatch）も 403 になる。
- 代わりに `requests/delete-branches.txt` に「repo branch」を 1 行ずつ書き（`#` 行はメモ）、PR を作って merge する。main に入ると `delete-branches.yml` が `FORK_ADMIN_PAT` で消す。default ブランチと、オープン PR（fork なら fork 元の PR も）があるブランチは消さない。もう無いブランチは SKIP。
- 消す前に確かめる：cloud セッションは 1 本のブランチから何度も PR を出す。merge 済みでも、最後の PR の後に新しいコミットがある・最近使われているブランチは消さない。
- PR が無いブランチ（特に fork）は、消す前に SHA を記録してユーザーに伝える。

## 手元だけのコミットを報告する前に

「push されていない」「コンテナが消えたらなくなる」とユーザーに伝える前に、そのコードがリモートのどこかのブランチ（作業ブランチや fork の他のブランチ）に残っていないか、ツリーの差分で確かめる。残っていれば報告しない。

## main への変更

cloud からは main に直接 push できないので、PR を作って merge する。

## バウンティ

- Algora の受け取り設定（Stripe・本人確認）はユーザーが済ませている。ユーザーに確認を求めない。
- Algora は merge だけでは支払われず、出資者が Algora で支払いを押すまで届かない。
