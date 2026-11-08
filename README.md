# herdr tab marker

[herdr](https://herdr.dev) のタブに、作業内容から生成した**タイトル**を自動で付ける hook。

```
認証まわりの調査
^^^^^^^^^^^^^^^^
会話ログから生成（手動で付けた名前は尊重して上書きしない）
```

> 0.1.x はリポジトリを表す絵文字もタブ名の先頭に付けていたが、0.2.0 で外した。
> 0.1.x が付けた絵文字は、そのタブの記録にある分だけ剥がしてから「自分の名前か」を判定する。

## 機能

| 機能 | 内容 | コスト |
|------|------|--------|
| タイトル自動設定 | 会話ログから短いタイトルを生成 | LLM 呼び出しあり |
| 話題変化での更新 | 話題が変わった時だけタイトルを作り直す | 判定はゼロ（文字列処理のみ） |

`HERDR_TAB_MARKER_TITLE=0` にすると何もしない（LLM を一切呼ばない）。

### 手動で付けた名前の扱い

手動ラベル（人や他のツールが付けた名前）には**一切触らない**。LLM も呼ばず、切り詰めもしない。
`ssh` と手で名付けたタブは `ssh` のまま、長い名前も長いまま残る。

上書きするのは、名前が空・herdr 既定の連番・前回この hook 自身が付けた名前のときだけ。

所有権の記録は**タブ単位**で持つ（`~/.claude/herdr-tab-marker/tabs/<tab_id>.json`）。
セッション単位で持つと、新しいセッションを起動した時点で
「前に自分が付けた名前」を手動ラベルと誤認し、二度と更新できなくなる。

### いつ作り直すか

既定では**話題が変わった時だけ**作り直す。

| 状況 | 挙動 |
|------|------|
| 初回 | 生成する |
| 話題が継続 | 生成しない（LLM を呼ばない） |
| 話題が変わった | 生成し直す |
| 手動ラベル | 生成しない（LLM を呼ばない） |

話題の判定に**LLM は使わない**。判定自体が有料になっては本末転倒なので、
直近プロンプトと現タイトルの語の重なりだけで決める。
日本語は形態素解析器が使えないため文字 bigram に落としている。

相づちや指示語（「ありがとう」「これも直して」）では作り直さない。
語が少なすぎるプロンプトは話題変化の根拠として弱く、
ここで生成すると話題が続いているのに毎回 LLM を呼んでしまうため。

`HERDR_TAB_MARKER_REGEN_EVERY` を明示すると、話題に関係なく
N 回ごとに作り直す従来動作に戻る（後方互換）。

## 必要なもの

- 次のどちらかの中で動いている Claude Code か Codex
  - herdr（TUI）のペイン（`HERDR_ENV=1` と `HERDR_SOCKET_PATH` が立っている）
  - Herdr のデスクトップアプリ（herdr core）のターミナル（`HC_TERMINAL_ID` が立っている）
- `python3`（標準ライブラリのみ。`fcntl` と `fork` を使うので Mac / Linux / WSL。Windows ネイティブの Python は非対応）
- タイトル生成を使う場合は `PATH` の通った `claude`

herdr の外で起動された場合は何もせずに終了する。両方の環境変数が見えるとき（herdr TUI を
デスクトップアプリのターミナルの中で動かしたとき）は、内側の herdr TUI のタブに名前を付ける。

## デスクトップアプリ（herdr core）

デスクトップアプリの裏で動くのは herdr TUI のサーバーではなく、別製品の **herdr-core**（`hc`）。
中のターミナルには `HERDR_ENV` も `HERDR_SOCKET_PATH` も渡らない。そこで次の経路で名前を付ける。

1. `herdr-core connect`（stdin/stdout で JSON-RPC を話す公式の入口）を起動する
2. `terminal.get(HC_TERMINAL_ID)` のラベル `herdr.tab` で、自分のタブ ID を知る
3. アプリが hc に保存している文書 `herdr-app` の `spaces[].tabs[].name` を、
   `doc.get` → `doc.set`（version 一致の確認付き）で書き換える。アプリは doc event を受けて画面を更新する

- 空の名前とアプリの自動命名（`Claude Code` / `Codex`）は、herdr TUI の連番と同じく上書きしてよい名前として扱う
- 所有権の記録は `tabs/hc_<タブ ID>.json`（herdr TUI のタブと混ざらないよう `hc:` を付けている）
- 文書 `herdr-app` はアプリ内部の形式なので、想定と違う形なら何もせずに終わる
- hc 0.1.18 / Herdr.app 0.1.13（macOS）で確認した

### herdr-core の探し方

| 順 | 候補 | 備考 |
|----|------|------|
| 1 | `HERDR_TAB_MARKER_HC_BIN` | 明示。`:` 区切りで複数可 |
| 2 | `PATH` 上の `herdr-core` | |
| 3 | `~/.local/bin/herdr-core` | macOS / Linux のインストール先 |
| 4 | `PATH` 上の `herdr-core.exe` | **WSL のみ**。Windows 版を interop で起動する |
| 5 | `/mnt/c/Users/*/AppData/Local/Programs/herdr-core/herdr-core.exe` | **WSL のみ**。Windows 版の既定のインストール先 |

ターミナル番号（`t7` など）は hc ごとの連番なので、WSL で Linux 版と Windows 版の両方があると
別のターミナルを拾いうる。そのため **ターミナルの pid が自分の祖先プロセスにある hc** を優先し、
それが無いとき（Windows 版の pid は WSL から確かめられない）は、該当するターミナルを返した hc が
1 つだけの場合に限って使う。タブの文書は、ターミナルとは別の hc にあってもよい
（Windows のアプリが WSL を `hc machine add` でつないでいる場合など）。

### WSL で動かないとき

- **`HC_TERMINAL_ID` が WSL に届いていない**: Windows のプロセスから `wsl.exe` を起動すると、
  環境変数は `WSLENV` に書いたものしか引き継がれない。WSL の中で `echo $HC_TERMINAL_ID` が空なら、
  Windows のユーザー環境変数 `WSLENV` に `HC_TERMINAL_ID` を足す（既にあれば `:` でつなぐ）
- **herdr-core が見つからない**: `HERDR_TAB_MARKER_HC_BIN` に Windows 版の exe のパス（`/mnt/c/...`）を渡す
- `HERDR_TAB_MARKER_DEBUG=1` で `debug.log` に、どこで諦めたかが出る

## 使い方

`UserPromptSubmit` hook として登録する。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 '/path/to/herdr-tab-marker.py'"
          }
        ]
      }
    ]
  }
}
```

`install.sh` は用意していない。`settings.json` を機械的に書き戻すと
既存のキー順が並び替わって大きな差分が出るため、登録は手で追記する方針にした。

反映は次に起動するセッションから。

## 環境変数

| 変数 | 既定 | 用途 |
|------|------|------|
| `HERDR_TAB_MARKER_TITLE` | `1` | タイトルを生成するか（`0` で LLM を一切呼ばない） |
| `HERDR_TAB_MARKER_LANG` | `auto` | `ja` / `en` / `auto`（ロケール追従） |
| `HERDR_TAB_MARKER_MAX_WIDTH` | `44` | タブ名の表示幅（半角 1 / 全角 2 カラム）。herdr の `sidebar_width` と揃える |
| `HERDR_TAB_MARKER_REGEN_EVERY` | `0` | タイトル再生成の間隔（`0` = 話題が変わった時だけ / `N` = N 回ごと） |
| `HERDR_TAB_MARKER_MAX_TAB_STATES` | `64` | 所有権レコードの保持件数（超えた分は古い順に間引く） |
| `HERDR_TAB_MARKER_MODEL` | `haiku` | タイトル生成モデル |
| `HERDR_TAB_MARKER_MAX_BUDGET_USD` | `0.05` | 1 回あたりのコスト上限 |
| `HERDR_TAB_MARKER_TIMEOUT_SEC` | `90` | タイトル生成のタイムアウト |
| `HERDR_TAB_MARKER_MAX_LOG_CHARS` | `1500` | LLM に渡す会話ログの上限 |
| `HERDR_TAB_MARKER_DEBUG` | `0` | `~/.claude/herdr-tab-marker/debug.log` に出力 |
| `HERDR_TAB_MARKER_FOREGROUND` | `0` | daemonize せず前景で実行（デバッグ用） |
| `HERDR_TAB_MARKER_DISABLE` | `0` | 一時的に無効化 |
| `HERDR_TAB_MARKER_HC_BIN` | （なし） | herdr-core の実行ファイル（デスクトップアプリ用。`:` 区切りで複数可） |

## サイドバーで複数の Claude を見分ける

1 つのタブに Claude を複数並べると、タブ名だけでは各ペインの作業が分からない。
これは **herdr 側の設定だけで解決する**（この hook の実装は要らない）。

Claude Code は自身の作業内容をターミナルタイトルに書いており、
herdr はそれを `terminal_title_stripped` として**ペインごとに**保持している。
行構成を指定すれば、そのまま読める。

`~/.config/herdr/config.toml`:

```toml
[ui]
sidebar_width = 44                        # 既定 26 だと日本語が切れる
show_agent_labels_on_pane_borders = true

[ui.sidebar.agents]
row_gap = 0

[ui.sidebar.agents.rows_by_agent]
claude = [["state_icon", "workspace", "tab"], ["terminal_title_stripped"], ["agent"]]
```

反映を確かめる:

```sh
herdr config check   # => config: ok
```

行に置けるトークンは `state_icon` / `state_text` / `workspace` / `tab` /
`agent` / `terminal_title` / `terminal_title_stripped`。
herdr 0.8 系で確認している。

`sidebar_width` は hook 側の `HERDR_TAB_MARKER_MAX_WIDTH`（既定 44）と揃える。
既定の 26 のままだと日本語タイトルが不自然に切れる。

### ペイン名を付けない理由

「hook が `agent.rename` でペインに日本語名を付ける」案は**採らなかった**。

herdr の agent 名は `[a-z][a-z0-9_-]{0,31}` に制限されており、
**日本語を受け付けない**（実機で `invalid_agent_name` を確認）。

一方 `terminal_title` は既にペインごとに存在し、常に最新に保たれている。
上記の設定だけで目的（複数 Claude の識別）が達成できるので、
コードを増やす理由がない。

## デバッグ

hook として登録する前に、単体で叩いて動作を確かめられる。

```sh
export HERDR_ENV=1
export HERDR_SOCKET_PATH="$HOME/.config/herdr/herdr.sock"
export HERDR_TAB_ID="w1:t6"          # 対象タブ
export HERDR_TAB_MARKER_DEBUG=1
export HERDR_TAB_MARKER_FOREGROUND=1  # daemonize を切って結果を待つ

echo '{"session_id":"test","cwd":"'$PWD'"}' | python3 herdr_tab_marker.py
cat ~/.claude/herdr-tab-marker/debug.log
```

## 開発

```sh
python3 -m unittest discover -s tests   # 158 tests
uvx ruff check .
```

`herdr_tab_marker.py` は単一ファイル・Python 3 標準ライブラリのみで完結している。
純粋関数（`strip_legacy_marker` / `truncate` / `sanitize_title` /
`may_overwrite_title` / `topic_changed` / `should_regenerate`）は
ソケットも LLM も要らないので単体でテストできる。

## 謝辞

設計は [sh1ma/herdr-auto-title](https://github.com/sh1ma/herdr-auto-title)（MIT）を参考にしている。
ソケット呼び出し・幅計算・タイトル整形・daemonize の考え方は同実装を踏襲した。

## ライセンス

MIT
