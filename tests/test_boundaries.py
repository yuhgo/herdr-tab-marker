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

    「タイトルは尊重、絵文字は付ける」が本実装の中核なので、
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

    def test_manual_label_keeps_title_but_gains_marker(self):
        generate, rename = self._run("ssh")
        # 手動ラベルなので LLM は呼ばない（コストゼロ）
        generate.assert_not_called()
        # 絵文字だけ付く
        self.assertEqual(rename.call_count, 1)
        new_label = rename.call_args[0][1]
        self.assertTrue(new_label.endswith(" ssh"), new_label)
        self.assertNotEqual(new_label, "ssh")

    def test_default_numeric_label_is_replaced_by_title(self):
        generate, rename = self._run("4")
        generate.assert_called_once()
        new_label = rename.call_args[0][1]
        self.assertTrue(new_label.endswith("生成されたタイトル"), new_label)
        # 連番は残さない
        self.assertNotIn("4", new_label)

    def test_already_marked_manual_label_is_not_double_marked(self):
        # 前回 marker を付けた状態で再実行しても increment しない
        repo = htm.detect_repo(os.getcwd())
        marker = htm.marker_for(repo, {})
        generate, rename = self._run(f"{marker} ssh")
        generate.assert_not_called()
        rename.assert_not_called()  # 変化なしなので rename も呼ばない


class TestMarkerPersistence(unittest.TestCase):
    """state に使った絵文字を記録し、MAP 変更後も剥がせること。

    記録が漏れると `🔧 dotfiles` が手動ラベルと誤認され、
    以後そのタブのタイトルが永久に更新されなくなる。
    """

    def _run_once(self, state_dir, current_label, marker_map_json, home):
        fake = FakeHerdr({"result": {"tab": {"label": current_label}}})
        try:
            env = {
                "HERDR_SOCKET_PATH": fake.path,
                "HERDR_TAB_ID": "w1:t4",
                "HOME": home,
            }
            if marker_map_json:
                env["HERDR_TAB_MARKER_MAP"] = marker_map_json
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(htm, "STATE_DIR", state_dir), \
                 mock.patch.object(htm, "TITLE_ENABLED", False), \
                 mock.patch.object(htm, "rename_tab", return_value=True) as rename:
                htm.run({"session_id": "s1", "cwd": os.getcwd()})
            return rename
        finally:
            fake.close()

    def test_marker_is_recorded_in_state(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        repo = htm.detect_repo(os.getcwd())
        self._run_once(state_dir, "4", f'{{"{repo}":"🔧"}}', home)
        state = htm.load_state(state_dir / "s1.json")
        self.assertEqual(
            state.get("marker"), "🔧", "使った絵文字が state に記録されていない"
        )

    def test_removed_map_value_is_still_stripped(self):
        state_dir = Path(tempfile.mkdtemp())
        home = tempfile.mkdtemp()
        repo = htm.detect_repo(os.getcwd())
        # 1 回目: MAP ありで 🔧 を付ける
        self._run_once(state_dir, "4", f'{{"{repo}":"🔧"}}', home)
        # 2 回目: MAP を消した状態で、🔧 が付いたラベルを渡す
        rename = self._run_once(state_dir, "🔧 手動の名前", None, home)
        self.assertEqual(rename.call_count, 1)
        new_label = rename.call_args[0][1]
        # 🔧 は剥がされ、パレット由来の絵文字に貼り替わる
        self.assertNotIn("🔧", new_label)
        self.assertTrue(new_label.endswith("手動の名前"), new_label)
        # 二重に絵文字が付いていない
        self.assertEqual(htm.display_width(new_label), 2 + 1 + htm.display_width("手動の名前"))


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
