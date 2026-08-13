"""detect_repo のテスト。

HANDOFF.md §5-2 の要点は「`--git-common-dir` を使い `--show-toplevel` を使わない」こと。
worktree で作業していても同じリポジトリ名（＝同じ絵文字）になる必要がある。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import herdr_tab_marker as htm  # noqa: E402


def git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class TestDetectRepo(unittest.TestCase):
    def test_returns_none_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            # /tmp 直下が誰かの git リポジトリでないことを前提にせず、
            # 明示的に git を無効化した状態を作る
            self.assertIsNone(htm.detect_repo(tmp, env={"GIT_CEILING_DIRECTORIES": tmp}))

    def test_returns_none_for_missing_dir(self):
        self.assertIsNone(htm.detect_repo("/nonexistent/path/xyz"))

    def test_returns_none_for_empty_cwd(self):
        self.assertIsNone(htm.detect_repo(""))
        self.assertIsNone(htm.detect_repo(None))

    def test_detects_repo_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myproject"
            repo.mkdir()
            git("init", "-q", cwd=repo)
            self.assertEqual(htm.detect_repo(str(repo)), "myproject")

    def test_detects_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myproject"
            (repo / "src" / "deep").mkdir(parents=True)
            git("init", "-q", cwd=repo)
            self.assertEqual(htm.detect_repo(str(repo / "src" / "deep")), "myproject")

    def test_worktree_reports_main_repo_name(self):
        """worktree でも本体と同じ名前を返す（これが --git-common-dir を使う理由）。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myproject"
            repo.mkdir()
            git("init", "-q", cwd=repo)
            git("-c", "user.email=t@e", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)

            wt = Path(tmp) / "wt-feature-branch"
            git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=repo)

            main_name = htm.detect_repo(str(repo))
            wt_name = htm.detect_repo(str(wt))
            self.assertEqual(main_name, "myproject")
            self.assertEqual(
                wt_name,
                main_name,
                "worktree が本体と違う名前を返している（--show-toplevel を使っていないか確認）",
            )

    def test_worktree_marker_matches_main_repo(self):
        """名前が一致する帰結として、絵文字も一致する。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myproject"
            repo.mkdir()
            git("init", "-q", cwd=repo)
            git("-c", "user.email=t@e", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
            wt = Path(tmp) / "wt-x"
            git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=repo)

            self.assertEqual(
                htm.marker_for(htm.detect_repo(str(repo)), {}),
                htm.marker_for(htm.detect_repo(str(wt)), {}),
            )

    def test_bare_repo_strips_git_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myproject.git"
            repo.mkdir()
            git("init", "-q", "--bare", cwd=repo)
            self.assertEqual(htm.detect_repo(str(repo)), "myproject")


if __name__ == "__main__":
    unittest.main()
