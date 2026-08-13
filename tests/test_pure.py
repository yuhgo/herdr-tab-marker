"""純粋関数層のテスト。

ソケットも LLM も要らない関数だけを対象にする（HANDOFF.md §5-7）。
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import herdr_tab_marker as htm  # noqa: E402


class TestDisplayWidth(unittest.TestCase):
    def test_ascii_counts_one_each(self):
        self.assertEqual(htm.display_width("abc"), 3)

    def test_fullwidth_counts_two_each(self):
        self.assertEqual(htm.display_width("認証"), 4)

    def test_mixed(self):
        self.assertEqual(htm.display_width("認証a"), 5)

    def test_empty(self):
        self.assertEqual(htm.display_width(""), 0)

    def test_emoji_counts_two(self):
        # マーカーに使う絵文字は 2 カラム見当で幅を配分する（HANDOFF.md §5-2）
        self.assertEqual(htm.display_width("🟦"), 2)


class TestTruncate(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(htm.truncate("abc", 10), "abc")

    def test_exact_fit_untouched(self):
        self.assertEqual(htm.truncate("abcde", 5), "abcde")

    def test_long_ascii_gets_ellipsis(self):
        out = htm.truncate("abcdefghij", 5)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(htm.display_width(out), 5)

    def test_long_fullwidth_respects_width(self):
        out = htm.truncate("認証まわりを調査する", 8)
        self.assertLessEqual(htm.display_width(out), 8)
        self.assertTrue(out.endswith("…"))

    def test_never_exceeds_budget(self):
        for width in range(2, 20):
            out = htm.truncate("認証aまわりbを調査cする", width)
            self.assertLessEqual(
                htm.display_width(out), width, f"width={width} out={out!r}"
            )


class TestSanitizeTitle(unittest.TestCase):
    def test_plain_title(self):
        self.assertEqual(htm.sanitize_title("認証まわりを調査"), "認証まわりを調査")

    def test_strips_quotes(self):
        self.assertEqual(htm.sanitize_title('"認証調査"'), "認証調査")

    def test_strips_title_prefix(self):
        self.assertEqual(htm.sanitize_title("タイトル: 認証調査"), "認証調査")
        self.assertEqual(htm.sanitize_title("Title: auth probe"), "auth probe")

    def test_takes_last_nonempty_line(self):
        # 前置きが混ざった場合の保険
        self.assertEqual(htm.sanitize_title("考えます\n\n認証調査"), "認証調査")

    def test_collapses_whitespace(self):
        self.assertEqual(htm.sanitize_title("auth    probe"), "auth probe")

    def test_empty_returns_empty(self):
        self.assertEqual(htm.sanitize_title(""), "")
        self.assertEqual(htm.sanitize_title("\n\n"), "")

    def test_strips_control_chars(self):
        self.assertEqual(htm.sanitize_title("認証\x00調査"), "認証調査")

    def test_respects_max_width_argument(self):
        out = htm.sanitize_title("認証まわりを徹底的に調査する", max_width=8)
        self.assertLessEqual(htm.display_width(out), 8)


class TestStatePath(unittest.TestCase):
    def test_sanitizes_separators(self):
        path = htm.state_path("a/b:c")
        self.assertNotIn("/", path.name)
        self.assertNotIn(":", path.name)

    def test_empty_falls_back(self):
        self.assertEqual(htm.state_path("").name, "unknown.json")

    def test_keeps_safe_chars(self):
        self.assertEqual(htm.state_path("abc-123_x.y").name, "abc-123_x.y.json")


class TestMarkerFor(unittest.TestCase):
    def test_explicit_map_wins(self):
        self.assertEqual(htm.marker_for("dotfiles", {"dotfiles": "🔧"}), "🔧")

    def test_falls_back_to_palette(self):
        marker = htm.marker_for("herdr-tab-marker", {})
        self.assertIn(marker, htm.PALETTE)

    def test_stable_within_process(self):
        a = htm.marker_for("some-repo", {})
        b = htm.marker_for("some-repo", {})
        self.assertEqual(a, b)

    def test_stable_across_processes(self):
        """組み込み hash() は PYTHONHASHSEED で変わるので使っていないことの確認。

        HANDOFF.md §5-2 が明示的に禁じている罠。別プロセスで seed を変えて実行し、
        同じ絵文字が返ることを確かめる。
        """
        root = str(Path(__file__).resolve().parent.parent)
        code = (
            f"import sys; sys.path.insert(0, {root!r});"
            "import herdr_tab_marker as h;"
            "print(h.marker_for('innocom-learning-platform', {}))"
        )
        results = set()
        for seed in ("0", "1", "12345"):
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            )
            results.add(out.stdout.strip())
        self.assertEqual(
            len(results), 1, f"marker changed across hash seeds: {results}"
        )

    def test_distributes_across_palette(self):
        names = [f"repo-{i}" for i in range(200)]
        used = {htm.marker_for(n, {}) for n in names}
        # 全部同じ絵文字に潰れていない（ハッシュが機能している）
        self.assertGreater(len(used), len(htm.PALETTE) // 2)

    def test_empty_name_returns_none(self):
        self.assertIsNone(htm.marker_for("", {}))
        self.assertIsNone(htm.marker_for(None, {}))

    def test_palette_has_no_duplicates(self):
        self.assertEqual(len(htm.PALETTE), len(set(htm.PALETTE)))

    def test_palette_markers_are_two_columns(self):
        for marker in htm.PALETTE:
            self.assertEqual(
                htm.display_width(marker), 2, f"{marker!r} is not 2 columns"
            )


class TestStripMarker(unittest.TestCase):
    def test_strips_palette_marker(self):
        marker = htm.PALETTE[0]
        self.assertEqual(htm.strip_marker(f"{marker} ssh", {}), "ssh")

    def test_strips_repeated_markers(self):
        marker = htm.PALETTE[0]
        self.assertEqual(htm.strip_marker(f"{marker}{marker} x", {}), "x")

    def test_no_marker_is_unchanged(self):
        self.assertEqual(htm.strip_marker("ssh", {}), "ssh")

    def test_strips_map_values_too(self):
        self.assertEqual(htm.strip_marker("🔧 dotfiles", {"dotfiles": "🔧"}), "dotfiles")

    def test_leaves_unknown_emoji_alone(self):
        """自分が付けうるものだけを剥がす（HANDOFF.md §5-2）。

        「絵文字とは何か」を正規表現で書き切ろうとしない方針の確認。
        """
        self.assertEqual(htm.strip_marker("🦄 unicorn", {}), "🦄 unicorn")

    def test_strips_without_space(self):
        marker = htm.PALETTE[0]
        self.assertEqual(htm.strip_marker(f"{marker}ssh", {}), "ssh")

    def test_marker_only_becomes_empty(self):
        marker = htm.PALETTE[0]
        self.assertEqual(htm.strip_marker(f"{marker} ", {}), "")

    def test_handles_none(self):
        self.assertEqual(htm.strip_marker(None, {}), "")

    def test_does_not_strip_from_middle(self):
        marker = htm.PALETTE[0]
        self.assertEqual(htm.strip_marker(f"ssh {marker}", {}), f"ssh {marker}")


class TestMayOverwriteTitle(unittest.TestCase):
    def test_empty_is_overwritable(self):
        self.assertTrue(htm.may_overwrite_title("", None))
        self.assertTrue(htm.may_overwrite_title(None, None))
        self.assertTrue(htm.may_overwrite_title("   ", None))

    def test_herdr_default_numeric_label_is_overwritable(self):
        self.assertTrue(htm.may_overwrite_title("4", None))
        self.assertTrue(htm.may_overwrite_title("12", None))

    def test_previous_own_title_is_overwritable(self):
        self.assertTrue(htm.may_overwrite_title("認証調査", "認証調査"))

    def test_manual_label_is_protected(self):
        self.assertFalse(htm.may_overwrite_title("ssh", None))
        self.assertFalse(htm.may_overwrite_title("ssh", "認証調査"))

    def test_judged_after_strip(self):
        """HANDOFF.md §5-2: 剥がしてから判定しないと前回の自分の名前を誤認する。"""
        marker = htm.PALETTE[0]
        previous = "認証調査"
        labelled = f"{marker} {previous}"
        # 剥がさずに渡すと手動ラベル扱いになってしまう（誤った使い方）
        self.assertFalse(htm.may_overwrite_title(labelled, previous))
        # 剥がしてから渡すのが正しい
        stripped = htm.strip_marker(labelled, {})
        self.assertTrue(htm.may_overwrite_title(stripped, previous))


class TestIsDisposableTitle(unittest.TestCase):
    """herdr 既定の連番は「保持する価値のないタイトル」として捨てる。

    捨てないと TITLE=0 の軽量モードで `🟦 5` のような残骸が残る。
    """

    def test_numeric_label_is_disposable(self):
        self.assertTrue(htm.is_disposable_title("5"))
        self.assertTrue(htm.is_disposable_title("12"))

    def test_empty_is_disposable(self):
        self.assertTrue(htm.is_disposable_title(""))
        self.assertTrue(htm.is_disposable_title(None))
        self.assertTrue(htm.is_disposable_title("   "))

    def test_real_title_is_kept(self):
        self.assertFalse(htm.is_disposable_title("認証調査"))
        self.assertFalse(htm.is_disposable_title("ssh"))

    def test_alphanumeric_is_kept(self):
        # 連番だけが対象。数字を含む名前は捨てない
        self.assertFalse(htm.is_disposable_title("v2"))
        self.assertFalse(htm.is_disposable_title("phase 3"))


class TestComposeLabel(unittest.TestCase):
    def test_marker_and_title(self):
        self.assertEqual(htm.compose_label("🟦", "認証調査"), "🟦 認証調査")

    def test_title_only(self):
        self.assertEqual(htm.compose_label(None, "認証調査"), "認証調査")

    def test_marker_only(self):
        self.assertEqual(htm.compose_label("🟦", ""), "🟦")

    def test_both_empty(self):
        self.assertEqual(htm.compose_label(None, ""), "")


class TestTitleBudget(unittest.TestCase):
    def test_marker_reserves_three_columns(self):
        """絵文字 2 カラム + 空白 1 = 3 を引く（HANDOFF.md §5-2）。"""
        self.assertEqual(htm.title_budget(28, "🟦"), 25)

    def test_no_marker_uses_full_width(self):
        self.assertEqual(htm.title_budget(28, None), 28)

    def test_never_negative(self):
        self.assertGreaterEqual(htm.title_budget(2, "🟦"), 0)


class TestParseMarkerMap(unittest.TestCase):
    def test_parses_json(self):
        self.assertEqual(htm.parse_marker_map('{"dotfiles":"🔧"}'), {"dotfiles": "🔧"})

    def test_empty_returns_empty_dict(self):
        self.assertEqual(htm.parse_marker_map(""), {})
        self.assertEqual(htm.parse_marker_map(None), {})

    def test_broken_json_returns_empty_dict(self):
        # 設定ミスで hook 全体が落ちないこと
        self.assertEqual(htm.parse_marker_map("{not json"), {})

    def test_non_mapping_returns_empty_dict(self):
        self.assertEqual(htm.parse_marker_map('["a"]'), {})

    def test_drops_non_string_values(self):
        self.assertEqual(htm.parse_marker_map('{"a": 1, "b": "🔧"}'), {"b": "🔧"})


if __name__ == "__main__":
    unittest.main()
