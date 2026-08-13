# herdr tab marker

[herdr](https://herdr.dev) のタブに、作業内容から生成した**タイトル**と、
作業中のリポジトリを表す**絵文字**を自動で付ける hook。

```
🐳 リポジトリ別絵文字タブ識…
^^ ^^^^^^^^^^^^^^^^^^^^^^^^
│  └─ 会話ログから生成（手動で付けた名前は尊重して上書きしない）
└──── リポジトリごとに固定（コストゼロ）
```

絵文字がリポジトリを表すので、どのタブがどのリポジトリかが一目で分かる。

## なぜ絵文字なのか

本当はタブを**色分け**したかったが、herdr 0.7.3 にタブ個別の色を設定する API は無い。
RPC は 72 メソッドあるが `tab.*` は `create` / `list` / `get` / `focus` / `rename` / `move` / `close` の 7 つで、
色・スタイル・アイコンに相当するものは存在しない。外部から設定できるのは `label`（自由文字列）だけ。
そこで色の代わりに絵文字を使い、1 文字で視覚的な弁別性を得ている。

リポジトリ名を接頭辞に付ける案（`iclp-認証調査`）もあったが、
同じリポジトリを複数タブで開くと全部同じ接頭辞になって邪魔なので採らなかった。
絵文字なら 2 カラムで済み、タイトル部分で個々のタブを区別できる。

詳しい調査結果と設計の経緯は [HANDOFF.md](HANDOFF.md) を参照。

## 機能

| 機能 | 内容 | コスト |
|------|------|--------|
| リポジトリ識別 | リポジトリごとに固有の絵文字を先頭に付ける | ゼロ（cwd から決まる） |
| タイトル自動設定 | 会話ログから短いタイトルを生成 | LLM 呼び出しあり |

`HERDR_TAB_MARKER_TITLE=0` にすると絵文字だけになり、LLM を一切呼ばない。

### 手動で付けた名前の扱い

タイトル部分と絵文字を分けて扱う。

| 対象 | 挙動 |
|------|------|
| タイトル部分 | 手動ラベルは**上書きしない**（LLM も呼ばない） |
| 絵文字 | **常に付ける** |

そのため `ssh` と手で名付けたタブは `🐳 ssh` になる。
意図的な命名を守りつつ、リポジトリ識別だけは効く。

### 絵文字の決まり方

1. `HERDR_TAB_MARKER_MAP` に明示指定があればそれを使う
2. 無ければリポジトリ名から安定ハッシュで 32 色のパレットから選ぶ

ハッシュは自前で畳み込んでいる（Python の `hash()` は実行ごとに seed が変わるため使っていない）。
同じリポジトリなら常に同じ絵文字になり、マシンをまたいでも一致する。

リポジトリ名は `git rev-parse --git-common-dir` から取る。
`--show-toplevel` ではないので、**worktree で作業していても本体と同じ絵文字**になる。

## 必要なもの

- herdr のペイン内で動いている Claude Code か Codex（`HERDR_ENV=1` が立っていること）
- `python3`（標準ライブラリのみ）
- タイトル生成を使う場合は `PATH` の通った `claude`

herdr の外で起動された場合は何もせずに終了する。

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
既存のキー順が並び替わって大きな差分が出るため、登録は手で追記する方針にした（[HANDOFF.md](HANDOFF.md) §5-5）。

反映は次に起動するセッションから。

## 環境変数

| 変数 | 既定 | 用途 |
|------|------|------|
| `HERDR_TAB_MARKER_MAP` | `{}` | `{"dotfiles":"🔧"}` 形式の固定割り当て |
| `HERDR_TAB_MARKER_MARKER` | `1` | 絵文字を付けるか |
| `HERDR_TAB_MARKER_TITLE` | `1` | タイトルを生成するか（`0` で LLM を一切呼ばない） |
| `HERDR_TAB_MARKER_LANG` | `auto` | `ja` / `en` / `auto`（ロケール追従） |
| `HERDR_TAB_MARKER_MAX_WIDTH` | `28` | タブ名の表示幅（半角 1 / 全角 2 カラム） |
| `HERDR_TAB_MARKER_REGEN_EVERY` | `0` | タイトル再生成の間隔（`0` = 最初に付けたら以後変えない） |
| `HERDR_TAB_MARKER_MODEL` | `haiku` | タイトル生成モデル |
| `HERDR_TAB_MARKER_MAX_BUDGET_USD` | `0.05` | 1 回あたりのコスト上限 |
| `HERDR_TAB_MARKER_TIMEOUT_SEC` | `90` | タイトル生成のタイムアウト |
| `HERDR_TAB_MARKER_MAX_LOG_CHARS` | `1500` | LLM に渡す会話ログの上限 |
| `HERDR_TAB_MARKER_DEBUG` | `0` | `~/.claude/herdr-tab-marker/debug.log` に出力 |
| `HERDR_TAB_MARKER_FOREGROUND` | `0` | daemonize せず前景で実行（デバッグ用） |
| `HERDR_TAB_MARKER_DISABLE` | `0` | 一時的に無効化 |

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

`HERDR_TAB_MARKER_TITLE=0` を足せば LLM を呼ばずに絵文字だけ検証できる。

## 開発

```sh
python3 -m unittest discover -s tests   # 104 tests
uvx ruff check .
```

`herdr_tab_marker.py` は単一ファイル・Python 3 標準ライブラリのみで完結している。
純粋関数（`marker_for` / `strip_marker` / `truncate` / `sanitize_title` / `detect_repo` /
`may_overwrite_title`）はソケットも LLM も要らないので単体でテストできる。

## 謝辞

設計は [sh1ma/herdr-auto-title](https://github.com/sh1ma/herdr-auto-title)（MIT）を参考にしている。
ソケット呼び出し・幅計算・タイトル整形・daemonize の考え方は同実装を踏襲した。

## ライセンス

MIT
