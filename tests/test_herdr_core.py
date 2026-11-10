"""herdr core（デスクトップアプリ）経路のテスト。

本物の herdr-core の代わりに、`connect` で JSON-RPC を話す偽の実行ファイルを置いて確かめる。
偽物の状態（ターミナルと文書）は実行ファイルの隣の JSON に持つ。
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import herdr_tab_marker as htm  # noqa: E402

FAKE_CORE = r'''
import json, sys
from pathlib import Path
state_path = Path(__file__).with_suffix(".json")
state = json.loads(state_path.read_text())
log = state.setdefault("calls", [])
for line in sys.stdin:
    req = json.loads(line)
    method, params = req["method"], req["params"]
    log.append(method)
    out = {"jsonrpc": "2.0", "id": req["id"]}
    if method == "terminal.get":
        term = state["terminals"].get(params["terminal"])
        if term is None:
            out["error"] = {"code": -32000, "message": "not_found", "data": {"kind": "not_found"}}
        else:
            out["result"] = term
    elif method == "doc.get":
        doc = state.get("doc")
        out["result"] = {"value": doc, "version": state.get("version", 0)}
    elif method == "doc.set":
        if state.get("conflicts", 0) > 0:
            state["conflicts"] -= 1
            state["version"] += 1
            out["error"] = {"code": -32000, "message": "conflict", "data": {"kind": "conflict"}}
        elif params["version"] != state.get("version", 0):
            out["error"] = {"code": -32000, "message": "conflict", "data": {"kind": "conflict"}}
        else:
            state["doc"] = params["value"]
            state["version"] += 1
            out["result"] = {"version": state["version"]}
    print(json.dumps(out), flush=True)
state_path.write_text(json.dumps(state))
'''


# Windows 版 herdr-core.exe の connect は、stdin が閉じた瞬間に未回答のまま終わる（2026-11-10 実測）。
# その振る舞いを真似る偽物。返事は少し遅らせ、stdin の EOF を見たら即座に終わる。
EAGER_EXIT_CORE = r'''
import json, os, queue, sys, threading, time
requests = queue.Queue()
def reader():
    for line in sys.stdin:
        requests.put(json.loads(line))
    os._exit(0)
threading.Thread(target=reader, daemon=True).start()
while True:
    req = requests.get()
    time.sleep(0.2)
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"ok": req["method"]}}), flush=True)
'''


def doc_with(*tabs):
    return {
        "spaces": [
            {"id": "sp1", "name": "", "tabs": [{"id": tid, "name": name} for tid, name in tabs]}
        ]
    }


class FakeCore:
    """偽の herdr-core。`path` を候補として渡し、`state()` で中身を確かめる。"""

    def __init__(self, terminals=None, doc=None, version=3, conflicts=0, name="herdr-core"):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / name
        script = self.dir / (name + ".py")
        script.write_text(FAKE_CORE)
        script.with_suffix(".json").write_text(
            json.dumps(
                {"terminals": terminals or {}, "doc": doc, "version": version,
                 "conflicts": conflicts}
            )
        )
        self.path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self._state = script.with_suffix(".json")

    def state(self):
        return json.loads(self._state.read_text())


def terminal(tab_id, pid=4242):
    return {"address": "t7", "pid": pid, "labels": {"herdr.tab": tab_id, "where": "x / y"}}


class TestHcRpc(unittest.TestCase):
    def test_round_trip_keeps_call_order(self):
        core = FakeCore(terminals={"t7": terminal("tb1")}, doc=doc_with(("tb1", "")))
        terminal_resp, doc_resp = htm.hc_rpc(
            str(core.path),
            [("terminal.get", {"terminal": "t7"}), ("doc.get", {"name": "herdr-app"})],
        )
        self.assertEqual(terminal_resp["result"]["labels"]["herdr.tab"], "tb1")
        self.assertEqual(doc_resp["result"]["version"], 3)

    def test_waits_for_replies_before_closing_stdin(self):
        """stdin の EOF で即終了する hc（Windows 版）でも、返事を受け取ってから閉じる。"""
        d = Path(tempfile.mkdtemp())
        script = d / "core.py"
        script.write_text(EAGER_EXIT_CORE)
        core = d / "herdr-core"
        core.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        core.chmod(core.stat().st_mode | stat.S_IXUSR)
        responses = htm.hc_rpc(str(core), [("terminal.get", {}), ("doc.get", {})], timeout=5)
        self.assertEqual([r["result"]["ok"] for r in responses], ["terminal.get", "doc.get"])

    def test_missing_binary_returns_nones(self):
        self.assertEqual(htm.hc_rpc("/nonexistent/herdr-core", [("doc.get", {})]), [None])


class TestHcFindTab(unittest.TestCase):
    def test_finds_tab(self):
        tab = htm.hc_find_tab(doc_with(("a", "x"), ("b", "y")), "b")
        self.assertEqual(tab["name"], "y")

    def test_unexpected_shapes_give_none(self):
        """アプリ内部の形式が変わっても、壊さずに諦める。"""
        for value in (None, [], {"spaces": "x"}, {"spaces": [{"tabs": [{"id": "b"}]}]},
                      {"spaces": [None, {"tabs": None}]}):
            self.assertIsNone(htm.hc_find_tab(value, "b"), value)


class TestHcTab(unittest.TestCase):
    def test_label_and_rename(self):
        core = FakeCore(doc=doc_with(("tb1", "手で付けた名前"), ("tb2", "")))
        tab = htm.HcTab(str(core.path), "tb1")
        self.assertEqual(tab.key, "hc:tb1")
        self.assertEqual(tab.label(), "手で付けた名前")
        self.assertTrue(tab.rename("認証まわりの調査"))
        names = [t["name"] for t in core.state()["doc"]["spaces"][0]["tabs"]]
        # 他のタブには触らない
        self.assertEqual(names, ["認証まわりの調査", ""])

    def test_auto_name_counts_as_empty(self):
        core = FakeCore(doc=doc_with(("tb1", "Claude Code")))
        self.assertEqual(htm.HcTab(str(core.path), "tb1").label(), "")

    def test_rename_retries_on_conflict(self):
        """アプリが先に書いたら最新の version で読み直して書く。"""
        core = FakeCore(doc=doc_with(("tb1", "")), conflicts=1)
        self.assertTrue(htm.HcTab(str(core.path), "tb1").rename("新しい名前"))
        self.assertEqual(core.state()["doc"]["spaces"][0]["tabs"][0]["name"], "新しい名前")

    def test_rename_gives_up_after_repeated_conflicts(self):
        core = FakeCore(doc=doc_with(("tb1", "")), conflicts=99)
        self.assertFalse(htm.HcTab(str(core.path), "tb1").rename("x"))

    def test_rename_missing_tab_fails(self):
        core = FakeCore(doc=doc_with(("other", "")))
        self.assertFalse(htm.HcTab(str(core.path), "tb1").rename("x"))
        self.assertNotIn("doc.set", core.state()["calls"])


class TestResolveHcTab(unittest.TestCase):
    def _resolve(self, cores, ancestors=frozenset()):
        with mock.patch.object(htm, "hc_candidates", return_value=[str(c.path) for c in cores]):
            return htm.resolve_hc_tab("t7", ancestors)

    def test_single_core(self):
        core = FakeCore(terminals={"t7": terminal("tb1")}, doc=doc_with(("tb1", "")))
        tab = self._resolve([core])
        self.assertEqual((tab.binary, tab.tab_id), (str(core.path), "tb1"))

    def test_ancestor_pid_disambiguates_same_terminal_number(self):
        """WSL で Linux 版と Windows 版の両方に t7 があっても、自分の祖先の方を選ぶ。"""
        other = FakeCore(terminals={"t7": terminal("tbX", pid=1111)}, doc=doc_with(("tbX", "")))
        mine = FakeCore(terminals={"t7": terminal("tbM", pid=2222)}, doc=doc_with(("tbM", "")))
        tab = self._resolve([other, mine], ancestors=frozenset({2222}))
        self.assertEqual((tab.binary, tab.tab_id), (str(mine.path), "tbM"))

    def test_ambiguous_without_ancestor_gives_up(self):
        a = FakeCore(terminals={"t7": terminal("tbA", pid=1)}, doc=doc_with(("tbA", "")))
        b = FakeCore(terminals={"t7": terminal("tbB", pid=2)}, doc=doc_with(("tbB", "")))
        self.assertIsNone(self._resolve([a, b]))

    def test_doc_can_live_on_another_core(self):
        """Windows のアプリが WSL をマシンとしてつなぐと、ターミナルと文書の hc が分かれる。"""
        wsl = FakeCore(terminals={"t7": terminal("tb1", pid=10)}, doc=None)
        windows = FakeCore(terminals={}, doc=doc_with(("tb1", "")), name="herdr-core.exe")
        tab = self._resolve([wsl, windows], ancestors=frozenset({10}))
        self.assertEqual((tab.binary, tab.tab_id), (str(windows.path), "tb1"))

    def test_terminal_without_tab_label(self):
        core = FakeCore(terminals={"t7": {"address": "t7", "pid": 1, "labels": {}}},
                        doc=doc_with(("tb1", "")))
        self.assertIsNone(self._resolve([core]))

    def test_no_binary(self):
        self.assertIsNone(self._resolve([]))


class TestHcCandidates(unittest.TestCase):
    def test_explicit_and_wsl_windows_exe(self):
        explicit = FakeCore(name="my-core")
        windows = FakeCore(name="herdr-core.exe")
        env = {"HERDR_TAB_MARKER_HC_BIN": str(explicit.path), "PATH": "", "HOME": "/nonexistent"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(htm, "is_wsl", return_value=True), \
             mock.patch.object(htm.glob, "glob", return_value=[str(windows.path)]):
            self.assertEqual(htm.hc_candidates(), [str(explicit.path), str(windows.path)])

    def test_windows_exe_is_not_searched_outside_wsl(self):
        with mock.patch.dict(os.environ, {"PATH": "", "HOME": "/nonexistent"}, clear=True), \
             mock.patch.object(htm, "is_wsl", return_value=False), \
             mock.patch.object(htm.glob, "glob") as globber:
            self.assertEqual(htm.hc_candidates(), [])
        globber.assert_not_called()

    def test_duplicates_are_dropped(self):
        core = FakeCore()
        env = {"HERDR_TAB_MARKER_HC_BIN": f"{core.path}{os.pathsep}{core.path}",
               "PATH": str(core.dir), "HOME": "/nonexistent"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(htm, "is_wsl", return_value=False):
            self.assertEqual(htm.hc_candidates(), [str(core.path)])


class TestResolveTabChoice(unittest.TestCase):
    def test_herdr_tui_wins_when_nested(self):
        """herdr TUI を herdr core のターミナルの中で動かしたら内側（TUI）のタブを選ぶ。"""
        env = {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": "/x.sock", "HERDR_TAB_ID": "w1:t4",
               "HC_TERMINAL_ID": "t7"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(htm, "resolve_hc_tab") as hc:
            tab = htm.resolve_tab()
        self.assertIsInstance(tab, htm.HerdrTab)
        hc.assert_not_called()

    def test_stale_socket_path_inside_hc(self):
        env = {"HERDR_SOCKET_PATH": "/stale.sock", "HC_TERMINAL_ID": "t7"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(htm, "resolve_hc_tab", return_value="hc-tab") as hc:
            self.assertEqual(htm.resolve_tab(), "hc-tab")
        hc.assert_called_once_with("t7")


class TestMainInHc(unittest.TestCase):
    def test_runs_with_only_hc_terminal_id(self):
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t7"}, clear=True), \
             mock.patch("sys.stdin.read", return_value='{"session_id":"s"}'), \
             mock.patch.object(htm, "run") as run, \
             mock.patch.object(htm, "daemonize") as daemonize, \
             mock.patch.object(htm, "ancestor_pids", return_value=frozenset({99})) as anc:
            self.assertEqual(htm.main(), 0)
            # fork すると祖先をたどれなくなるので、daemonize より前に記録している
            self.assertEqual(htm._ANCESTOR_PIDS, frozenset({99}))
        anc.assert_called_once()
        run.assert_called_once()
        daemonize.assert_called_once()

    def test_child_env_strips_hc_terminal_id(self):
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t7", "PATH": "/bin"}, clear=True):
            env = htm.child_env()
        self.assertNotIn("HC_TERMINAL_ID", env)


class TestAncestorPids(unittest.TestCase):
    def test_includes_parent_but_not_self(self):
        pids = htm.ancestor_pids()
        self.assertIn(os.getppid(), pids)
        self.assertNotIn(os.getpid(), pids)


class TestRunWithHc(unittest.TestCase):
    """run() を herdr core 経路で一巡させる。"""

    def _run(self, current_name):
        core = FakeCore(terminals={"t7": terminal("tb1", pid=777)},
                        doc=doc_with(("tb1", current_name)))
        state_dir = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"HC_TERMINAL_ID": "t7", "HOME": tempfile.mkdtemp()},
                             clear=True), \
             mock.patch.object(htm, "hc_candidates", return_value=[str(core.path)]), \
             mock.patch.object(htm, "_ANCESTOR_PIDS", frozenset({777})), \
             mock.patch.object(htm, "STATE_DIR", state_dir), \
             mock.patch.object(htm, "TITLE_ENABLED", True), \
             mock.patch.object(htm, "generate_title", return_value="生成されたタイトル") as gen:
            htm.run({"session_id": "s1", "cwd": os.getcwd(), "prompt": "認証を調べて"})
        name = core.state()["doc"]["spaces"][0]["tabs"][0]["name"]
        return name, gen, state_dir

    def test_empty_name_gets_title_and_ownership_is_recorded(self):
        name, gen, state_dir = self._run("")
        self.assertEqual(name, "生成されたタイトル")
        tab_state = json.loads((state_dir / "tabs" / "hc_tb1.json").read_text())
        self.assertEqual(tab_state["title"], "生成されたタイトル")

    def test_auto_named_tab_gets_title(self):
        name, _, _ = self._run("Claude Code")
        self.assertEqual(name, "生成されたタイトル")

    def test_manual_name_is_left_alone(self):
        name, gen, _ = self._run("ssh")
        self.assertEqual(name, "ssh")
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
