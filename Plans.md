# herdr-tab-marker Plans.md

作成日: 2026-08-27

タブ名をリアルタイム更新し、サイドバーで複数 Claude を識別できるようにする。
product contract は `spec.md`。precedence は `spec.md > Plans.md`。

---

## Stage gate

| Stage | 内容 | 対応 Task |
|-------|------|----------|
| 1. 検証・調査 | herdr 0.8.0 の API と config を実機確認 | 1.1（完了済み） |
| 2. 実装計画確定 | lane / DoD 確定 | 本ファイル |
| 3. 実装(TDD) | A → B-2 → D → C | 2.1〜2.4, 3.1, 4.1 |
| 4. レビュー | 全体レビュー | 5.1 |
| 5. PR closeout | evidence → PR | 5.2 |

---

## Phase 1: 検証・調査（完了済み）

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | `[lane:fast]` `[tdd:skip:investigation-only]` herdr 0.8.0 の RPC / config を実機確認。`agent.rename` の文字種制限、`terminal_title` の存在、`ui.sidebar.agents` の可否を確定 | 確認結果が spec.md §5 に反映されている | - | cc:完了 |

### 1.1 の調査結果（確定した事実）

- herdr は 0.7.3 稼働中、`release-notes.json` に **0.8.0** あり（channel: stable）
- RPC は **95 メソッド**（README 記載の 72 は 0.7.3 時点の値で、現在は古い）
- `agent.rename` は `target` 必須、名前は `[a-z][a-z0-9_-]{0,31}` で **日本語不可**
- `terminal_title` / `terminal_title_stripped` が **ペインごとに存在し常に最新**
- `ui.sidebar.agents.rows_by_agent` で claude 専用の行構成を指定できる
- 現行 state は session_id 単位（`~/.claude/herdr-tab-marker/<session_id>.json`）

### unknown_data（未確認。absent と断定しない）

- `pane.report_metadata` の正確なフィールド形状（3 通り試して `invalid_metadata_request`）
- `--custom-status` が UI のどこに出るか（`agent list` には現れず）
- 0.8.0 実機での `ui.sidebar.agents` 描画（0.7.3 稼働中のため未検証）

---

## Phase 2: A — セッション跨ぎの所有権修正（最優先・他の前提）

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 2.1 | `[lane:gate]` `[tdd:required]` タブ単位の所有権レコードを導入。`~/.claude/herdr-tab-marker/tabs/<tab_id>.json` に `title` を保存し、session state と分離する | 新規テストが「別 session_id でも自分の付けたタイトルは上書き可」を検証し失敗→通過する | 1.1 | cc:TODO |
| 2.2 | `[lane:gate]` `[tdd:required]` `may_overwrite_title()` の判定元をタブ所有権レコードに切り替える。手動ラベルの保護は維持 | 手動ラベル(`ssh`)が保護され、かつ別セッションの自前タイトルが更新される両方がテストで通る | 2.1 | cc:TODO |
| 2.3 | `[lane:fast]` `[tdd:required]` 所有権レコードの GC。古いタブのレコードを起動時に間引く | 上限件数を超えたレコードが削除されることをテストで検証。既存 32 件の残骸が増え続けない | 2.1 | cc:TODO |
| 2.4 | `[lane:fast]` `[tdd:skip:no-behavior-change]` 既存の smoke 用 state 残骸(`smoke-*.json` 等)を掃除 | `~/.claude/herdr-tab-marker/` に検証用ゴミが残っていない | 2.3 | cc:TODO |

**なぜ最優先か**: これを直さないと B-2 を入れても新セッションでは更新されない。
現行動作は実機で再現済み（`may_overwrite → False`）。

---

## Phase 3: B-2 — 話題変化での再生成

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 3.1 | `[lane:gate]` `[tdd:required]` 話題変化検知を実装。直近プロンプトと現タイトルの語の重なりが閾値未満なら再生成。判定に LLM を使わない | 「同話題→生成しない / 別話題→生成する」がテストで通る。純粋関数として単体テスト可能 | 2.2 | cc:TODO |
| 3.2 | `[lane:gate]` `[tdd:required]` `REGEN_EVERY` の既定を話題変化モードに変更。明示指定時は従来の N 回ごとを維持（後方互換） | 環境変数未設定＝話題変化モード、`REGEN_EVERY=3`＝従来動作、の両方がテストで通る | 3.1 | cc:TODO |

**設計制約**: 判定は文字列処理のみ。LLM 呼び出しを増やさないことが B-2 の存在理由。

---

## Phase 4: D — サイドバー設定（最小コスト・最大効果）

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 4.1 | `[lane:fast]` `[tdd:skip:config-only-no-code]` herdr 0.8.0 へ更新し、`~/.config/herdr/config.toml` にサイドバー設定を追加。`rows_by_agent.claude` に `terminal_title_stripped` を入れる | `herdr --version` が 0.8.0。サイドバーに各ペインの作業内容が表示されることを目視確認 | 1.1 | cc:TODO |
| 4.2 | `[lane:fast]` `[tdd:skip:config-only-no-code]` `sidebar_width` / `agent_panel_sort` / `show_agent_labels_on_pane_borders` を調整し、hook 側 `MAX_WIDTH` と幅を整合させる | 日本語タイトルが不自然に切れない幅になっている | 4.1 | cc:TODO |

**注**: 4.1 は dotfiles 管理外（`~/.config/herdr/config.toml` は herdr 側）。
設定内容は本リポジトリの README に記録し、再現可能にする。

---

## Phase 5: C — 複数 Claude の識別（設計変更あり）

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 5.1 | `[lane:fast]` `[tdd:skip:superseded-by-4.1]` C（pane ごとの agent name 付与）を **実装しない**判断を README / spec に記録 | `agent.rename` が日本語不可であること、`terminal_title` で代替できることが文書化されている | 4.1 | cc:TODO |

**当初案からの変更**: C は「hook が `agent.rename` でペイン名を付ける」案だった。
調査の結果 **`agent.rename` は日本語を受け付けない**ため実装不可。
一方 herdr は既に `terminal_title` をペインごとに保持しており、
**Task 4.1 の config 設定だけで C の目的（複数 Claude の識別）が達成される**。
コードを書かずに済むので、実装ではなく判断の記録に置き換える。

---

## Phase 6: レビュー・closeout

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 6.1 | `[lane:gate]` `[tdd:skip:review-task]` 全体レビュー。ruff + 全テスト + 実機での動作確認 | `uvx ruff check .` エラー 0、`python3 -m unittest discover -s tests` 全通過、実タブでタイトルが話題変化に追従することを確認 | 3.2, 4.2, 5.1 | cc:TODO |
| 6.2 | `[lane:gate]` `[tdd:skip:docs-only]` README 更新（環境変数表・herdr 0.8.0 前提・サイドバー設定例）と dotfiles への持ち込み | README が新しい既定を説明。`cp herdr_tab_marker.py <dotfiles>/claude/hooks/herdr-tab-marker.py` 済み | 6.1 | cc:TODO |
| 6.3 | `[lane:release]` `[tdd:skip:release-task]` PR closeout | PR body に evidence（テスト結果・実機確認）が載っている | 6.2 | cc:TODO |

---

## Spec delta

`spec.md` を新規作成した（このリポジトリに product contract が無かった）。

| 節 | 内容 |
|----|------|
| §2.2 | 所有権レコードを **タブ単位**と定義。session 単位は不具合と明記 |
| §2.3 | 更新タイミングを「話題が変わったら」と定義。LLM での判定を禁止 |
| §5 | サイドバーは herdr config で実現。`terminal_title` が既に存在する事実を記録 |
| §5.3 | `agent.rename` を使わない理由（日本語不可）を明記 |
| §6 | 非目標に「タブ色分け」「状態のタブ名反映」「report_metadata」を列挙 |

---

## 事前確認

- 事項: `~/.config/herdr/config.toml` の書き換え（Task 4.1 / 4.2）
  理由: サイドバー行構成の設定に必要。herdr 管理下の設定ファイルで、本リポジトリ外
  scope: Phase 4 / Task 4.1, 4.2

- 事項: `herdr update` による 0.7.3 → 0.8.0 更新（Task 4.1）
  理由: `ui.sidebar.agents` が 0.8.0 の機能のため。稼働中セッションの再接続が発生する
  scope: Phase 4 / Task 4.1

- 事項: `~/.claude/herdr-tab-marker/` 配下のファイル削除（Task 2.3 / 2.4）
  理由: 古い state レコードの GC と検証用ゴミの掃除。hook のローカル state のみ
  scope: Phase 2 / Task 2.3, 2.4

- 事項: dotfiles への `cp`（Task 6.2）
  理由: hook 本体を実運用先へ反映するため。`<dotfiles>/claude/hooks/herdr-tab-marker.py`
  scope: Phase 6 / Task 6.2

- 事項: `git push` / `gh pr create`（Task 6.3）
  理由: PR closeout。public リポジトリへの外部送信
  scope: Phase 6 / Task 6.3

---

## team_validation_mode

`manual-pass`

Product / Architecture / Security / QA / Skeptic の各観点を単独で分けて評価した。

| 観点 | 結果 |
|------|------|
| Product | 元要望（リアルタイム更新・複数 Claude 識別・サイドバー活用）を全てカバー。C は目的を保ったまま手段を変更 |
| Architecture | A が B-2 の前提という依存を Depends に明示。state のタブ単位化は既存の純粋関数設計を壊さない |
| Security | 秘密情報の読み取り無し。外部送信は PR closeout のみで事前確認済み。LLM に渡すのは会話ログのみで既存と同じ |
| QA | 既存 148 テストが回帰の網。新規は全て純粋関数として単体テスト可能。lint(ruff)/test 基盤は既存 |
| Skeptic | 「毎回 LLM を呼ぶ」案を退けた（コスト）。`agent.rename` 案を実機検証で棄却した。`terminal_title` の発見で C の実装が不要になった点が最大の節約 |

lint / formatter baseline: `ruff.toml` あり（setup task 不要）
test framework: `unittest`（148 tests 通過中）
