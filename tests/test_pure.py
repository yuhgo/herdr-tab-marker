"""純粋関数層のテスト。

ソケットも LLM も要らない関数だけを対象にする。
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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
        # マーカーに使う絵文字は 2 カラム見当で幅を配分する
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

        絵文字が実行ごとに変わると識別子として機能しない。別プロセスで seed を変えて実行し、
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
        """自分が付けうるものだけを剥がす。

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

    def test_strips_previously_used_marker(self):
        """MAP から消えた絵文字も、以前使ったものなら剥がせる。

        `{"dotfiles":"🔧"}` を設定していたタブ名 `🔧 dotfiles` が残った状態で
        MAP を削除すると、剥がせないまま手動ラベルと誤認されて
        以後タイトル生成が永久に止まってしまう。
        """
        self.assertEqual(
            htm.strip_marker("🔧 dotfiles", {}, previous_marker="🔧"), "dotfiles"
        )

    def test_previous_marker_none_is_harmless(self):
        marker = htm.PALETTE[0]
        self.assertEqual(
            htm.strip_marker(f"{marker} x", {}, previous_marker=None), "x"
        )

    def test_previous_marker_does_not_widen_to_unknown(self):
        # 記録済みのもの以外は依然として剥がさない
        self.assertEqual(
            htm.strip_marker("🦄 unicorn", {}, previous_marker="🔧"), "🦄 unicorn"
        )


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
        """剥がしてから判定しないと前回の自分の名前を誤認する。"""
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
        """絵文字 2 カラム + 空白 1 = 3 を引く。"""
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


class TestLooksLikeProse(unittest.TestCase):
    """タイトルではなく地の文（拒否・説明）を弾けること。"""

    def test_japanese_refusal(self):
        self.assertTrue(htm.looks_like_prose("具体的な作業内容を特定することができません"))

    def test_english_refusal(self):
        self.assertTrue(
            htm.looks_like_prose("This greeting alone does not provide enough context.")
        )

    def test_request_for_more_info(self):
        self.assertTrue(htm.looks_like_prose("より詳細な会話ログをご提供ください"))

    def test_sentence_punctuation_is_prose(self):
        # システムプロンプトで句読点を禁じているので、あれば地の文
        self.assertTrue(htm.looks_like_prose("認証を直しました。"))
        self.assertTrue(htm.looks_like_prose("認証、ログイン、両方"))

    def test_real_japanese_title_passes(self):
        self.assertFalse(htm.looks_like_prose("ログイン画面バリデーション修正"))

    def test_real_english_title_passes(self):
        self.assertFalse(htm.looks_like_prose("Auth token refresh fix"))

    def test_dotted_identifier_is_not_prose(self):
        # settings.json のような識別子でピリオドを誤検知しないこと
        self.assertFalse(htm.looks_like_prose("settings.json の修正"))


class TestSanitizeTitleRejectsProse(unittest.TestCase):
    def test_refusal_becomes_empty(self):
        refusal = (
            "申し訳ございませんが、提供いただいたログでは作業内容を特定できません。\n\n"
            "作業セッションのログをご確認のうえ、再度ご提供いただけますでしょうか。"
        )
        self.assertEqual(htm.sanitize_title(refusal), "")

    def test_english_refusal_becomes_empty(self):
        raw = "This greeting alone does not provide enough context to determine the work."
        self.assertEqual(htm.sanitize_title(raw), "")

    def test_valid_title_survives(self):
        self.assertEqual(htm.sanitize_title("認証エラーの調査"), "認証エラーの調査")

    def test_prefix_is_still_stripped(self):
        self.assertEqual(htm.sanitize_title("タイトル: 認証エラーの調査"), "認証エラーの調査")

    def test_overlong_output_rejected(self):
        # 予算の 2 倍を超える出力はタイトルではなく地の文とみなす
        self.assertEqual(htm.sanitize_title("あ" * 100, 10), "")


class TestIsGreetingOnly(unittest.TestCase):
    """挨拶だけのときは LLM を呼ばず、タイトルを保留すること。"""

    def test_japanese_greetings(self):
        for text in ("こんにちは", "おはよう", "おはようございます", "お疲れ様です"):
            self.assertTrue(htm.is_greeting_only(text), text)

    def test_english_greetings(self):
        for text in ("hello", "Hi!", "good morning", "thanks"):
            self.assertTrue(htm.is_greeting_only(text), text)

    def test_empty_is_greeting_only(self):
        self.assertTrue(htm.is_greeting_only(""))

    def test_all_lines_must_be_greetings(self):
        self.assertTrue(htm.is_greeting_only("こんにちは\nおはよう"))

    def test_work_prompt_is_not_greeting(self):
        self.assertFalse(htm.is_greeting_only("ログイン画面のバリデーションを直して"))

    def test_greeting_plus_work_is_not_greeting_only(self):
        # 挨拶で始まっても作業の行があればタイトルを作る
        self.assertFalse(htm.is_greeting_only("こんにちは\nログイン画面を直して"))

    def test_greeting_with_trailing_work_on_same_line(self):
        self.assertFalse(htm.is_greeting_only("こんにちは、ログインを直して"))


class TestResolveLang(unittest.TestCase):
    """明示指定 > ロケール > 既定 ja の順で決まること。

    hook は環境変数もロケールも削がれた状態で起動されることがある。
    そこで en に倒れると日本語利用者に英語タイトルが付くので、
    「英語だと判別できたときだけ en」を守る。
    """

    def _resolve(self, raw, env=None, locale_name=""):
        with mock.patch.dict(htm.os.environ, env or {}, clear=True):
            with mock.patch.object(htm.locale, "getlocale", return_value=(locale_name, None)):
                return htm._resolve_lang(raw)

    def test_explicit_value_wins_over_locale(self):
        self.assertEqual(self._resolve("en", {"LANG": "ja_JP.UTF-8"}), "en")
        self.assertEqual(self._resolve("ja", {"LANG": "en_US.UTF-8"}), "ja")

    def test_auto_follows_lang_env(self):
        self.assertEqual(self._resolve("auto", {"LANG": "ja_JP.UTF-8"}), "ja")

    def test_auto_follows_lc_all_env(self):
        self.assertEqual(self._resolve("auto", {"LC_ALL": "ja_JP.UTF-8"}), "ja")

    def test_auto_uses_en_only_when_locale_says_english(self):
        self.assertEqual(self._resolve("auto", {}, locale_name="en_US"), "en")

    def test_auto_defaults_to_ja_without_any_hint(self):
        # 環境変数もロケールも無い hook 実行環境で英語に落ちないこと
        self.assertEqual(self._resolve("auto", {}, locale_name=""), "ja")

    def test_unknown_locale_defaults_to_ja(self):
        self.assertEqual(self._resolve("auto", {}, locale_name="de_DE"), "ja")

    def test_unknown_value_falls_back_to_locale_rules(self):
        self.assertEqual(self._resolve("xx", {}, locale_name=""), "ja")

    def test_broken_locale_does_not_raise(self):
        with mock.patch.dict(htm.os.environ, {}, clear=True):
            with mock.patch.object(htm.locale, "getlocale", side_effect=ValueError):
                self.assertEqual(htm._resolve_lang("auto"), "ja")


if __name__ == "__main__":
    unittest.main()
