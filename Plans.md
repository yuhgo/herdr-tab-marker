# herdr-tab-marker Plans.md

作成日: 2026-08-13

正本: 設計・調査結果は [HANDOFF.md](HANDOFF.md)（product contract 相当）。
この Plans.md は task ledger であり、HANDOFF.md の決定事項を上書きしない。

---

## 実装前に再確認した前提（stage 1: 検証・調査）

HANDOFF.md §3-1 / §5-6 が「実装前に再確認せよ」と指定した項目を実機確認済み。

| 確認項目 | 結果 | 根拠 |
|---------|------|------|
| herdr の色 API の有無 | **無し**（RPC 72 メソッド / `tab.*` は 7 つ / color・style・icon・theme 系 0 件） | `herdr.sock` に不正メソッドを投げてメソッド一覧を取得（HANDOFF.md §3-1 の手法） |
| herdr バージョン | 0.7.3（HANDOFF.md 調査時と同一） | `herdr --version` |
| `claude -p` のフラグ有効性 | `--safe-mode` / `--tools` / `--max-budget-usd` / `--no-session-persistence` / `--system-prompt` すべて有効 | `claude --help`（CLI 2.1.231） |
| dotfiles の差し替え箇所 | 7 箇所すべて実在（下表） | `grep -rn "herdr-auto-title\|HERDR_AUTO_TITLE" claude/ .gitignore` |
| `ruff` | **未インストール** → Task 1.1 で baseline を用意する | `which ruff` |
| `shellcheck` | 利用可能 | `which shellcheck` |

→ **絵文字方式（HANDOFF.md §2 の決定）は有効。方針変更なし。**

### 差し替え対象の実測行番号（2026-08-13 再確認済み）

| # | ファイル | 実測行 | 現在の内容 |
|---|---------|-------|-----------|
| 1 | `claude/settings.mac.json` | 13 | `"HERDR_AUTO_TITLE_LANG": "ja",` |
| 2 | `claude/settings.mac.json` | 178 | `python3 '.../hooks/herdr-auto-title.py'` |
| 3 | `claude/settings.wsl.json` | 6 | `"HERDR_AUTO_TITLE_LANG": "ja",` |
| 4 | `claude/settings.wsl.json` | 184 | `python3 '.../hooks/herdr-auto-title.py'` |
| 5 | `.gitignore` | 145-146 | `claude/herdr-auto-title/` |
| 6 | `claude/AGENTS.md` | 171 | 設定ファイル表の該当行 |
| 7 | `claude/AGENTS.md` | 177-191 | 「herdr 連携」節 |

### unknown_data

| 項目 | 状態 | 理由 |
|------|------|------|
| Codex CLI での実機動作 | `unknown` | `codex` CLI が未インストール（dotfiles AGENTS.md に既述）。コードパスは実装するが実機検証できない |
| WSL 環境での実機動作 | `unknown` | 本セッションは macOS。WSL 側の settings は編集するが、起動確認は WSL 側セッションで行う必要がある |
| 古い herdr での `HERDR_TAB_ID` 欠落 | `unknown` | 0.7.3 では存在する。欠落ケースは `pane.get` フォールバックで対応するが再現環境なし |

---

## Phase 1: 足場（lint baseline）

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | `[lane:fast]` `[tdd:skip:tooling-setup]` `ruff.toml` を追加し lint baseline を敷く。上流の設定（`target-version = "py39"` / `line-length = 100` / `select = ["E","W","F","I","UP","B","C4","SIM","RET"]`）を踏襲。`ruff` 未インストールのため `uvx ruff` か `python3 -m ruff` で実行できる手順を README に書く | `uvx ruff check .` が exit 0（対象ファイルが無い時点でも成功すること）／`ruff.toml` が commit されている | - | cc:完了 |

---

## Phase 2: 本体実装（TDD）

HANDOFF.md §5-1 のファイル構成に従い、`herdr_tab_marker.py` 単一ファイル・Python 3 標準ライブラリのみ。
上流から踏襲する箇所（§4-3）は MIT なので流用可。**LICENSE に上流の帰属表記を入れる**。

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `[lane:gate]` `[tdd:required]` 純粋関数層を実装。`display_width` / `truncate` / `sanitize_title` / `state_path`（§4-3 踏襲）+ 新規 `detect_repo`（`git rev-parse --git-common-dir` を使う。`--show-toplevel` は使わない） | `tests/` の単体テストが全通過。`detect_repo` が worktree でもリポジトリ本体名を返すテストを含む | 1.1 | cc:完了 |
| 2.2 | `[lane:gate]` `[tdd:required]` マーカー決定ロジック。`marker_for`（1. `HERDR_TAB_MARKER_MAP` の明示指定 → 2. 自前畳み込みハッシュ `digest = (digest*131 + ord(ch)) & 0xFFFFFFFF` で PALETTE 選択）。**Python 組み込み `hash()` は使わない**（seed 変動で絵文字が毎回変わるため） | 同一リポジトリ名で常に同一絵文字を返すテストが通過。別プロセス起動でも一致することを確認するテストを含む | 2.1 | cc:完了 |
| 2.3 | `[lane:gate]` `[tdd:required]` `strip_marker` — 既存マーカーを剥がす。剥がす対象は**自分が付けうるもの（PALETTE + MAP の値）に限定**する（「絵文字一般」を正規表現で書こうとしない） | `🟦 ssh` → `ssh`、`🟦🟦 x` → `x`、絵文字なし `ssh` → `ssh` が通過。PALETTE 外の絵文字は剥がさないテストを含む | 2.2 | cc:完了 |
| 2.4 | `[lane:gate]` `[tdd:required]` `may_overwrite_title` — **`strip_marker` 後の文字列**に対して判定する（HANDOFF.md §5-2）。上書き可は「空」「herdr 既定ラベル `^\d+$`」「前回自分が付けたタイトルと一致」の 3 つ | `🟦 ssh` の `ssh` 部分を手動ラベルと判定するテストが通過。剥がす前に判定して誤認しないことを回帰テストで固定 | 2.3 | cc:完了 |
| 2.5 | `[lane:gate]` `[tdd:skip:socket-io-boundary]` herdr RPC 層。`herdr_call`（id は**文字列必須**）/ `resolve_tab_id`（`HERDR_TAB_ID` → 無ければ `pane.get` フォールバック）/ `get_tab_label` / `rename_tab`（§4-3 踏襲） | `HERDR_SOCKET_PATH` 未設定時に `None` を返して例外を出さない。実機 `tab.get` が成功することを手動確認 | 2.1 | cc:完了 |
| 2.6 | `[lane:gate]` `[tdd:skip:subprocess-boundary]` タイトル生成。`claude -p --safe-mode --model haiku --tools "" --no-session-persistence --max-budget-usd 0.05 --system-prompt ...`（§4-4）。`child_env()` で `HERDR_ENV` 等を除去し**自己再入を防ぐ**。`detect_agent`（`turn_id` の有無で Claude/Codex 判別） | `claude` が PATH に無い場合に空文字を返して落ちない。`child_env()` が `HERDR_ENV` / `HERDR_PANE_ID` / `HERDR_TAB_ID` / `HERDR_WORKSPACE_ID` / `CLAUDECODE` を除去するテストが通過 | 2.1 | cc:完了 |
| 2.7 | `[lane:gate]` `[tdd:skip:process-orchestration]` 統合フロー。`HERDR_ENV != 1` で即 `exit 0` / `fcntl.flock` で多重起動防止 / `daemonize()` で hook をブロックしない / 幅配分（`MAX_WIDTH` - マーカー3カラム = タイトル予算）/ `HERDR_TAB_MARKER_FOREGROUND=1` で前景実行（§5-7 のデバッグ用） | HANDOFF.md §5-7 のワンライナー（`echo '{"session_id":"test",...}' \| python3 herdr_tab_marker.py`）が前景で完走し debug.log にフローが出る | 2.4, 2.5, 2.6 | cc:完了 |
| 2.8 | `[lane:fast]` `[tdd:skip:config-surface]` 環境変数 8 個を §5-3 の表どおり実装。`HERDR_TAB_MARKER_TITLE=0` で LLM を一切呼ばない軽量モードが成立すること | `HERDR_TAB_MARKER_TITLE=0` で `claude` サブプロセスが起動しない（絵文字のみ付く）ことをテストで固定 | 2.7 | cc:完了 |

---

## Phase 3: 検証

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | `[lane:gate]` `[tdd:skip:verification-task]` `python3 -m unittest discover -s tests` + `uvx ruff check .` を通す | 両方 exit 0。テストは純粋関数（`marker_for` / `strip_marker` / `truncate` / `sanitize_title` / `detect_repo` / `may_overwrite_title`）を網羅 | 2.8 | cc:完了 |
| 3.2 | `[lane:gate]` `[tdd:skip:manual-smoke]` herdr 実機スモーク。`HERDR_TAB_MARKER_DEBUG=1 HERDR_TAB_MARKER_FOREGROUND=1` で実行し、実際に `tab.rename` が通ってタブ名が `<絵文字> <タイトル>` になることを目視確認 | 実タブ名が変わったことを確認。debug.log に rename 成功が記録される | 3.1 | cc:完了 |
| 3.3 | `[lane:fast]` `[tdd:skip:docs-only]` README を実装済みの内容に更新（「status: 未実装」を外す／環境変数表／`TITLE=0` 軽量モード／上流への謝辞と MIT 帰属） | README に未実装表記が残っていない。環境変数 8 個すべてが記載されている | 3.1 | cc:完了 |

---

## Phase 4: dotfiles 差し替え（**これをやらないと動かない**）

HANDOFF.md §5-6。**実装完了だけで終わらせない。** 7 箇所すべて差し替える。

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | `[lane:gate]` `[tdd:skip:deployment-step]` 本体を dotfiles へ配置。`cp herdr_tab_marker.py <dotfiles>/claude/hooks/herdr-tab-marker.py` + `chmod +x`。旧 hook を `git rm claude/hooks/herdr-auto-title.py` | 新 hook が dotfiles 追跡下にあり実行権限が付いている。旧 `.py` が削除されている | 3.2 | cc:完了 |
| 4.2 | `[lane:gate]` `[tdd:skip:config-edit]` settings 2 ファイルを **Edit で手編集**（`json.dump` で書き戻さない。§5-5 の理由）。`HERDR_AUTO_TITLE_LANG` → `HERDR_TAB_MARKER_LANG`、hook command のパスを差し替え。**Mac / WSL 両方**やる | `python3 -c "import json;[json.load(open(f)) for f in [...]]"` が OK。diff が該当行のみ（キー順が並び替わっていない） | 4.1 | cc:完了 |
| 4.3 | `[lane:fast]` `[tdd:skip:docs-only]` dotfiles の `.gitignore`（145-146 行）と `claude/AGENTS.md`（171 行 / 177-191 節）を本実装の内容に改訂 | `grep -rn "herdr-auto-title\|HERDR_AUTO_TITLE" claude/ .gitignore` が**ヒット 0 件** | 4.2 | cc:完了 |
| 4.4 | `[lane:fast]` `[tdd:skip:cleanup]` 旧 state を掃除。`rm -rf ~/.claude/herdr-auto-title/` | ディレクトリが存在しない | 4.3 | cc:完了 |
| 4.5 | `[lane:gate]` `[tdd:skip:manual-verification]` 差し替え後の確認（§5-6 の確認節）。旧参照 0 件 + 新セッションでタブ名に絵文字が付くことを目視 | 旧参照 grep が 0 件。新セッションで絵文字付きタブ名を確認（動かない場合は `HERDR_TAB_MARKER_DEBUG=1` で debug.log を確認） | 4.4 | cc:完了 |

---

## Phase 5: closeout

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 5.1 | `[lane:gate]` `[tdd:skip:review-task]` 実装レビュー（`harness-review` 相当）。特に「ハッシュ安定性」「マーカー剥がしの取りこぼし」「自己再入防止」を重点確認 | review artifact が残り、指摘が解消または理由付きで受容されている | 4.5 | cc:完了 |
| 5.2 | `[lane:fast]` `[tdd:skip:docs-only]` 実装内容の Artifact を書き出す（ユーザー依頼分）。何をどう作ったかを図解付きでまとめる | Artifact が publish され URL がユーザーに渡っている | 5.1 | cc:完了 |

---

## Spec delta

root `spec.md` は**作成しない**。理由:

HANDOFF.md が既に product contract として機能している（決定事項の表・却下案の記録・herdr 仕様の一次調査結果を持つ）。
同内容の `spec.md` を並置すると二重管理になり、precedence が曖昧になる。
本 Plans.md 冒頭で HANDOFF.md を正本として明示し、Plans.md は task ledger に徹する。

ただし以下 2 点は HANDOFF.md に**追記**する（実装で確定した事実であり、将来の判断材料になるため）:

1. §3-1 の再確認結果（0.7.3 時点で色 API がまだ無いこと / 確認日）
2. §5-3 に `HERDR_TAB_MARKER_FOREGROUND` を正式な環境変数として追加（§5-7 では「実装しておくと楽」という提案止まりだった）

---

## 事前確認

- 事項: external-send — `git commit` / `git push`（herdr-tab-marker リポジトリと dotfiles リポジトリの 2 つ）
  理由: 実装成果を commit し、dotfiles の差し替えを永続化するため
  scope: Phase 4 / Task 4.1-4.3、Phase 5 / Task 5.1

- 事項: destructive — `git rm claude/hooks/herdr-auto-title.py`（dotfiles）
  理由: 旧 hook と新 hook はタブ名を奪い合うため併存不可（HANDOFF.md §5-4）。git 履歴に残るのでロールバック可能
  scope: Phase 4 / Task 4.1

- 事項: destructive — `rm -rf ~/.claude/herdr-auto-title/`
  理由: 旧 hook の state ディレクトリ掃除（HANDOFF.md §5-6 手順 4）。生成物のみでソースは含まない
  scope: Phase 4 / Task 4.4

- 事項: destructive — 稼働中の Claude Code hook 設定の書き換え（`settings.mac.json` / `settings.wsl.json`）
  理由: hook 登録を旧実装から本実装へ差し替えるため。JSON 構文エラーは全セッションに影響するので Task 4.2 の DoD で `json.load` 検証を必須にしている
  scope: Phase 4 / Task 4.2

- 事項: external-send — `claude -p` による LLM 呼び出し（タイトル生成、`--max-budget-usd 0.05`）
  理由: タイトル生成機能の実機検証に必要。会話ログの先頭 1500 文字が Anthropic API に送られる
  scope: Phase 3 / Task 3.2

- 事項: external-send — `uvx ruff`（初回は PyPI からダウンロード）
  理由: `ruff` 未インストールのため lint baseline の実行に必要
  scope: Phase 1 / Task 1.1

- 事項: secret-read — **なし**
  理由: 本計画は `.env` / secrets / 鍵ファイルを読まない
  scope: 全 Phase

---

## team_validation_mode

`subagent`

Product / Architecture / Security / QA / Skeptic の観点は、HANDOFF.md 作成時の調査セッションで既に一巡している
（色 API の実機確認 → 絵文字方式への転換、接頭辞案・連結案・fork 案の却下記録が §6 に残る）。
本 Plans.md ではその決定を task へ変換しつつ、実装前に §3-1 / §5-6 の前提を**再実測**して裏を取った
（結果は「実装前に再確認した前提」節）。
Phase 5 / Task 5.1 で実装後レビューを別視点として通す。
