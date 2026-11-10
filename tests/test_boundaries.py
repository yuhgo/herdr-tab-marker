"""境界層のテスト。

ソケット / サブプロセス / 環境変数の扱いを、実際の herdr や LLM を呼ばずに確かめる。
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import herdr_tab_marker as htm  # noqa: E402


class FakeHerdr:
    """1 リクエストだけ応答する herdr ソケットのスタブ。"""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "herdr.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            self.requests.append(json.loads(buf.split(b"\n", 1)[0].decode()))
            conn.sendall((json.dumps(self.response) + "\n").encode())

    def close(self):
        self._server.close()


class TestHerdrCall(unittest.TestCase):
    def test_returns_none_without_socket_path(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(htm.herdr_call("tab.get", {"tab_id": "w1:t1"}))

    def test_returns_none_for_missing_socket(self):
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/nonexistent/x.sock"}):
            self.assertIsNone(htm.herdr_call("tab.get", {"tab_id": "w1:t1"}))

    def test_round_trip(self):
        fake = FakeHerdr({"id": "x", "result": {"tab": {"label": "ssh"}}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                response = htm.herdr_call("tab.get", {"tab_id": "w1:t4"})
            self.assertEqual(response["result"]["tab"]["label"], "ssh")
        finally:
            fake.close()

    def test_request_id_is_a_string(self):
        """herdr は数値 id を invalid_request で弾く。"""
        fake = FakeHerdr({"id": "x", "result": {}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                htm.herdr_call("tab.get", {"tab_id": "w1:t4"})
            self.assertIsInstance(fake.requests[0]["id"], str)
        finally:
            fake.close()


class TestGetTabLabel(unittest.TestCase):
    def test_reads_label(self):
        fake = FakeHerdr({"result": {"tab": {"label": "ssh"}}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                self.assertEqual(htm.get_tab_label("w1:t4"), "ssh")
        finally:
            fake.close()

    def test_missing_label_is_none(self):
        fake = FakeHerdr({"result": {"tab": {}}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                self.assertIsNone(htm.get_tab_label("w1:t4"))
        finally:
            fake.close()


class TestRenameTab(unittest.TestCase):
    def test_success(self):
        fake = FakeHerdr({"result": {"tab": {"label": "🟦 x"}}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                self.assertTrue(htm.rename_tab("w1:t4", "🟦 x"))
            self.assertEqual(fake.requests[0]["method"], "tab.rename")
            self.assertEqual(fake.requests[0]["params"]["label"], "🟦 x")
        finally:
            fake.close()

    def test_error_response_is_false(self):
        fake = FakeHerdr({"error": {"message": "no such tab"}})
        try:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": fake.path}):
                self.assertFalse(htm.rename_tab("w1:t99", "x"))
        finally:
            fake.close()

    def test_unreachable_socket_is_false(self):
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/nonexistent/x.sock"}):
            self.assertFalse(htm.rename_tab("w1:t4", "x"))


class TestResolveTabId(unittest.TestCase):
    def test_prefers_pane_get(self):
        fake = FakeHerdr({"result": {"pane": {"tab_id": "w1:t7"}}})
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HERDR_SOCKET_PATH": fake.path,
                    "HERDR_PANE_ID": "w1:p6",
                    "HERDR_TAB_ID": "w1:t1",  # 古い値
                },
            ):
                self.assertEqual(htm.resolve_tab_id(), "w1:t7")
        finally:
            fake.close()

    def test_falls_back_to_env(self):
        """古い herdr には HERDR_TAB_ID が無い場合がある。その逆で、ソケットが死んでいるケース。"""
        with mock.patch.dict(
            os.environ,
            {"HERDR_SOCKET_PATH": "/nonexistent/x.sock", "HERDR_TAB_ID": "w1:t4"},
            clear=True,
        ):
            self.assertEqual(htm.resolve_tab_id(), "w1:t4")

    def test_none_when_nothing_available(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(htm.resolve_tab_id())


class TestChildEnv(unittest.TestCase):
    def test_strips_herdr_vars(self):
        """これを忘れると生成用の agent が自分の hook を呼び返す。"""
        dirty = {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p6",
            "HERDR_TAB_ID": "w1:t4",
            "HERDR_WORKSPACE_ID": "w1",
            "HERDR_SOCKET_PATH": "/x.sock",
            "CLAUDECODE": "1",
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(os.environ, dirty, clear=True):
            env = htm.child_env()
        for key in (
            "HERDR_ENV",
            "HERDR_PANE_ID",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
            "HERDR_SOCKET_PATH",
            "CLAUDECODE",
        ):
            self.assertNotIn(key, env, f"{key} が子プロセスに漏れている")
        self.assertEqual(env["PATH"], "/usr/bin")


class TestGenerateTitle(unittest.TestCase):
    def test_empty_log_skips_subprocess(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(htm.generate_title("   ", htm.CLAUDE, 25), "")
            run.assert_not_called()

    def test_missing_claude_returns_empty(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertEqual(htm.generate_title("log", htm.CLAUDE, 25), "")

    def test_nonzero_exit_returns_empty(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(htm.generate_title("log", htm.CLAUDE, 25), "")

    def test_uses_safe_mode_and_no_tools(self):
        """--safe-mode が再入防止の要。hooks / plugins / MCP を読ませない。"""
        completed = mock.Mock(returncode=0, stdout="認証調査\n", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            title = htm.generate_title("log", htm.CLAUDE, 25)
        self.assertEqual(title, "認証調査")
        command = run.call_args[0][0]
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--max-budget-usd", command)
        self.assertEqual(command[command.index("--tools") + 1], "")

    def test_respects_budget_width(self):
        completed = mock.Mock(returncode=0, stdout="とても長い作業内容のタイトルです\n", stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            title = htm.generate_title("log", htm.CLAUDE, 10)
        self.assertLessEqual(htm.display_width(title), 10)


class TestDetectAgent(unittest.TestCase):
    def test_codex_has_turn_id(self):
        self.assertEqual(htm.detect_agent({"turn_id": "t1"}), htm.CODEX)

    def test_claude_has_no_turn_id(self):
        self.assertEqual(htm.detect_agent({"session_id": "s1"}), htm.CLAUDE)


class TestState(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            htm.save_state(path, {"title": "認証調査", "prompt_count": 3})
            self.assertEqual(htm.load_state(path)["title"], "認証調査")

    def test_missing_file_is_empty_dict(self):
        self.assertEqual(htm.load_state(Path("/nonexistent/x.json")), {})

    def test_broken_json_is_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(htm.load_state(path), {})

    def test_non_dict_json_is_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("[1,2]", encoding="utf-8")
            self.assertEqual(htm.load_state(path), {})


class TestReadPrompts(unittest.TestCase):
    def _write(self, tmp, lines):
        path = Path(tmp) / "t.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return str(path)

    def test_missing_path_is_empty(self):
        self.assertEqual(htm.read_prompts(None, 1500), "")
        self.assertEqual(htm.read_prompts("/nonexistent/t.jsonl", 1500), "")

    def test_extracts_user_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "認証を調べて"}},
                {"type": "assistant", "message": {"content": "はい"}},
                {"type": "user", "message": {"content": "続き"}},
            ])
            out = htm.read_prompts(path, 1500)
        self.assertIn("認証を調べて", out)
        self.assertIn("続き", out)
        self.assertNotIn("はい", out)

    def test_handles_content_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": [
                    {"type": "text", "text": "ブロック形式"},
                    {"type": "image"},
                ]}},
            ])
            self.assertIn("ブロック形式", htm.read_prompts(path, 1500))

    def test_skips_injected_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            reminder = "<system-reminder>noise</system-reminder>"
            path = self._write(tmp, [
                {"type": "user", "message": {"content": reminder}},
                {"type": "user", "message": {"content": "本題"}},
            ])
            out = htm.read_prompts(path, 1500)
        self.assertNotIn("noise", out)
        self.assertIn("本題", out)

    def test_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "あ" * 500}},
            ])
            self.assertEqual(len(htm.read_prompts(path, 100)), 100)

    def test_ignores_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                '{not json\n{"type":"user","message":{"content":"ok"}}\n',
                encoding="utf-8",
            )
            self.assertIn("ok", htm.read_prompts(str(path), 1500))

    def test_skips_interrupt_markers(self):
        """中断通知だけのログは空扱い。生成させるとメタ文がタイトルになる。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "[Request interrupted by user]"}},
                {"type": "user", "message": {
                    "content": "[Request interrupted by user for tool use]"}},
            ])
            self.assertEqual(htm.read_prompts(path, 1500), "")

    def test_skips_skill_bodies(self):
        """skill / slash command の本文はユーザーの意図ではない。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {
                    "content": "Base directory for this skill: /x/y\n\n# Harness Plan\n手順"}},
                {"type": "user", "message": {"content": "巨大" * 3000}},
                {"type": "user", "message": {"content": "本題"}},
            ])
            out = htm.read_prompts(path, 1500)
        self.assertEqual(out, "本題")

    def test_appends_current_prompt_when_transcript_lacks_it(self):
        """UserPromptSubmit 時点では最新プロンプトが transcript に無いことがある。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "前の発言"}},
            ])
            out = htm.read_prompts(path, 1500, "サンプルのTODOスクリプトを作って")
        self.assertIn("前の発言", out)
        self.assertTrue(out.endswith("サンプルのTODOスクリプトを作って"))

    def test_does_not_duplicate_current_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "作って"}},
            ])
            self.assertEqual(htm.read_prompts(path, 1500, "作って"), "作って")

    def test_current_prompt_survives_unreadable_transcript(self):
        self.assertEqual(htm.read_prompts(None, 1500, "作って"), "作って")
        self.assertEqual(htm.read_prompts("/nonexistent/t.jsonl", 1500, "作って"), "作って")

    def test_current_prompt_noise_is_dropped(self):
        self.assertEqual(htm.read_prompts(None, 1500, "[Request interrupted by user]"), "")

    def test_keeps_newest_prompts_when_truncating(self):
        """溢れたら古い方を捨てる。直近ほど今の作業を表す。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"type": "user", "message": {"content": "古い" * 400}},
                {"type": "user", "message": {"content": "最新の依頼"}},
            ])
            out = htm.read_prompts(path, 100)
        self.assertEqual(len(out), 100)
        self.assertTrue(out.endswith("最新の依頼"))


class TestMainGuards(unittest.TestCase):
    def _main_with(self, env, stdin='{"session_id":"s"}'):
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin.read", return_value=stdin), \
             mock.patch.object(htm, "run") as run, \
             mock.patch.object(htm, "daemonize") as daemonize:
            code = htm.main()
        return code, run, daemonize

    def test_exits_outside_herdr(self):
        """HERDR_ENV が無ければ即 exit 0。herdr の外では何もしない。"""
        code, run, _ = self._main_with({})
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_exits_without_socket_path(self):
        code, run, _ = self._main_with({"HERDR_ENV": "1"})
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_runs_inside_herdr(self):
        code, run, daemonize = self._main_with(
            {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock"}
        )
        self.assertEqual(code, 0)
        run.assert_called_once()
        daemonize.assert_called_once()

    def test_foreground_skips_daemonize(self):
        env = {
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": "/x.sock",
            "HERDR_TAB_MARKER_FOREGROUND": "1",
        }
        # FOREGROUND はモジュール読み込み時に確定するのでパッチする
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("sys.stdin.read", return_value="{}"), \
             mock.patch.object(htm, "run") as run, \
             mock.patch.object(htm, "daemonize") as daemonize, \
             mock.patch.object(htm, "FOREGROUND", True):
            htm.main()
        run.assert_called_once()
        daemonize.assert_not_called()

    def test_disable_flag(self):
        with mock.patch.dict(
            os.environ,
            {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock",
             "HERDR_TAB_MARKER_DISABLE": "1"},
            clear=True,
        ), mock.patch("sys.stdin.read", return_value="{}"), \
             mock.patch.object(htm, "run") as run:
            self.assertEqual(htm.main(), 0)
        run.assert_not_called()

    def test_broken_stdin_does_not_crash(self):
        code, run, _ = self._main_with(
            {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock"}, stdin="{not json"
        )
        self.assertEqual(code, 0)
        run.assert_called_once_with({})


class TestTitleDisabledMode(unittest.TestCase):
    """HERDR_TAB_MARKER_TITLE=0 は LLM を一切呼ばない。"""

    def test_no_subprocess_when_title_disabled(self):
        fake = FakeHerdr({"result": {"tab": {"label": "4"}}})
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HERDR_SOCKET_PATH": fake.path,
                    "HERDR_TAB_ID": "w1:t4",
                    "HOME": tempfile.mkdtemp(),
                },
                clear=True,
            ), mock.patch.object(htm, "TITLE_ENABLED", False), \
                 mock.patch.object(htm, "STATE_DIR", Path(tempfile.mkdtemp())), \
                 mock.patch.object(htm, "generate_title") as generate:
                htm.run({"session_id": "s1", "cwd": os.getcwd()})
            generate.assert_not_called()
        finally:
            fake.close()


class TestManualLabelIntegration(unittest.TestCase):
    """手動ラベル保護を run() 経由で一巡させる統合テスト。

    「手動ラベルには一切触らない」が本実装の中核なので、
    純粋関数のテストだけでなく通しでも固定する。
    """

    def _run(self, current_label, title_enabled=True):
        fake = FakeHerdr({"result": {"tab": {"label": current_label}}})
        state_dir = Path(tempfile.mkdtemp())
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HERDR_SOCKET_PATH": fake.path,
                    "HERDR_TAB_ID": "w1:t4",
                    "HOME": tempfile.mkdtemp(),
                },
                clear=True,
            ), mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", title_enabled), \
                 mock.patch.object(htm, "generate_title") as generate, \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                generate.return_value = "生成されたタイトル"
                htm.run({"session_id": "s1", "cwd": os.getcwd()})
            return generate, rename
        finally:
            fake.close()

    def test_manual_label_is_left_untouched(self):
        generate, rename = self._run("ssh")
        # 手動ラベルなので LLM は呼ばない（コストゼロ）
        generate.assert_not_called()
        # 名前も変えない
        rename.assert_not_called()

    def test_long_manual_label_is_not_truncated(self):
        # オーケストレーターが付けた長い名前を MAX_WIDTH で切り詰めない
        label = "#1454 [grill] 役割ごとのモデル・effort・api-version を型付きレジストリで持つ"
        generate, rename = self._run(label)
        generate.assert_not_called()
        rename.assert_not_called()

    def test_default_numeric_label_is_replaced_by_title(self):
        generate, rename = self._run("4")
        generate.assert_called_once()
        # 絵文字を付けず、タイトルだけにする
        self.assertEqual(rename.call_args[0][1], "生成されたタイトル")


class TestCrossSessionOwnership(unittest.TestCase):
    """A の受け入れ条件: 別 session_id でも自分の付けたタイトルは上書きできる。

    かつては session_id 単位の state しか無く、新セッションでは
    previous_title が None になるため may_overwrite_title が必ず False を返し、
    タブ名が二度と更新されなかった。
    """

    def _run(self, state_dir, home, session_id, current_label, generated, prompt=""):
        fake = FakeHerdr({"result": {"tab": {"label": current_label}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": home,
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", True), \
                 mock.patch.object(htm, "read_prompts", return_value="ログ"), \
                 mock.patch.object(htm, "generate_title", return_value=generated) as gen, \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                htm.run({
                    "session_id": session_id,
                    "cwd": os.getcwd(),
                    "prompt": prompt,
                })
            return gen, rename
        finally:
            fake.close()

    def test_second_session_can_update_its_own_title(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        # 1 回目: セッション s1 が「認証調査」を付ける
        _, rename1 = self._run(
            state_dir, home, "s1", "4", "認証調査", prompt="認証まわりを調べたい"
        )
        label1 = rename1.call_args[0][1]
        self.assertTrue(label1.endswith("認証調査"), label1)

        # 2 回目: 別セッション s2 が別の話題で来る。
        # タブには前回のラベルが残っているが、自分が付けた名前なので上書きできる
        gen2, rename2 = self._run(
            state_dir, home, "s2", label1, "課金修正",
            prompt="課金の請求書PDFの出力を直したい",
        )
        gen2.assert_called_once()  # 手動ラベル扱いされず生成まで進む
        label2 = rename2.call_args[0][1]
        self.assertTrue(label2.endswith("課金修正"), label2)

    def test_manual_label_survives_a_new_session(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        # 1 回目でタブ所有権を作る
        self._run(state_dir, home, "s1", "4", "認証調査")

        # 2 回目: 人が手で `ssh` に付け替えた状態
        gen2, rename2 = self._run(state_dir, home, "s2", "ssh", "課金修正")
        gen2.assert_not_called()  # 手動ラベルなので LLM を呼ばない
        rename2.assert_not_called()

    def test_legacy_marked_own_title_can_be_updated(self):
        # 0.1.x が絵文字つきで付けた自分の名前は、記録した絵文字を剥がして自分のものと分かる
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        tabs = state_dir / "tabs"
        tabs.mkdir(parents=True)
        (tabs / "w1_t4.json").write_text(
            '{"title": "認証調査", "marker": "🐳", "generated_at_prompt": 1}', encoding="utf-8"
        )
        gen, rename = self._run(
            state_dir, home, "s2", "🐳 認証調査", "課金修正",
            prompt="課金の請求書PDFの出力を直したい",
        )
        gen.assert_called_once()
        self.assertEqual(rename.call_args[0][1], "課金修正")
        record = htm.load_state(tabs / "w1_t4.json")
        self.assertNotIn("marker", record)

    def test_ownership_record_lives_under_tabs_dir(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        self._run(state_dir, home, "s1", "4", "認証調査")
        record = htm.load_state(state_dir / "tabs" / "w1_t4.json")
        self.assertEqual(record.get("title"), "認証調査")


class TestTabStateGcIsWired(unittest.TestCase):
    """GC は run() から実際に呼ばれること。関数があるだけでは溜まり続ける。"""

    def test_run_prunes_old_tab_records(self):
        state_dir = Path(tempfile.mkdtemp())
        tabs = state_dir / "tabs"
        tabs.mkdir(parents=True)
        # 上限を超える残骸を先に置く
        for i in range(8):
            stale = tabs / f"w9_t{i}.json"
            stale.write_text('{"title": "古い"}', encoding="utf-8")
            os.utime(stale, (1_700_000_000 + i, 1_700_000_000 + i))

        fake = FakeHerdr({"result": {"tab": {"label": "4"}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": tempfile.mkdtemp(),
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "MAX_TAB_STATES", 3), \
                 mock.patch.object(htm, "TITLE_ENABLED", True), \
                 mock.patch.object(htm, "read_prompts", return_value="ログ"), \
                 mock.patch.object(htm, "generate_title", return_value="認証調査"), \
                 mock.patch.object(htm, "rename_tab", return_value=True):
                htm.run({"session_id": "s1", "cwd": os.getcwd()})
        finally:
            fake.close()

        remaining = sorted(p.name for p in tabs.glob("*.json"))
        self.assertEqual(len(remaining), 3, remaining)
        # いま書いたレコードは最新なので必ず残る
        self.assertIn("w1_t4.json", remaining)


class TestTopicRegenerationIsWired(unittest.TestCase):
    """B-2 の受け入れ条件を run() 経由で確かめる。

    同話題なら LLM を呼ばず、話題が変われば呼ぶ。
    """

    def _run(self, state_dir, home, session_id, current_label, prompt, regen_every=0):
        fake = FakeHerdr({"result": {"tab": {"label": current_label}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": home,
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", True), \
                 mock.patch.object(htm, "REGEN_EVERY", regen_every), \
                 mock.patch.object(htm, "read_prompts", return_value="ログ"), \
                 mock.patch.object(htm, "generate_title", return_value="課金請求書修正") as gen, \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                htm.run({
                    "session_id": session_id,
                    "cwd": os.getcwd(),
                    "prompt": prompt,
                })
            return gen, rename
        finally:
            fake.close()

    def _seed(self, state_dir, home):
        """既に「認証まわり調査」が付いている状態を作る。"""
        fake = FakeHerdr({"result": {"tab": {"label": "4"}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": home,
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", True), \
                 mock.patch.object(htm, "read_prompts", return_value="ログ"), \
                 mock.patch.object(htm, "generate_title", return_value="認証まわり調査"), \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                htm.run({"session_id": "s1", "cwd": os.getcwd(), "prompt": "認証を調べたい"})
            return rename.call_args[0][1]
        finally:
            fake.close()

    def test_same_topic_does_not_call_llm(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        label = self._seed(state_dir, home)
        gen, _ = self._run(
            state_dir, home, "s1", label, "認証まわりのテストを追加したい"
        )
        gen.assert_not_called()

    def test_new_topic_calls_llm(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        label = self._seed(state_dir, home)
        gen, rename = self._run(
            state_dir, home, "s1", label, "課金の請求書PDFを直したい"
        )
        gen.assert_called_once()
        self.assertTrue(rename.call_args[0][1].endswith("課金請求書修正"))

    def test_explicit_regen_every_ignores_topic(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        label = self._seed(state_dir, home)
        # REGEN_EVERY=5 なら話題が変わっても間隔を守る（後方互換）
        gen, _ = self._run(
            state_dir, home, "s1", label, "課金の請求書PDFを直したい", regen_every=5
        )
        gen.assert_not_called()


class TestDaemonizeFailure(unittest.TestCase):
    def test_fork_failure_falls_back_to_foreground(self):
        """fork に失敗しても hook 全体を落とさない。

        daemonize は「hook をブロックしないため」の仕組みなので、
        それ自体の失敗で prompt が止まっては本末転倒。
        """
        with mock.patch.dict(
            os.environ,
            {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock"},
            clear=True,
        ), mock.patch("sys.stdin.read", return_value="{}"), \
             mock.patch("os.fork", side_effect=OSError("cannot fork")), \
             mock.patch.object(htm, "run") as run:
            self.assertEqual(htm.main(), 0)
        # fork できなくても処理自体は前景で走る
        run.assert_called_once()

    def test_unexpected_error_in_run_is_swallowed(self):
        with mock.patch.dict(
            os.environ,
            {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock"},
            clear=True,
        ), mock.patch("sys.stdin.read", return_value="{}"), \
             mock.patch.object(htm, "daemonize"), \
             mock.patch.object(htm, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(htm.main(), 0)


if __name__ == "__main__":
    unittest.main()


class TestGeneratedAtIsTabScoped(unittest.TestCase):
    """「いつ生成したか」はタイトルと同じ置き場所（タブ側）に持つ。

    session 側に残すと、タイトルはタブ所有権レコードから復元できるのに
    generated_at だけが 0 に戻り、新セッションの初回で必ず「初回だから生成」
    と判断されてしまう。話題が続いていても LLM を呼ぶことになり、
    B-2（無駄な生成を減らす）が新セッションで効かない。
    """

    def _run(self, state_dir, home, session_id, label, prompt):
        fake = FakeHerdr({"result": {"tab": {"label": label}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": home,
            }
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", True), \
                 mock.patch.object(htm, "REGEN_EVERY", 0), \
                 mock.patch.object(htm, "read_prompts", return_value="ログ"), \
                 mock.patch.object(htm, "generate_title",
                                   return_value="課金請求書修正") as gen, \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                htm.run({"session_id": session_id, "cwd": os.getcwd(), "prompt": prompt})
            return gen, rename
        finally:
            fake.close()

    def test_new_session_same_topic_does_not_regenerate(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        # セッション A がタイトルを付ける
        _, rename = self._run(state_dir, home, "s1", "4", "課金の請求書を直したい")
        label = rename.call_args[0][1]

        # 別セッション B が同じ話題で来る。生成し直す理由がない
        gen, _ = self._run(
            state_dir, home, "s2", label, "課金の請求書の消費税計算も直したい"
        )
        gen.assert_not_called()

    def test_new_session_new_topic_still_regenerates(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        _, rename = self._run(state_dir, home, "s1", "4", "課金の請求書を直したい")
        label = rename.call_args[0][1]
        gen, _ = self._run(
            state_dir, home, "s2", label, "認証まわりのトークン期限を調べたい"
        )
        gen.assert_called_once()
