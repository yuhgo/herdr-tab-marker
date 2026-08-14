#!/usr/bin/env python3
"""herdr のタブ名に「リポジトリを表す絵文字」と「作業内容のタイトル」を付ける hook。

    🟦 認証まわりを調査
    ^^ ^^^^^^^^^^^^^^^
    │  └─ 会話ログから LLM が生成（手動ラベルは尊重して上書きしない）
    └──── 作業中のリポジトリから決まる（コストゼロ・常に付ける）

herdr 0.7.3 にはタブ個別の色を設定する API が無く、外部から触れるのは label だけなので、
色の代わりに絵文字で視覚的な弁別性を得ている（herdr 0.7.3 で確認）。

Claude Code / Codex の UserPromptSubmit hook として呼ばれる。herdr の外では何もしない。

設計は sh1ma/herdr-auto-title (MIT) を参考にしている。
ソケット呼び出し・幅計算・タイトル整形・daemonize の考え方は同実装を踏襲した。
"""

from __future__ import annotations

import fcntl
import json
import locale
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

__version__ = "0.1.0"
SOURCE = "herdr-tab-marker"

# ---------------------------------------------------------------- 設定

STATE_DIR = Path.home() / ".claude" / SOURCE

JA = "ja"
EN = "en"
CLAUDE = "claude"
CODEX = "codex"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"HERDR_TAB_MARKER_{name}", default)


def _env_flag(name: str, default: str = "0") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _resolve_lang(raw: str) -> str:
    """`auto` はロケール追従。それ以外は ja / en に丸める。"""
    value = raw.strip().lower()
    if value in (JA, EN):
        return value
    # auto や未知の値はロケールから決める
    for candidate in (os.environ.get("LC_ALL"), os.environ.get("LANG")):
        if candidate and candidate.lower().startswith("ja"):
            return JA
    try:
        current = locale.getlocale()[0] or ""
    except ValueError:
        current = ""
    return JA if current.lower().startswith("ja") else EN


MARKER_ENABLED = _env_flag("MARKER", "1")
TITLE_ENABLED = _env_flag("TITLE", "1")
MAX_WIDTH = _env_int("MAX_WIDTH", 28)
REGEN_EVERY = _env_int("REGEN_EVERY", 0)
TIMEOUT_SEC = _env_int("TIMEOUT_SEC", 90)
MAX_LOG_CHARS = _env_int("MAX_LOG_CHARS", 1500)
MAX_BUDGET_USD = _env("MAX_BUDGET_USD", "0.05")
MODEL = _env("MODEL", "haiku")
LANG = _resolve_lang(_env("LANG", "auto"))
DEBUG = _env_flag("DEBUG")
FOREGROUND = _env_flag("FOREGROUND")

# マーカーに使う絵文字。
#
# 選定条件:
#   - 表示幅がちょうど 2 カラム（East Asian Width が W か F）
#   - 単体で完結する（肌の色や性別の修飾子・ZWJ 結合を持たない）
#   - 互いに見分けやすい
#
# 順番を変えると既存タブの絵文字が変わるので、追加は末尾に行う。
PALETTE = (
    "🟦", "🟩", "🟨", "🟧", "🟥", "🟪", "🟫", "⬛",
    "🔷", "🔶", "🔺", "🔻", "⭐", "🌙", "🔥", "💧",
    "🌱", "🍀", "🌸", "🍁", "🐳", "🐙", "🦊", "🐰",
    "🍎", "🍊", "🍋", "🍇", "⚡", "❄", "🎯", "🎲",
)

# ---------------------------------------------------------------- ログ


def log(message: str) -> None:
    if not DEBUG:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "debug.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 表示幅


def display_width(text: str) -> int:
    """タブバーに占めるカラム数を数える。全角と絵文字は 2 カラム。"""
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in "WF":
            width += 2
        elif unicodedata.combining(ch):
            # 結合文字はそれ自体では幅を持たない
            continue
        elif ord(ch) >= 0x2190 and unicodedata.category(ch) == "So":
            # 記号・絵文字は端末側でおおむね 2 カラム扱いされる
            width += 2
        else:
            width += 1
    return width


TRIM_CHARS = " \t　\"'`「」『』【】[]()<>*#-—…。．.:："


def truncate(text: str, max_width: int) -> str:
    """max_width カラムに収める。切ったことが分かるよう末尾に … を付ける。"""
    if display_width(text) <= max_width:
        return text
    if max_width <= 1:
        return "…"[:max_width] if max_width else ""
    out: list[str] = []
    width = 0
    for ch in text:
        step = display_width(ch)
        # … の 1 カラム分を残す
        if width + step > max_width - 1:
            break
        out.append(ch)
        width += step
    return "".join(out).rstrip(TRIM_CHARS) + "…"


# ---------------------------------------------------------------- マーカー

PREFIX_RE = re.compile(r"^(タイトル|title)\s*[:：]\s*", re.IGNORECASE)


def parse_marker_map(raw: str | None) -> dict:
    """HERDR_TAB_MARKER_MAP を読む。壊れていても hook 全体は落とさない。"""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log(f"HERDR_TAB_MARKER_MAP is not valid JSON: {str(raw)[:120]!r}")
        return {}
    if not isinstance(parsed, dict):
        log("HERDR_TAB_MARKER_MAP is not a JSON object; ignoring")
        return {}
    return {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str) and v}


def _digest(text: str) -> int:
    """安定ハッシュ。

    組み込みの hash() は PYTHONHASHSEED で実行ごとに変わるため使えない。
    同じリポジトリなら常に同じ絵文字、マシンをまたいでも一致させたいので自前で畳み込む。
    """
    value = 0
    for ch in text:
        value = (value * 131 + ord(ch)) & 0xFFFFFFFF
    return value


def marker_for(repo: str | None, marker_map: dict) -> str | None:
    """リポジトリ名に対応する絵文字を返す。"""
    if not repo:
        return None
    explicit = marker_map.get(repo)
    if explicit:
        return explicit
    return PALETTE[_digest(repo) % len(PALETTE)]


def known_markers(marker_map: dict, previous_marker: str | None = None) -> tuple:
    """自分が付けうる絵文字の一覧。

    「絵文字とは何か」を正規表現で書き切るのは無理なので、
    剥がす対象を自分が付けうるものだけに限定する。

    previous_marker には state に記録した「前回このタブに付けた絵文字」を渡す。
    HERDR_TAB_MARKER_MAP から値を消したあとでも、それで付けたタブ名を剥がせるようにするため。
    渡さないと `🔧 dotfiles` が手動ラベルと誤認され、以後タイトルが更新されなくなる。
    """
    markers = tuple(PALETTE) + tuple(marker_map.values())
    if previous_marker and previous_marker not in markers:
        markers += (previous_marker,)
    return markers


def strip_marker(
    label: str | None, marker_map: dict, previous_marker: str | None = None
) -> str:
    """タブ名の先頭から自分が付けた絵文字を剥がす。

    毎回先頭に足すと 🟦🟦🟦 と積み上がるので、貼り替える前に剥がす。
    """
    if not label:
        return ""
    text = label
    markers = known_markers(marker_map, previous_marker)
    changed = True
    while changed:
        changed = False
        for marker in markers:
            if text.startswith(marker):
                text = text[len(marker):]
                changed = True
    return text.strip()


def compose_label(marker: str | None, title: str) -> str:
    if marker and title:
        return f"{marker} {title}"
    if marker:
        return marker
    return title


def title_budget(max_width: int, marker: str | None) -> int:
    """タイトルに使える幅。マーカー分（絵文字 2 + 空白 1）を引く。"""
    if not marker:
        return max_width
    return max(0, max_width - display_width(marker) - 1)


# ---------------------------------------------------------------- リポジトリ検出


def detect_repo(cwd: str | None, env: dict | None = None) -> str | None:
    """作業中のリポジトリ名を返す。

    `--git-common-dir` を使う理由: worktree で作業していると `--show-toplevel` は
    worktree 側のディレクトリ名（wt-feature-branch など）を返してしまい、
    同じリポジトリなのに絵文字が変わる。common dir はリポジトリ本体を指す。
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=run_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"git rev-parse failed: {exc}")
        return None
    if completed.returncode != 0:
        return None
    common_dir = completed.stdout.strip()
    if not common_dir:
        return None
    # 相対パス（"." や ".git"）で返ることがあるので cwd 基準で解決する
    path = Path(common_dir)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        path = path.resolve()
    except OSError:
        return None
    # 通常のリポジトリ: /path/to/myproject/.git → myproject
    # bare リポジトリ:   /path/to/myproject.git  → myproject
    name = path.parent.name if path.name == ".git" else path.name
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or None


# ---------------------------------------------------------------- herdr ソケット


def herdr_call(method: str, params: dict, timeout: float = 3.0) -> dict | None:
    """herdr の Unix ソケット API を 1 往復だけ呼ぶ。

    1 行 JSON を送って 1 行 JSON が返る。id は文字列でないと invalid_request で弾かれる。
    """
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        return None
    request = {
        "id": f"{SOURCE}:{time.time_ns()}",
        "method": method,
        "params": params,
    }
    buf = b""
    try:
        # with 文で抜けるので、接続失敗やタイムアウトでも fd を閉じ残さない
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall((json.dumps(request) + "\n").encode())
            while b"\n" not in buf:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        log(f"socket error on {method}: {exc}")
        return None
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        log(f"bad response for {method}: {line[:200]}")
        return None


def resolve_tab_id() -> str | None:
    """このペインが今いるタブの ID を返す。

    ペインが別ワークスペースへ移動していると環境変数の HERDR_TAB_ID は古くなるため、
    pane.get の結果を優先し、取れないときだけ環境変数に落とす。
    """
    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        response = herdr_call("pane.get", {"pane_id": pane_id})
        tab_id = (response or {}).get("result", {}).get("pane", {}).get("tab_id")
        if isinstance(tab_id, str) and tab_id:
            return tab_id
    return os.environ.get("HERDR_TAB_ID") or None


def get_tab_label(tab_id: str) -> str | None:
    response = herdr_call("tab.get", {"tab_id": tab_id})
    label = (response or {}).get("result", {}).get("tab", {}).get("label")
    return label if isinstance(label, str) else None


def rename_tab(tab_id: str, label: str) -> bool:
    response = herdr_call("tab.rename", {"tab_id": tab_id, "label": label})
    if response is None:
        return False
    if response.get("error"):
        log(f"tab.rename error: {response['error']}")
        return False
    return True


# ---------------------------------------------------------------- 会話ログ


def read_prompts(transcript_path: str | None, limit: int) -> str:
    """transcript から user 発言だけを拾って連結する。"""
    if not transcript_path:
        return ""
    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"cannot read transcript: {exc}")
        return ""
    prompts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        text = text.strip()
        # hook が挿入する system-reminder 等は会話の意図ではないので除く
        if not text or text.startswith("<"):
            continue
        prompts.append(text)
    joined = "\n".join(prompts)
    return joined[:limit]


# ---------------------------------------------------------------- タイトル生成

SYSTEM_PROMPTS = {
    JA: (
        "あなたは会話ログを読み、その作業内容を表す短いタイトルを1つだけ出力します。"
        f"制約: 全角{MAX_WIDTH // 2}文字以内 / 体言止め / 句読点・記号・引用符・絵文字を使わない / "
        "説明や前置きを書かず、タイトルだけを1行で出力する。"
    ),
    EN: (
        "Given a conversation, you output exactly one English title describing the work. "
        f"Constraints: at most {MAX_WIDTH} characters / a noun phrase with no trailing period / "
        "no symbols, quotes, punctuation or emoji / "
        "no explanation or preamble, output only the title itself on a single line."
    ),
}


def sanitize_title(raw: str, max_width: int | None = None) -> str:
    """モデル出力を 1 行のタブ名として使える形に整える。"""
    text = "".join(ch for ch in raw if ch == "\n" or unicodedata.category(ch)[0] != "C")
    # 空行を除いた最後の行を採る（思考めいた前置きが混ざった場合の保険）
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    title = PREFIX_RE.sub("", lines[-1]).strip(TRIM_CHARS)
    title = re.sub(r"\s+", " ", title)
    if not title:
        return ""
    return truncate(title, MAX_WIDTH if max_width is None else max_width)


def detect_agent(hook_input: dict) -> str:
    """hook を呼んだのが Claude Code と Codex のどちらかを見分ける。

    hook 入力のフィールドはおおむね共通だが、turn_id は Codex にしかない。
    """
    return CODEX if hook_input.get("turn_id") else CLAUDE


def child_env() -> dict:
    """子プロセスから herdr 連携の痕跡を消す。

    これを忘れると、生成のために起動した agent が自分自身の hook を呼び返す。
    """
    env = dict(os.environ)
    for key in (
        "HERDR_ENV",
        "HERDR_PANE_ID",
        "HERDR_TAB_ID",
        "HERDR_WORKSPACE_ID",
        "HERDR_SOCKET_PATH",
    ):
        env.pop(key, None)
    env.pop("CLAUDECODE", None)
    return env


def user_message_for(conversation_log: str, agent: str) -> str:
    name = "Codex" if agent == CODEX else "Claude Code"
    if LANG == JA:
        return (
            f"以下は {name} の作業セッションでユーザーが入力した発言のログです。"
            "この作業に付けるタイトルを1行で出力してください。\n\n"
            f"{conversation_log}"
        )
    return (
        f"Below is a log of the prompts a user typed during a {name} session. "
        "Output a title for this work on a single line.\n\n"
        f"{conversation_log}"
    )


def generate_title(conversation_log: str, agent: str, max_width: int) -> str:
    """claude -p を最小構成で呼び、タイトル 1 行を得る。

    --safe-mode が要点。CLAUDE.md / skills / plugins / hooks / MCP を読まないので
    起動が速く、かつこの呼び出しが自分自身の hook を再入させない。
    """
    if not conversation_log.strip():
        return ""
    command = [
        "claude",
        "-p",
        "--safe-mode",
        "--model",
        MODEL,
        "--tools",
        "",
        "--no-session-persistence",
        "--max-budget-usd",
        MAX_BUDGET_USD,
        "--system-prompt",
        SYSTEM_PROMPTS[LANG],
    ]
    try:
        completed = subprocess.run(
            command,
            input=user_message_for(conversation_log, agent),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
            # プロジェクト固有の設定を巻き込まないよう作業ディレクトリを離す
            cwd=str(Path.home()),
            env=child_env(),
        )
    except FileNotFoundError:
        log("claude command not found; skipping title generation")
        return ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"title generation failed: {exc}")
        return ""
    if completed.returncode != 0:
        log(f"claude -p failed ({completed.returncode}): {completed.stderr[:300]}")
        return ""
    return sanitize_title(completed.stdout, max_width)


# ---------------------------------------------------------------- 状態


def state_path(session_id: str | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "") or "unknown"
    return STATE_DIR / f"{safe}.json"


def load_state(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log(f"cannot save state: {exc}")


AUTO_LABEL_RE = re.compile(r"^\d+$")


def is_disposable_title(stripped_label: str | None) -> bool:
    """このタイトル部分に保持する価値がないか。

    空、または herdr が付ける既定ラベル（タブ番号の連番）が対象。
    捨てずに持ち回ると、タイトルを生成しない軽量モードで `🟦 5` のような残骸が残る。
    """
    if not stripped_label or not stripped_label.strip():
        return True
    return bool(AUTO_LABEL_RE.match(stripped_label.strip()))


def may_overwrite_title(stripped_label: str | None, previous_title: str | None) -> bool:
    """このタイトル部分を上書きしてよいか判定する。

    必ず strip_marker を通した文字列を渡すこと。絵文字が付いたままだと
    前回自分が付けた名前を手動ラベルと誤認する。

    上書きしてよいのは 3 つだけ:
      1. 空
      2. herdr 既定のラベル（連番）
      3. 前回この hook 自身が付けたタイトル
    """
    if is_disposable_title(stripped_label):
        return True
    return previous_title is not None and stripped_label.strip() == previous_title


# ---------------------------------------------------------------- 本体


def run(hook_input: dict) -> None:
    session_id = hook_input.get("session_id") or "unknown"
    path = state_path(session_id)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 同じセッションで hook が重なっても 1 つだけ通す
    lock_file = path.with_suffix(".lock")
    try:
        lock_handle = lock_file.open("w")
    except OSError as exc:
        log(f"cannot open lock file: {exc}")
        return
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run holds the lock; skipping")
        lock_handle.close()
        return
    try:
        _run_locked(hook_input, path)
    finally:
        # flock はハンドルを閉じた時点で解放される
        lock_handle.close()


def _run_locked(hook_input: dict, path: Path) -> None:
    """ロックを取った状態での本処理。"""
    state = load_state(path)
    prompt_count = int(state.get("prompt_count", 0)) + 1
    generated_at = int(state.get("generated_at_prompt", 0))
    previous_title = state.get("title")
    state["prompt_count"] = prompt_count
    save_state(path, state)

    tab_id = resolve_tab_id()
    if not tab_id:
        log("cannot resolve tab id; giving up")
        return

    marker_map = parse_marker_map(os.environ.get("HERDR_TAB_MARKER_MAP"))
    repo = detect_repo(hook_input.get("cwd"))
    marker = marker_for(repo, marker_map) if MARKER_ENABLED else None
    log(f"tab={tab_id} repo={repo!r} marker={marker!r} prompt={prompt_count}")

    current_label = get_tab_label(tab_id)
    # 手動ラベル判定は絵文字を剥がしてから行う。
    # 前回付けた絵文字も剥がす対象に含める（MAP から値を消しても取り残さないため）
    current_title = strip_marker(current_label, marker_map, state.get("marker"))

    budget = title_budget(MAX_WIDTH, marker)
    # 既定ラベル（連番）は持ち回らない。残すと `🟦 5` のような残骸になる
    title = "" if is_disposable_title(current_title) else current_title

    # タイトルを作り直すか判断する。
    # REGEN_EVERY=0 は「最初に付けたら以後変えない」。
    due = generated_at == 0 or (REGEN_EVERY > 0 and prompt_count - generated_at >= REGEN_EVERY)
    if not TITLE_ENABLED:
        log("title generation disabled; marker only")
    elif not due:
        log(f"title not due (generated_at={generated_at}, regen_every={REGEN_EVERY})")
    elif not may_overwrite_title(current_title, previous_title):
        log(f"title part {current_title!r} looks manual; leaving it alone")
    else:
        conversation_log = read_prompts(hook_input.get("transcript_path"), MAX_LOG_CHARS)
        generated = generate_title(conversation_log, detect_agent(hook_input), budget)
        if generated:
            title = generated
            state["title"] = generated
            state["generated_at_prompt"] = prompt_count
        else:
            log("title generation produced nothing; keeping current title")

    # 手動ラベルでもマーカーは付ける。ただし幅は超えないように詰める
    title = truncate(title, budget) if title else ""
    new_label = compose_label(marker, title)

    if not new_label:
        log("nothing to set; leaving tab as-is")
        return
    if new_label == current_label:
        log(f"label already {new_label!r}; no rename needed")
        return
    if rename_tab(tab_id, new_label):
        state["tab_id"] = tab_id
        # 次回この絵文字を剥がせるように覚えておく（MAP を変更されても取り残さない）
        state["marker"] = marker
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_state(path, state)
        log(f"renamed {current_label!r} -> {new_label!r}")
    else:
        log(f"rename to {new_label!r} failed")


def daemonize() -> bool:
    """親を即座に終了させ、以降の処理を Claude Code から切り離して続ける。

    タイトル生成に数秒かかるので、hook をブロックしたままにしない。

    fork できなかった場合は False を返す。daemonize は「hook を止めないため」の
    仕組みなので、その失敗自体で prompt を止めては本末転倒になる。
    """
    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError as exc:
        log(f"cannot fork ({exc}); continuing in foreground")
        return False
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)
    return True


def main() -> int:
    if "--version" in sys.argv[1:]:
        print(f"{SOURCE} {__version__}")
        return 0

    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}

    if _env_flag("DISABLE"):
        return 0
    # herdr の外では何もしない
    if os.environ.get("HERDR_ENV") != "1":
        return 0
    if not os.environ.get("HERDR_SOCKET_PATH"):
        log("HERDR_SOCKET_PATH is not set; giving up")
        return 0
    if not MARKER_ENABLED and not TITLE_ENABLED:
        return 0

    if FOREGROUND:
        run(hook_input)
        return 0

    # fork できなくてもそのまま前景で続ける。数秒 hook を待たせるほうが、
    # タブ名が付かないより望ましい
    daemonize()
    try:
        run(hook_input)
    except Exception as exc:  # 落ちても hook 本体には影響させない
        log(f"unexpected error: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
