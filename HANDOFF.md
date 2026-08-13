# 引き継ぎ資料 — herdr-tab-marker

このリポジトリは **まだ空**（このファイルと README のみ）。実装はこれから。
別セッションで `cd ~/ghq/github.com/yuhgo/herdr-tab-marker` して Claude を起動し、
このファイルを読ませれば続きから始められる。

作成日: 2026-08-13 / 調査環境: macOS, herdr 0.7.3

---

## 1. 何を作るのか

[herdr](https://herdr.dev) のタブ名を自動で整える hook。2 つの機能を 1 つの hook で提供する。

| 機能 | 内容 | コスト |
|------|------|--------|
| **タイトル自動設定** | 会話ログから短いタイトルを生成してタブ名にする | LLM 呼び出しあり |
| **リポジトリ識別** | 作業中のリポジトリごとに固有の絵文字をタブ名の先頭に付ける | ゼロ（cwd から決まる） |

最終的なタブ名の形:

```
🟦 認証まわりを調査
```

### なぜ絵文字なのか（重要な前提）

当初は「リポジトリごとにタブを**色分け**」したかった。**が、herdr にその API は無い**（§3 の調査結果）。
タブに対して外部から設定できるのは `label`（自由文字列）だけ。
そこで**色の代わりに絵文字**を使い、1 文字で視覚的な弁別性を得る方針にした。

接頭辞にリポジトリ名（`iclp-` 等）を付ける案もあったが、
**同じリポジトリを複数タブで開くと全部同じ接頭辞になって邪魔**なので却下した。
絵文字なら 2 カラムで済み、タイトル部分で個々のタブを区別できる。

---

## 2. 決定済みの方針

ユーザーとの合意事項。**変更するときは確認を取ること。**

| 論点 | 決定 |
|------|------|
| 色 vs 絵文字 | **絵文字**（色 API が無いため） |
| タブ名の構成 | **`<絵文字> <タイトル>`**（例: `🟦 認証まわりを調査`） |
| リポジトリ名の接頭辞 | **付けない**（重複して邪魔なため） |
| 上流との関係 | **フルスクラッチ**。`herdr-auto-title` は参考にするが依存しない |
| 手動ラベルの扱い | **絵文字は常に付ける。タイトル部分は手動ラベルを尊重して上書きしない** |
| リポジトリ | `github.com/yuhgo/herdr-tab-marker`（作成済み・public・空） |

### 手動ラベルの扱いを分けた理由

上流 `herdr-auto-title` は「手で付けたラベルには一切触らない」設計。
そのため `ssh` という名前を手で付けたタブでは**何も起きなかった**（§4 の調査で判明）。

本実装では2つを分離する:

- **タイトル部分** → 手動ラベルを尊重（上書きしない）
- **絵文字** → 常に付ける（`ssh` → `🟦 ssh`）

こうすると「意図的な命名を守りつつ、リポジトリ識別は効く」が両立する。

---

## 3. herdr のタブ仕様 — 調査済みの事実

**herdr 0.7.3 で実機確認済み。** 一次情報は herdr 本体の実装。

### 3-1. RPC メソッドは全 72 個、タブの色を設定する口は無い

存在しないメソッド名を投げるとエラーに全メソッド名が列挙されるので、そこから取得した。

```python
# 全メソッド一覧を取り直す方法
import json, socket, re
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2)
s.connect("/Users/yamamotoyugo/.config/herdr/herdr.sock")
s.sendall((json.dumps({"id":"x","method":"__no__","params":{}})+"\n").encode())
buf = b""
while b"\n" not in buf:
    c = s.recv(65536)
    if not c: break
    buf += c
msg = json.loads(buf.decode().splitlines()[0])["error"]["message"]
print(re.findall(r"`([a-z_]+\.[a-z_]+|ping)`", msg))
```

`tab.*` は **7 つだけ**:

```
tab.create  tab.list  tab.get  tab.focus  tab.rename  tab.move  tab.close
```

**色・スタイル・アイコンに類するメソッドは存在しない。**

### 3-2. `tab.rename` に `color` を渡しても無視される

```python
rpc("tab.rename", {"tab_id": "w1:t4", "label": "probe", "color": "red"})
# → エラーにならず、返る tab_info に色フィールドは無い。label だけ変わる。
```

### 3-3. タブが持つフィールド（`tab.get` / `tab.list` の返却）

```json
{
  "tab_id": "w1:t4", "workspace_id": "w1", "number": 4,
  "label": "ssh", "focused": false, "pane_count": 1, "agent_status": "idle"
}
```

**色に相当するフィールドは無い。** 外部から制御できるのは実質 `label` のみ。

なお `agent_status`（idle / working）は herdr 側が見た目に反映していると思われる。
つまり「タブごとに見た目を変える」内部機構はあるが、**外部に開かれていない**。

### 3-4. config.toml の `[theme]` は UI 全体の設定

`catppuccin` などの組み込みテーマと `accent` 色。**タブ個別ではない**ので今回の用途には使えない。

### 3-5. RPC の注意点

- **`id` は文字列必須。** 数値や省略は `invalid_request` で弾かれる
- ソケットは `$HERDR_SOCKET_PATH`（例: `~/.config/herdr/herdr.sock`）
- 1 行 JSON を送って 1 行 JSON が返る（改行区切り）
- `nc -U` はサンドボックスでブロックされることがある。Python の `socket` を使うのが確実

### 3-6. hook に渡る環境変数

```
HERDR_ENV=1                    # herdr 配下で動いている印。これが無ければ即 exit 0
HERDR_SOCKET_PATH=/Users/yamamotoyugo/.config/herdr/herdr.sock
HERDR_PANE_ID=w1:p6
HERDR_TAB_ID=w1:t4             # 古い herdr には無い場合があるので pane.get で引く経路も要る
HERDR_WORKSPACE_ID=w1
```

---

## 4. 上流 herdr-auto-title の調査で分かったこと

上流: `~/ghq/github.com/sh1ma/herdr-auto-title`（clone 済み・MIT）
現在 dotfiles に導入済み: `~/.claude/hooks/herdr-auto-title.py`

### 4-1. 「タブ名が変わらない」の原因はバグではなく仕様

`HERDR_AUTO_TITLE_DEBUG=1` で `~/.claude/herdr-auto-title/debug.log` に:

```
tab w1:t4 has a manual label 'ssh'; leaving it alone
```

`may_overwrite()`（上流 `herdr_auto_title.py:515`）が上書き可能なタブ名を 3 つに限定している:

1. ラベルが空
2. herdr 既定ラベル（`AUTO_LABEL_RE = ^\d+$` の連番、上流 `:512`）
3. 前回この hook 自身が付けたタイトル（state の `title` と一致）

`ssh` はどれにも該当せず「手動ラベル」と判定され早期 return。
**タイトル生成（`claude -p`）に到達しておらず、API コールも発生していない。**

### 4-2. 上流のフロー（どこで止まったか）

| フェーズ | 結果 |
|---|---|
| `HERDR_ENV=1` 判定 | 通過 |
| ロック取得（`fcntl.flock`） | 通過 |
| `due` 判定（初回なので生成対象） | 通過 |
| タブ ID 解決（`w1:t4`） | 通過 |
| **手動ラベルガード** | ← ここで早期 return |
| タイトル生成 | 未到達 |
| `tab.rename` | 未到達 |

### 4-3. 参考にする価値のある上流の実装（行番号は上流ファイル）

フルスクラッチとはいえ、以下は素直に踏襲してよい（MIT なので問題なし。ただし**流用するならライセンス表記を入れる**）。

| 箇所 | 行 | 何が良いか |
|------|-----|-----------|
| `herdr_call()` | `:123` | ソケット 1 往復の最小実装。id を文字列にする点も含む |
| `resolve_tab_id()` | `:158` | `HERDR_TAB_ID` → 無ければ `pane.get` で引くフォールバック |
| `display_width()` / `truncate()` | `:317` / `:321` | 全角を 2 カラムで数える幅計算。タブバー幅に収める要 |
| `sanitize_title()` | `:335` | LLM 出力から前置きや引用符を落として 1 行にする |
| `state_path()` | `:493` | session_id をファイル名に使うためのサニタイズ |
| `daemonize()` | `:612` | **重要**。fork して hook をブロックしない。LLM 呼び出しは数秒かかる |
| `detect_agent()` | `:531` | `turn_id` の有無で Claude / Codex を判別 |
| `run()` の冒頭 | `:541` | `fcntl.flock` で多重起動を防ぐ |

### 4-4. LLM 呼び出しの構成（上流 `:393`）

```
claude -p --safe-mode --model haiku --tools "" \
  --no-session-persistence --max-budget-usd 0.05 \
  --system-prompt <タイトル生成指示>
```

- `--safe-mode` が**特に重要**: CLAUDE.md / skills / plugins / hooks / MCP を読まない。
  起動が速いだけでなく、**この呼び出しが自分自身の hook を再入させない**
- `--tools ""` でツールなし、`--model haiku` で最安
- 子プロセスの env から `HERDR_ENV` 等を削る（上流 `child_env()`）のも再入防止

### 4-5. 上流の既定値

| 変数 | 既定 | 意味 |
|------|------|------|
| `MAX_WIDTH` | 28 | タブ名の表示幅（半角 1 / 全角 2） |
| `REGEN_EVERY` | **0** | **0 = 最初に付けたら以後変えない**（毎プロンプトではない） |
| `TIMEOUT_SEC` | 90 | LLM 呼び出しのタイムアウト |
| `MAX_BUDGET_USD` | 0.05 | 1 回あたりのコスト上限 |
| `MAX_LOG_CHARS` | 1500 | LLM に渡す会話ログの上限 |

---

## 5. 実装方針（未着手・ここから作る）

### 5-1. ファイル構成（案）

```
herdr-tab-marker/
├── herdr_tab_marker.py   # 本体（単一ファイル・標準ライブラリのみ）
├── install.sh            # hook 登録スクリプト（§5-5 の注意あり）
├── tests/
├── README.md
├── HANDOFF.md            # このファイル
└── LICENSE               # MIT
```

上流同様 **Python 3 標準ライブラリのみ**、単一ファイルで完結させる方針。

### 5-2. 中核となるロジック

**マーカー（絵文字）の決定**

```
リポジトリ名 → 絵文字
  1. HERDR_TAB_MARKER_MAP（環境変数の JSON）に明示があればそれ
  2. 無ければリポジトリ名のハッシュで PALETTE から選ぶ
```

ハッシュは **Python の `hash()` を使わないこと**。実行ごとに seed が変わるので絵文字が毎回変わってしまう。
自前で畳み込む（例: `digest = (digest * 131 + ord(ch)) & 0xFFFFFFFF`）。
こうすると同じリポジトリなら常に同じ絵文字、マシンをまたいでも一致する。

**リポジトリ名の検出**

`git rev-parse --git-common-dir` を使う。`--show-toplevel` では**なく**。
worktree で作業しているとき `--show-toplevel` は worktree 側のディレクトリ名を返すが、
`--git-common-dir` はリポジトリ本体を指すので、worktree でも同じ絵文字になる。

cwd は hook 入力の `cwd` フィールドから取れる。

**マーカーの貼り替え**

タブ名から既存マーカーを剥がす必要がある（毎回先頭に足すと `🟦🟦🟦 ...` になる）。
「絵文字とは何か」を正規表現で書き切るのは無理なので、
**自分が付けうるもの（PALETTE + マッピング表の値）だけ**を剥がす対象にする。

**手動ラベル判定はマーカーを剥がしてから**

`🟦 ssh` の `ssh` 部分を見て判定する。剥がさずに判定すると前回自分が付けた名前を
手動ラベルと誤認する。

**幅の配分**

`MAX_WIDTH` からマーカー分（絵文字 2 カラム + 空白 1 = 3）を引いた残りがタイトルの予算。

### 5-3. 環境変数（案）

上流と衝突しないよう `HERDR_TAB_MARKER_` プレフィックスにする。

| 変数 | 既定 | 用途 |
|------|------|------|
| `HERDR_TAB_MARKER_MAP` | `{}` | `{"dotfiles":"🔧"}` 形式の固定割り当て |
| `HERDR_TAB_MARKER_MARKER` | `1` | 絵文字を付けるか |
| `HERDR_TAB_MARKER_TITLE` | `1` | タイトルを生成するか（`0` で LLM を一切呼ばない軽量モード） |
| `HERDR_TAB_MARKER_LANG` | `auto` | `ja` / `en` / `auto`（ロケール追従） |
| `HERDR_TAB_MARKER_MAX_WIDTH` | 28 | タブ名の表示幅 |
| `HERDR_TAB_MARKER_REGEN_EVERY` | 0 | タイトル再生成の間隔 |
| `HERDR_TAB_MARKER_MODEL` | `haiku` | タイトル生成モデル |
| `HERDR_TAB_MARKER_DEBUG` | `0` | `~/.claude/herdr-tab-marker/debug.log` に出力 |

`HERDR_TAB_MARKER_TITLE=0` で「絵文字だけ・LLM なし」で使えるようにしておくと、
コストゼロで運用したい人にも使ってもらえる。

### 5-4. 上流との共存をどうするか（**未決定・要判断**）

現在 dotfiles には `herdr-auto-title` が導入済みで、**両方入れると同じ label を奪い合う**。

選択肢:

- **A: 乗り換え** — `herdr-auto-title` の hook 登録を外し、本実装に一本化（すっきりする）
- **B: 併存** — 本実装を `HERDR_TAB_MARKER_TITLE=0`（絵文字のみ）にして、タイトルは上流に任せる
  - ただし上流は自分が付けた名前でないと上書きしないので、絵文字を足すと上流が「手動ラベル」と誤認して止まる可能性が高い。**おそらく破綻する**

→ **A（乗り換え）が素直。** 実装が動いたら dotfiles 側の hook 登録を差し替える。

### 5-5. install.sh を書くときの注意（**dotfiles 環境固有・重要**）

上流の `install.sh` をそのまま真似ると、この環境では困ったことになる。

`~/.claude/settings.json` は **dotfiles の `settings.mac.json` への symlink**（git 追跡外）。
上流の install.sh はこれを Python の `json.dump` で書き戻すため:

1. **ファイル全体のキー順が並び替わり、179 行規模の無意味な diff が出る**
2. `settings.wsl.json` には触れないので、WSL 側は結局手で入れる必要がある

そのため今回 `herdr-auto-title` を導入したときは **install.sh を使わず、hook 登録は手で Edit** した。
経緯は dotfiles の `claude/AGENTS.md`「herdr 連携」節に記載済み。

本実装の install.sh を書くなら:

- **既存のキー順を壊さない**書き方にする（`json.dump` で全体を書き戻さない）か、
- そもそも「settings に追記する 1 行」を出力するだけにして、登録は利用者に任せる

のどちらかが良い。汎用リポジトリとして公開するなら前者が親切だが、
**自分の環境で使う分には後者で十分**。

### 5-6. テスト方針

上流は `python3 -m unittest discover -s tests` + `ruff` + `shellcheck` の構成。
同じで良い。純粋関数（`marker_for` / `strip_marker` / `truncate` / `sanitize_title` /
`detect_repo` / `may_overwrite_title`）はソケットも LLM も要らないので普通に単体テストできる。

**herdr 実機での確認方法**:

```sh
# 前景で実行してログを見る（daemonize を切る）
export HERDR_TAB_MARKER_DEBUG=1
export HERDR_TAB_MARKER_FOREGROUND=1   # そういうフラグを実装しておくと楽
echo '{"session_id":"test","cwd":"'$PWD'","prompt":"テスト"}' | python3 herdr_tab_marker.py
```

hook を settings に登録する前に、この形で単体で叩けるようにしておくとデバッグが速い。

---

## 6. 却下した案（蒸し返さないための記録）

| 案 | 却下理由 |
|----|---------|
| タブを色分け | **herdr に API が無い**（§3） |
| リポジトリ名を接頭辞に付ける | 同じリポジトリを複数開くと全部同じ接頭辞になり邪魔 |
| 略称マッピング（`innocom-learning-platform` → `iclp`） | 接頭辞案の派生。接頭辞自体を却下したので不要に。<br>なお `iclp` は頭文字から機械的には導けない（機械的だと `ilp`）ため手動表が必須だった |
| 手動ラベル + 自動ラベルの連結（`ssh-認証調査`） | 再生成時の接頭辞復元、区切り文字、幅配分の設計が重く、方針変更で取りやめ |
| herdr 本体に色 API を要望する issue | 実装時期が読めず待てない。将来入ったら移行を検討する余地はある |
| 上流を fork して拡張 | 上流の「手動ラベルには触らない」設計に引きずられる。フルスクラッチを選択 |

---

## 7. 参照先

| 対象 | 場所 |
|------|------|
| 上流 herdr-auto-title | `~/ghq/github.com/sh1ma/herdr-auto-title`（MIT） |
| 現在稼働中の hook | `~/.claude/hooks/herdr-auto-title.py` |
| その state / ログ | `~/.claude/herdr-auto-title/`（`<session_id>.json`, `debug.log`） |
| hook 登録箇所 | dotfiles `claude/settings.mac.json` の `UserPromptSubmit` |
| dotfiles の関連ドキュメント | dotfiles `claude/AGENTS.md`「herdr 連携」節 |
| herdr 本体 | `~/.local/bin/herdr`（0.7.3）、設定は `~/.config/herdr/config.toml` |

**herdr の仕様は 0.7.3 時点の実機調査に基づく。** バージョンが上がると
RPC メソッドが増えている可能性があるので、色 API の有無は §3-1 の方法で
**実装前に再確認すること**（色 API が入っていれば方針ごと見直す価値がある）。
