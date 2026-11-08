#!/usr/bin/env python3
"""herdr のタブ名に「作業内容のタイトル」を付ける hook。

タイトルは会話ログから LLM が生成する（手動ラベルは尊重して上書きしない）。
以前はリポジトリを表す絵文字も先頭に付けていたが、0.2.0 で外した。

Claude Code / Codex の UserPromptSubmit hook として呼ばれる。herdr の外では何もしない。

設計は sh1ma/herdr-auto-title (MIT) を参考にしている。
ソケット呼び出し・幅計算・タイトル整形・daemonize の考え方は同実装を踏襲した。
"""

from __future__ import annotations

import fcntl
import glob
import json
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

__version__ = "0.3.0"
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
    """`auto` はロケール追従。それ以外は ja / en に丸める。

    ロケールが英語だと判別できたときだけ en にし、判別できなければ ja に倒す。
    hook は環境変数が削がれた状態で起動されることがあり、そこで en に落ちると
    日本語で使っているのに英語タイトルが付く。既定を ja に寄せて事故を防ぐ。
    """
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
    return EN if current.lower().startswith("en") else JA


TITLE_ENABLED = _env_flag("TITLE", "1")
# herdr のサイドバーの幅（sidebar_width = 44）に合わせる
MAX_WIDTH = _env_int("MAX_WIDTH", 44)
REGEN_EVERY = _env_int("REGEN_EVERY", 0)
TIMEOUT_SEC = _env_int("TIMEOUT_SEC", 90)
MAX_LOG_CHARS = _env_int("MAX_LOG_CHARS", 1500)
MAX_BUDGET_USD = _env("MAX_BUDGET_USD", "0.05")
MODEL = _env("MODEL", "haiku")
LANG = _resolve_lang(_env("LANG", "auto"))
DEBUG = _env_flag("DEBUG")
FOREGROUND = _env_flag("FOREGROUND")

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


PREFIX_RE = re.compile(r"^(タイトル|title)\s*[:：]\s*", re.IGNORECASE)


def strip_legacy_marker(label: str | None, previous_marker: str | None) -> str:
    """0.1.x がタブ名の先頭に付けた絵文字を剥がす。

    剥がすのは、そのタブの記録に残っている絵文字だけ（人が付けた絵文字は残す）。
    """
    text = (label or "").strip()
    if previous_marker and text.startswith(previous_marker):
        text = text[len(previous_marker):]
    return text.strip()


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


# ---------------------------------------------------------------- herdr core（デスクトップアプリ）
#
# Herdr.app（デスクトップ版）の裏で動くのは herdr TUI のサーバーではなく、別製品の
# herdr-core（hc）。中のターミナルには HERDR_ENV も HERDR_SOCKET_PATH も渡らず、
# 渡るのは HC_TERMINAL_ID だけ。タブはアプリが hc に保存する文書 `herdr-app` の
# spaces[].tabs[] にあり、ターミナルとは labels["herdr.tab"] でつながっている。
# 外から doc.set で書き換えると、アプリは doc event を受けて画面を更新する
# （hc 0.1.18 / Herdr.app 0.1.13 で確認）。

HC_DOC_NAME = "herdr-app"
HC_TAB_LABEL = "herdr.tab"
# 状態ファイルのキー。herdr TUI のタブ ID（w1:t4）と混ざらないよう接頭辞を付ける
HC_KEY_PREFIX = "hc:"
HC_TIMEOUT_SEC = 10
HC_SET_RETRIES = 3
# アプリが自動で付けるタブ名。空と同じく「人が付けた名前ではない」として扱う
HC_AUTO_NAMES = frozenset({"Claude Code", "Codex"})

# daemonize すると親が init に付け替わり祖先をたどれなくなるので、fork 前に記録する
_ANCESTOR_PIDS: frozenset = frozenset()


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _which(name: str, path: str | None = None) -> str | None:
    found = shutil.which(name, path=path)
    return found if found else None


def hc_candidates() -> list:
    """herdr-core の実行ファイルの候補を、手元に近い順に返す。

    - Mac / Linux: PATH 上の herdr-core、なければ ~/.local/bin/herdr-core
    - WSL: Linux 版に加えて、Windows 版の herdr-core.exe（interop で直接起動できる）。
      Windows でアプリを使うと、タブの文書は Windows 側の hc にある
    - HERDR_TAB_MARKER_HC_BIN で明示したものは最優先（os.pathsep 区切りで複数可）
    """
    raw = []
    raw.extend(p for p in _env("HC_BIN").split(os.pathsep) if p)
    raw.append(_which("herdr-core"))
    raw.append(str(Path.home() / ".local" / "bin" / "herdr-core"))
    if is_wsl():
        raw.append(_which("herdr-core.exe"))
        raw.extend(
            sorted(glob.glob("/mnt/c/Users/*/AppData/Local/Programs/herdr-core/herdr-core.exe"))
        )
    seen = set()
    result = []
    for path in raw:
        if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
            continue
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def hc_rpc(binary: str, calls: list, timeout: float = HC_TIMEOUT_SEC) -> list:
    """`herdr-core connect` を 1 回起動し、JSON-RPC をまとめて投げる。

    hc は環境変数でソケットの場所を教えてくれないので、ソケットへ直接つながず
    公式の入口（stdin/stdout で API を話す connect）を使う。
    返り値は calls と同じ順の応答（取れなかったものは None）。
    """
    lines = "".join(
        json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n"
        for i, (method, params) in enumerate(calls)
    )
    try:
        proc = subprocess.run(
            [binary, "connect"],
            input=lines,
            capture_output=True,
            # Windows 版の exe も UTF-8 の JSON を返す。ロケール既定に任せると化ける
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"hc connect failed ({binary}): {exc}")
        return [None] * len(calls)
    by_id = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"hc bad response ({binary}): {line[:200]}")
            continue
        if isinstance(message, dict) and isinstance(message.get("id"), int):
            by_id[message["id"]] = message
    return [by_id.get(i) for i in range(len(calls))]


def _hc_result(response) -> dict | None:
    if not isinstance(response, dict):
        return None
    if response.get("error"):
        log(f"hc error: {response['error']}")
        return None
    result = response.get("result")
    return result if isinstance(result, dict) else None


def _hc_error_kind(response) -> str | None:
    error = (response or {}).get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return None
    data = error.get("data")
    return data.get("kind") if isinstance(data, dict) else None


def hc_find_tab(doc_value, tab_id: str) -> dict | None:
    """文書 `herdr-app` の中から tab_id のタブを探す。

    文書は hc から見れば opaque（アプリ内部の形式）なので、想定と違う形なら None を返して
    何もしない。アプリの更新で形が変わっても、壊すより諦めるほうを選ぶ。
    """
    if not isinstance(doc_value, dict):
        return None
    spaces = doc_value.get("spaces")
    if not isinstance(spaces, list):
        return None
    for space in spaces:
        tabs = space.get("tabs") if isinstance(space, dict) else None
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict) or tab.get("id") != tab_id:
                continue
            if isinstance(tab.get("name"), str):
                return tab
    return None


def ancestor_pids() -> frozenset:
    """自分の祖先プロセスの pid を集める（自分は含めない）。"""
    parents = {}
    proc = Path("/proc")
    if proc.is_dir():
        for stat in proc.glob("[0-9]*/stat"):
            try:
                # comm に空白や括弧が入りうるので、最後の ')' より後ろを読む
                fields = stat.read_text().rsplit(")", 1)[1].split()
                parents[int(stat.parent.name)] = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
    else:
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                parents[int(parts[0])] = int(parts[1])
    result = set()
    pid = os.getppid()
    while pid > 1 and pid not in result:
        result.add(pid)
        pid = parents.get(pid, 0)
    return frozenset(result)


class HerdrTab:
    """herdr TUI のタブ（Unix ソケット API）。"""

    def __init__(self, tab_id: str):
        self.tab_id = tab_id
        self.key = tab_id

    def label(self) -> str | None:
        return get_tab_label(self.tab_id)

    def rename(self, label: str) -> bool:
        return rename_tab(self.tab_id, label)


class HcTab:
    """herdr core（デスクトップアプリ）のタブ。名前は文書 `herdr-app` にある。"""

    def __init__(self, binary: str, tab_id: str):
        self.binary = binary
        self.tab_id = tab_id
        self.key = HC_KEY_PREFIX + tab_id

    def _get(self) -> tuple:
        result = _hc_result(hc_rpc(self.binary, [("doc.get", {"name": HC_DOC_NAME})])[0])
        if result is None:
            return None, None
        return result.get("value"), result.get("version")

    def label(self) -> str | None:
        value, _ = self._get()
        tab = hc_find_tab(value, self.tab_id)
        if tab is None:
            return None
        name = tab["name"]
        # アプリの自動命名は空と同じ扱いにして、タイトルで置き換えてよいことにする
        return "" if name.strip() in HC_AUTO_NAMES else name

    def rename(self, label: str) -> bool:
        """version を添えて書く（CAS）。アプリと同時に書いても相手の変更は消さない。"""
        for _ in range(HC_SET_RETRIES):
            value, version = self._get()
            tab = hc_find_tab(value, self.tab_id)
            if tab is None or not isinstance(version, int):
                log(f"hc tab {self.tab_id} not found in doc {HC_DOC_NAME!r}")
                return False
            tab["name"] = label
            params = {"name": HC_DOC_NAME, "value": value, "version": version}
            response = hc_rpc(self.binary, [("doc.set", params)])[0]
            if _hc_error_kind(response) == "conflict":
                log("hc doc.set conflict; retrying with the latest version")
                continue
            return _hc_result(response) is not None
        log(f"hc doc.set kept conflicting after {HC_SET_RETRIES} tries")
        return False


def resolve_hc_tab(terminal_id: str, ancestors: frozenset | None = None) -> HcTab | None:
    """HC_TERMINAL_ID からタブを特定する。

    ターミナル番号（t7 など）は hc ごとの連番なので、WSL で Linux 版と Windows 版の両方が
    あると別物の t7 を拾いうる。そこで次の順で確かめる。
      1. ターミナルの pid が自分の祖先にある hc（確実に自分のターミナル）
      2. 1 が無ければ、herdr.tab ラベルを持つターミナルを返した hc がちょうど 1 つのとき
         （Windows 版の pid は WSL から確かめられない）
    タブの文書は、ターミナルとは別の hc にありうる（Windows のアプリが WSL をマシンとして
    つないでいる場合など）ので、タブ ID が見つかる文書を候補から改めて探す。
    """
    if ancestors is None:
        ancestors = _ANCESTOR_PIDS
    candidates = hc_candidates()
    if not candidates:
        log("HC_TERMINAL_ID is set but herdr-core was not found")
        return None
    seen = []  # (binary, tab_id, pid が祖先にあるか, doc の value)
    for binary in candidates:
        terminal, doc = hc_rpc(
            binary,
            [("terminal.get", {"terminal": terminal_id}), ("doc.get", {"name": HC_DOC_NAME})],
        )
        terminal = _hc_result(terminal) or {}
        labels = terminal.get("labels")
        tab_id = labels.get(HC_TAB_LABEL) if isinstance(labels, dict) else None
        value = (_hc_result(doc) or {}).get("value")
        seen.append((binary, tab_id if isinstance(tab_id, str) else None,
                     terminal.get("pid") in ancestors, value))
    strong = [s for s in seen if s[1] and s[2]]
    weak = [s for s in seen if s[1]]
    if strong:
        tab_id = strong[0][1]
    elif len(weak) == 1:
        tab_id = weak[0][1]
    else:
        log(f"hc terminal {terminal_id}: {len(weak)} candidates matched; cannot pick one")
        return None
    for binary, _, _, value in seen:
        if hc_find_tab(value, tab_id) is not None:
            return HcTab(binary, tab_id)
    log(f"hc tab {tab_id} not found in any {HC_DOC_NAME!r} doc")
    return None


def resolve_tab():
    """このプロセスがいるタブを返す。herdr TUI を herdr core より優先する。

    herdr TUI を herdr core のターミナルの中で動かすと両方の環境変数が見えるが、
    名前を付けたいのは内側（herdr TUI）のタブなので。
    """
    terminal_id = os.environ.get("HC_TERMINAL_ID")
    # herdr core の中に古い HERDR_SOCKET_PATH だけ残っているときは herdr core を選ぶ
    if os.environ.get("HERDR_SOCKET_PATH") and (
        os.environ.get("HERDR_ENV") == "1" or not terminal_id
    ):
        tab_id = resolve_tab_id()
        return HerdrTab(tab_id) if tab_id else None
    if terminal_id:
        return resolve_hc_tab(terminal_id)
    return None


# ---------------------------------------------------------------- 会話ログ


# transcript には user ロールで書かれるが、ユーザーの意図ではない発言がある。
# これらを混ぜるとタイトルが「作業内容が不明」のようなメタ文になるので除く。
NOISE_PATTERNS = (
    # Esc 中断などで Claude Code 自身が差し込む擬似発言
    re.compile(r"^\[[^\]]*(interrupted|cancelled|canceled|no response)[^\]]*\]$", re.IGNORECASE),
    # skill / slash command の本文。ユーザーが書いた文章ではない
    re.compile(r"^Base directory for this skill\s*:", re.IGNORECASE),
)

# skill / slash command の本文は user ロールで数 KB〜十数 KB 差し込まれ、
# 前置き行を持たないものもある。人が打つプロンプトの長さではないので、
# 桁で切って落とす。落としすぎても、いま送信されたプロンプトは必ず残る。
MAX_PROMPT_CHARS = 2000


def is_noise_prompt(text: str) -> bool:
    """user ロールだが会話の意図を表さない発言か。"""
    if not text:
        return True
    # hook が挿入する system-reminder 等のタグ塊
    if text.startswith("<"):
        return True
    return any(pattern.match(text) for pattern in NOISE_PATTERNS)


def read_prompts(transcript_path: str | None, limit: int, current_prompt: str = "") -> str:
    """transcript から user 発言だけを拾って連結する。

    UserPromptSubmit の時点では、いま送信されたプロンプトが transcript に
    まだ書かれていないことがある（特にセッション 1 通目）。hook 入力の
    `prompt` を末尾に足して、最新の発言が必ずログに載るようにする。
    """
    current = (current_prompt or "").strip()
    if is_noise_prompt(current):
        current = ""
    if not transcript_path:
        return current[-limit:] if current else ""
    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # 履歴が読めなくても、いま送信されたプロンプトだけでタイトルは作れる
        log(f"cannot read transcript: {exc}")
        return current[-limit:] if current else ""
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
        # 現在のプロンプトと違い、履歴側は落としても最新の意図を失わない
        if is_noise_prompt(text) or len(text) > MAX_PROMPT_CHARS:
            continue
        prompts.append(text)
    # 最新の発言を必ず含める。transcript 側に既に載っていれば重複させない
    if current and (not prompts or prompts[-1] != current):
        prompts.append(current)
    joined = "\n".join(prompts)
    # 溢れるときは古い方を捨てる。直近の発言ほど今の作業を表す
    return joined[-limit:]


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


# 挨拶だけの会話などでは、モデルがタイトルではなく「作業内容が特定できません」
# という説明文を返す。これをそのまま採ると拒否文の断片がタブ名に焼き付き、
# REGEN_EVERY=0 の既定では二度と直らない。タイトルらしさを検証して弾く。
#
# 判定は「タイトルなら現れないもの」を見る:
#   - 文末の句点/ピリオド（システムプロンプトで禁止しているので、あれば地の文）
#   - 読点や「が/ので/ため」等でつながる説明調の接続
#   - 拒否・要求でよく出る定型語
REFUSAL_RE = re.compile(
    r"(申し訳|すみません|できません|できかね|特定でき|判断でき|不明です|"
    r"情報が必要|提供いただ|ご確認|お教え|いただけれ|ください|"
    r"I'm sorry|I am sorry|I apologize|cannot|can't|unable to|"
    r"does not (provide|contain|include)|doesn't (provide|contain|include)|"
    r"not enough|insufficient|no (specific|actual|clear) |"
    r"please provide|need more|would need|appears to be (only|just))",
    re.IGNORECASE,
)

# 説明文にだけ出る文末・接続の記号。タイトルは体言止め / noun phrase なので出ない。
SENTENCE_RE = re.compile(r"[。．、，]|(?<=[a-zA-Z])\.(?:\s|$)")


def looks_like_prose(title: str) -> bool:
    """タイトルではなく地の文（拒否・説明）に見えるか。"""
    if REFUSAL_RE.search(title):
        return True
    return bool(SENTENCE_RE.search(title))


def sanitize_title(raw: str, max_width: int | None = None) -> str:
    """モデル出力を 1 行のタブ名として使える形に整える。

    タイトルとして使えないと判断したときは空を返す。呼び出し側はそれを
    「生成できなかった」として扱い、既存のタイトルを維持する。
    """
    text = "".join(ch for ch in raw if ch == "\n" or unicodedata.category(ch)[0] != "C")
    # 空行を除いた最後の行を採る（思考めいた前置きが混ざった場合の保険）
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # 拒否文は複数行にわたることがある。1 行でも地の文が混じれば全体を捨てる
    # （最終行だけ見ると「ご確認のうえ…」のような断片を拾ってしまう）
    if any(looks_like_prose(line) for line in lines):
        log(f"model returned prose, not a title: {lines[0][:80]!r}")
        return ""
    title = PREFIX_RE.sub("", lines[-1]).strip(TRIM_CHARS)
    title = re.sub(r"\s+", " ", title)
    if not title:
        return ""
    # 切り詰める前に長さを見る。タイトルなら収まるはずで、大きく溢れるなら地の文
    budget = MAX_WIDTH if max_width is None else max_width
    limit = max(budget, 1) * 2
    if display_width(title) > limit:
        log(f"model output too long for a title ({display_width(title)} > {limit})")
        return ""
    return truncate(title, budget)


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
        "HC_TERMINAL_ID",
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


# 挨拶・相槌だけの発言。これしか無い時点ではタイトルを付けようがない。
GREETING_RE = re.compile(
    r"^(おはよう(ございます)?|こんにちは|こんばんは|はじめまして|やあ|よろしく(お願いします)?|"
    r"ありがとう(ございます)?|お疲れ(様|さま)?(です)?|ちょっといい\?|ping|test|"
    r"hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you|yo)"
    r"[\s!！。．、,\.~〜ー]*$",
    re.IGNORECASE,
)


def is_greeting_only(conversation_log: str) -> bool:
    """会話が挨拶・相槌だけで、まだ作業の手がかりが無いか。

    ここで打ち切らないと、モデルが「作業内容が不明です」という地の文を返し、
    それを弾いた結果としてタイトルが付かない…という無駄な往復が毎回起きる。
    さらに空振りでも中身の薄いタイトルが付くと REGEN_EVERY=0 で固定される。
    """
    lines = [line.strip() for line in conversation_log.splitlines() if line.strip()]
    if not lines:
        return True
    return all(GREETING_RE.match(line) for line in lines)


def generate_title(conversation_log: str, agent: str, max_width: int) -> str:
    """claude -p を最小構成で呼び、タイトル 1 行を得る。

    --safe-mode が要点。CLAUDE.md / skills / plugins / hooks / MCP を読まないので
    起動が速く、かつこの呼び出しが自分自身の hook を再入させない。
    """
    if not conversation_log.strip():
        return ""
    # 挨拶だけなら LLM を呼ばずに諦める。呼んでも拒否文が返るだけで、
    # 課金と待ち時間が無駄になる
    if is_greeting_only(conversation_log):
        log("conversation is greetings only; no title yet")
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


# ---------------------------------------------------------------- 話題の変化

# 助詞・助動詞など、話題を表さない 1〜2 文字語。bigram に混ざると
# 無関係な文どうしが「似ている」と誤判定される
STOP_BIGRAMS = frozenset(
    {
        "まわ", "わり", "した", "って", "てい", "でき", "ます", "です",
        "ある", "いる", "する", "なる", "こと", "もの", "ため", "よう",
    }
)

ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
CJK_RUN_RE = re.compile(r"[ぁ-んァ-ヶ一-龥ー]{2,}")

# 語の重なりがこれ未満なら「話題が変わった」とみなす
TOPIC_SIMILARITY_THRESHOLD = 0.20

# 「話題が変わった」と言い切るのに要る最低限の中身の量（文字数）。
# 「ありがとう」「これも直して」のような相づちは、いまの話題と語が重ならず
# 重なり 0 になる。ここを通すと話題が続いているのに毎回生成してしまい、
# B-2 の目的（無駄な生成を減らす）と逆になる。
#
# 語数ではなく文字数で測る。日本語は bigram に落とすため、短い相づちでも
# 語数だけは増えてしまい（「ありがとう」→ 4 語）判断材料にならない。
MIN_TOPIC_CHARS = 8


def tokenize_for_topic(text: str | None) -> set:
    """話題の比較に使う語を切り出す。

    形態素解析器は使えない（標準ライブラリのみ）ので、日本語は文字 bigram に
    落とす。厳密な語ではないが、「同じ話題か」を測るには十分な粒度になる。
    """
    if not text:
        return set()
    tokens = set()
    # 英数字は語として拾う。1 文字は話題を表さないので落とす
    for word in ASCII_WORD_RE.findall(text):
        tokens.add(word.lower())
    # 日本語は bigram に落とす
    for run in CJK_RUN_RE.findall(text):
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in STOP_BIGRAMS:
                tokens.add(bigram)
    return tokens


def topic_content(text: str | None) -> str:
    """話題を表す部分だけを残す。記号や空白は量として数えない。"""
    if not text:
        return ""
    return "".join(ASCII_WORD_RE.findall(text)) + "".join(CJK_RUN_RE.findall(text))


def topic_changed(
    recent_prompt: str | None,
    current_title: str | None,
    threshold: float = TOPIC_SIMILARITY_THRESHOLD,
) -> bool:
    """直近のプロンプトが、いまのタイトルと別の話題か。

    **判定に LLM を使わない。** 判定自体が有料になっては本末転倒なので、
    語の重なりだけで決める。

    - タイトルが無い → 作るしかないので True
    - プロンプトが空 → 判断材料が無い。無駄な生成を避けて False
    """
    if not current_title or not current_title.strip():
        return True
    # 短すぎるプロンプトは、話題変化の根拠として弱い。
    # 相づちや指示語で作り直すと無駄な LLM 呼び出しになる
    if len(topic_content(recent_prompt)) < MIN_TOPIC_CHARS:
        return False
    prompt_tokens = tokenize_for_topic(recent_prompt)
    if not prompt_tokens:
        return False
    title_tokens = tokenize_for_topic(current_title)
    if not title_tokens:
        return True
    overlap = prompt_tokens & title_tokens
    # タイトルは短いので、タイトル側の語がどれだけ拾われたかで測る
    similarity = len(overlap) / len(title_tokens)
    return similarity < threshold


def should_regenerate(
    generated_at: int,
    prompt_count: int,
    regen_every: int,
    recent_prompt: str | None,
    current_title: str | None,
) -> bool:
    """いまタイトルを作り直すか。

    - 初回（まだ一度も生成していない）: 必ず作る
    - `REGEN_EVERY` 明示: 従来どおり N 回ごと。話題は見ない（後方互換）
    - 既定（`REGEN_EVERY=0`）: 話題が変わった時だけ作る
    """
    if generated_at == 0:
        return True
    if regen_every > 0:
        return prompt_count - generated_at >= regen_every
    return topic_changed(recent_prompt, current_title)


# ---------------------------------------------------------------- 状態


def _safe_stem(raw: str | None) -> str:
    """任意の識別子をファイル名として安全な語に落とす。

    `..` や `/` を含む値でもディレクトリの外へ出られないようにする。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw or "")
    # 連続するドットは親ディレクトリ参照になりうるので 1 つに潰す
    safe = re.sub(r"\.{2,}", ".", safe)
    # ドットや区切り文字だけの名前も親ディレクトリを指してしまう
    if not safe.strip("._-"):
        return "unknown"
    return safe


def state_path(session_id: str | None) -> Path:
    return STATE_DIR / f"{_safe_stem(session_id)}.json"


TAB_STATE_SUBDIR = "tabs"


def tab_state_dir() -> Path:
    """タブ所有権レコードの置き場所。

    STATE_DIR から呼び出し時に組み立てる。定数に畳むとテストが
    STATE_DIR を差し替えても実 home に書き込んでしまう。
    """
    return STATE_DIR / TAB_STATE_SUBDIR


def tab_state_path(tab_id: str | None) -> Path:
    """タブ単位の所有権レコードのパス。

    session_id 単位で持つと、新しいセッションを起動した時点で
    「前に自分が付けた名前」を手動ラベルと誤認し、二度と更新できなくなる。
    タブに紐づけて持てば、セッションを跨いでも自分の名前だと分かる。
    """
    return tab_state_dir() / f"{_safe_stem(tab_id)}.json"


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


MAX_TAB_STATES = _env_int("MAX_TAB_STATES", 64)


def gc_tab_states(directory: Path, keep: int | None = None) -> None:
    """古いタブ所有権レコードを間引く。

    タブは開くたびに増えるので、放っておくとレコードが際限なく溜まる。
    更新が新しいものを keep 件だけ残す。消しても実害はない（そのタブの
    タイトルが一度だけ手動ラベル扱いになるだけ）ので、失敗しても黙って続ける。
    """
    # 既定値を引数に畳むと、後から MAX_TAB_STATES を変えても効かない
    if keep is None:
        keep = MAX_TAB_STATES
    if keep < 0:
        return
    try:
        records = list(directory.glob("*.json"))
    except OSError as exc:
        log(f"cannot list tab states: {exc}")
        return
    if len(records) <= keep:
        return
    try:
        # 更新が新しい順に並べ、あふれた分を落とす。
        # st_mtime（秒）だと同時刻の書き込みが並び順で決まってしまうので ns で見る
        records.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    except OSError as exc:
        log(f"cannot stat tab states: {exc}")
        return
    for stale in records[keep:]:
        try:
            stale.unlink()
        except OSError as exc:
            log(f"cannot remove {stale.name}: {exc}")


def save_tab_state(path: Path, state: dict) -> None:
    """タブ所有権レコードを保存する。置き場所（tabs/）は無ければ作る。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"cannot create tab state dir: {exc}")
        return
    save_state(path, state)


AUTO_LABEL_RE = re.compile(r"^\d+$")


def is_disposable_title(stripped_label: str | None) -> bool:
    """このタイトル部分に保持する価値がないか。

    空、または herdr が付ける既定ラベル（タブ番号の連番）が対象。
    捨てずに持ち回ると、タイトルを生成しない軽量モードで `🟦 5` のような残骸が残る。
    """
    if not stripped_label or not stripped_label.strip():
        return True
    return bool(AUTO_LABEL_RE.match(stripped_label.strip()))


def owned_title(tab_state: dict | None) -> str | None:
    """タブ所有権レコードから「この hook が付けた名前」を取り出す。

    state が壊れていても落とさない。読めなければ所有権なしとして扱う。
    """
    if not isinstance(tab_state, dict):
        return None
    title = tab_state.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    return title


def may_overwrite_title(stripped_label: str | None, previous_title: str | None) -> bool:
    """このタイトル部分を上書きしてよいか判定する。

    必ず strip_legacy_marker を通した文字列を渡すこと。0.1.x の絵文字が付いたままだと
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
    state["prompt_count"] = prompt_count
    save_state(path, state)

    tab = resolve_tab()
    if tab is None:
        log("cannot resolve tab id; giving up")
        return
    # 状態ファイルのキー（herdr core のタブには hc: が付く）
    tab_id = tab.key

    # 所有権はタブに紐づく。セッションを跨いでも自分の名前だと分かるようにする
    tab_path = tab_state_path(tab_id)
    tab_state = load_state(tab_path)
    previous_title = owned_title(tab_state)
    # 「いつ生成したか」はタイトルと同じ置き場所に持つ。session 側に残すと
    # タイトルだけ復元できて generated_at が 0 に戻り、新セッションの初回で
    # 話題が続いていても必ず生成してしまう
    generated_at = int(tab_state.get("generated_at_prompt", 0) or 0)

    log(f"tab={tab_id} prompt={prompt_count}")

    current_label = tab.label()
    # 0.1.x は先頭にリポジトリの絵文字を付けていた。そのとき付けた絵文字だけは剥がしてから比べる
    # （剥がさないと、自分が付けた名前を手動ラベルと誤認して二度と更新できなくなる）
    current_title = strip_legacy_marker(current_label, tab_state.get("marker") or state.get("marker"))

    # タイトルを作り直すか判断する。
    # 既定は話題が変わった時だけ。REGEN_EVERY 明示時は従来の N 回ごと
    current_prompt = hook_input.get("prompt") or ""
    due = should_regenerate(
        generated_at,
        prompt_count,
        REGEN_EVERY,
        current_prompt,
        "" if is_disposable_title(current_title) else current_title,
    )
    if not TITLE_ENABLED:
        log("title generation disabled; nothing to do")
        return
    if not due:
        log(f"title not due (generated_at={generated_at}, regen_every={REGEN_EVERY})")
        return
    # 手動ラベル（人やオーケストレーターが付けた名前）には一切触らない。切り詰めもしない
    if not may_overwrite_title(current_title, previous_title):
        log(f"title {current_title!r} looks manual; leaving it alone")
        return
    conversation_log = read_prompts(
        hook_input.get("transcript_path"),
        MAX_LOG_CHARS,
        hook_input.get("prompt") or "",
    )
    generated = generate_title(conversation_log, detect_agent(hook_input), MAX_WIDTH)
    if not generated:
        log("title generation produced nothing; keeping current title")
        return
    title = truncate(generated, MAX_WIDTH)
    tab_state["generated_at_prompt"] = prompt_count
    new_label = title

    if new_label == current_label:
        log(f"label already {new_label!r}; no rename needed")
        return
    if tab.rename(new_label):
        state["tab_id"] = tab_id
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save_state(path, state)
        # 所有権レコードはタブ側に残す。次のセッションがこれを見て
        # 「自分が付けた名前」だと判断できる
        tab_state["title"] = title
        # 絵文字はもう付けないので、0.1.x の記録は消しておく
        tab_state.pop("marker", None)
        tab_state["updated_at"] = state["updated_at"]
        save_tab_state(tab_path, tab_state)
        # 保存した後に間引く。いま書いたレコードが最新なので必ず残る
        gc_tab_states(tab_state_dir())
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
    # herdr の外では何もしない。
    # herdr TUI は HERDR_ENV / HERDR_SOCKET_PATH を、herdr core（デスクトップアプリ）は
    # HC_TERMINAL_ID だけを渡す
    in_herdr = os.environ.get("HERDR_ENV") == "1"
    in_hc = bool(os.environ.get("HC_TERMINAL_ID"))
    if in_herdr and not os.environ.get("HERDR_SOCKET_PATH"):
        log("HERDR_SOCKET_PATH is not set; giving up")
        return 0
    if not in_herdr and not in_hc:
        return 0
    if not TITLE_ENABLED:
        return 0
    if not in_herdr:
        global _ANCESTOR_PIDS
        _ANCESTOR_PIDS = ancestor_pids()

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
