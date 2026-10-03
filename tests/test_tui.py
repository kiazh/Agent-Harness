"""Tests for the ported pi-style TUI (ah/cli/tui)."""
from __future__ import annotations

import pytest

from ah.cli.tui import (
    Editor,
    Loader,
    SelectItem,
    SelectList,
    StackEntry,
    Text,
    TruncatedText,
    VStack,
    HStack,
    color_to_hex,
    get_theme,
    matches_key,
    parse_color,
    parse_key,
    strip_ansi,
    style_text,
    truncate_to_width,
    visible_width,
    wrap_text_with_ansi,
)
from ah.cli.tui.colors import OklchColor, RgbColor, TextStyle
from ah.cli.tui.components.markdown import Markdown
from ah.cli.tui.widgets import AssistantMessage, Footer, ToolExecution, UserMessage


# ─── utils ──────────────────────────────────────────────────────────────────

class TestUtils:
    def test_visible_width_ignores_ansi(self):
        styled = "\x1b[31mHello\x1b[0m"
        assert visible_width(styled) == 5

    def test_visible_width_counts_wide_chars(self):
        assert visible_width("ab") == 2
        assert visible_width("\u4e2d\u6587") == 4  # CJK = 2 cols each

    def test_strip_ansi(self):
        assert strip_ansi("\x1b[1;32mok\x1b[0m") == "ok"

    def test_truncate_adds_ellipsis(self):
        out = truncate_to_width("Hello World", 8)
        assert visible_width(out) <= 8
        assert out.endswith("...")

    def test_truncate_preserves_short(self):
        assert truncate_to_width("hi", 10) == "hi"

    def test_wrap_respects_width(self):
        lines = wrap_text_with_ansi("one two three four five", 9)
        assert all(visible_width(l) <= 9 for l in lines)
        assert len(lines) > 1


# ─── colors ─────────────────────────────────────────────────────────────────

class TestColors:
    def test_parse_hex(self):
        c = parse_color("#00aaff")
        assert isinstance(c, RgbColor)
        assert (round(c.r), round(c.g), round(c.b)) == (0, 170, 255)

    def test_parse_hex_short(self):
        assert color_to_hex(parse_color("#0af")) == "#00aaff"

    def test_parse_oklch(self):
        assert isinstance(parse_color("oklch(62% 0.1 200)"), OklchColor)

    def test_parse_okhsl_in_gamut(self):
        # Any OKHSL value must convert to a valid sRGB hex.
        hexval = color_to_hex(parse_color("okhsl(250 60% 55%)"))
        assert hexval.startswith("#") and len(hexval) == 7

    def test_okhsl_hue_sanity(self):
        # Blue-ish hue should have blue as the dominant channel.
        c = parse_color("okhsl(250 80% 55%)")
        from ah.cli.tui.colors import color_to_rgb
        rgb = color_to_rgb(c)
        assert rgb.b >= rgb.r

    def test_style_text_wraps_and_resets(self):
        out = style_text("x", TextStyle(bold=True))
        assert out.startswith("\x1b[1m")
        assert out.endswith("\x1b[22m")
        assert "x" in out

    def test_invalid_color_raises(self):
        with pytest.raises(ValueError):
            parse_color("not-a-color")


# ─── theme ──────────────────────────────────────────────────────────────────

class TestTheme:
    def test_dark_resolves_vars(self):
        t = get_theme("dark")
        # accent -> violet var -> okhsl -> hex
        assert color_to_hex(t.color("accent")).startswith("#")

    def test_fallback_roles(self):
        t = get_theme("dark")
        # thinkingMax present in dark; searchMatchBg present; both resolve.
        assert color_to_hex(t.color("thinkingMax"))
        assert color_to_hex(t.color("searchMatchBg"))

    def test_light_theme_distinct(self):
        d = color_to_hex(get_theme("dark").color("text"))
        l = color_to_hex(get_theme("light").color("text"))
        assert d != l

    def test_style_role(self):
        t = get_theme("dark")
        out = t.style("hi", "error")
        assert "hi" in out and out.startswith("\x1b[")


# ─── keys ───────────────────────────────────────────────────────────────────

class TestKeys:
    def test_enter(self):
        assert parse_key("\r") == "enter"

    def test_ctrl_c(self):
        assert parse_key("\x03") == "ctrl+c"

    def test_arrow_up(self):
        assert parse_key("\x1b[A") == "up"

    def test_printable(self):
        assert parse_key("a") == "a"

    def test_matches_key(self):
        assert matches_key("\x1b[B", "down")
        assert not matches_key("\x1b[B", "up")


# ─── components ─────────────────────────────────────────────────────────────

class TestComponents:
    def test_text_pads_to_width(self):
        lines = Text("hi", padding_x=1).render(10)
        assert all(visible_width(l) == 10 for l in lines)

    def test_truncated_single_line(self):
        out = TruncatedText("x" * 100).render(20)
        assert len(out) == 1
        assert visible_width(out[0]) <= 20

    def test_editor_has_rules_and_cursor(self):
        e = Editor()
        e.focused = True
        e.set_value("hi")
        lines = e.render(20)
        assert len(lines) >= 3  # top rule, body, bottom rule
        from ah.cli.tui.component import CURSOR_MARKER
        assert any(CURSOR_MARKER in l for l in lines)

    def test_editor_submit(self):
        got = {}
        e = Editor()
        e.on_submit = lambda v: got.setdefault("v", v)
        e.set_value("hello")
        e.handle_input("\r")
        assert got["v"] == "hello"

    def test_editor_typing(self):
        e = Editor()
        for ch in "abc":
            e.handle_input(ch)
        assert e.get_value() == "abc"

    def test_loader_frame(self):
        ld = Loader("Working")
        ld.start()
        out = ld.render(20)[0]
        assert "Working" in strip_ansi(out)

    def test_select_list_navigation(self):
        items = [SelectItem("a", "Alpha"), SelectItem("b", "Beta")]
        picked = {}
        sl = SelectList(items)
        sl.on_select = lambda it: picked.setdefault("v", it.value)
        sl.handle_input("\x1b[B")  # down
        sl.handle_input("\r")
        assert picked["v"] == "b"


# ─── layout ─────────────────────────────────────────────────────────────────

class TestLayout:
    def test_vstack_fills_height(self):
        vs = VStack([
            StackEntry(Text("body"), grow=1, min_size=1),
            StackEntry(Text("footer"), basis=1),
        ], height=6)
        assert len(vs.render(20)) == 6

    def test_hstack_splits_width(self):
        hs = HStack([StackEntry(Text("l"), grow=1), StackEntry(Text("r"), grow=1)])
        rows = hs.render(20)
        assert all(visible_width(r) <= 20 for r in rows)


# ─── markdown ───────────────────────────────────────────────────────────────

class TestMarkdown:
    def test_heading_and_list(self):
        t = get_theme("dark")
        md = Markdown("# Title\n\n- one\n- two", t)
        lines = md.render(40)
        text = "\n".join(strip_ansi(l) for l in lines)
        assert "Title" in text
        assert "one" in text and "two" in text

    def test_code_block_has_border(self):
        t = get_theme("dark")
        md = Markdown("```\ncode\n```", t)
        text = "\n".join(strip_ansi(l) for l in md.render(30))
        assert "code" in text
        assert "\u256d" in text or "\u2502" in text  # box border glyphs

    def test_width_contract(self):
        t = get_theme("dark")
        md = Markdown("a very long paragraph " * 20, t)
        assert all(visible_width(l) <= 40 for l in md.render(40))


# ─── widgets ────────────────────────────────────────────────────────────────

class TestWidgets:
    def test_user_message(self):
        t = get_theme("dark")
        lines = UserMessage("hi", t).render(30)
        assert len(lines) >= 1
        assert all(visible_width(l) <= 30 for l in lines)

    def test_assistant_message_append(self):
        t = get_theme("dark")
        am = AssistantMessage("", t)
        am.append("Hello ")
        am.append("world")
        text = "\n".join(strip_ansi(l) for l in am.render(40))
        assert "Hello world" in text

    def test_tool_execution_states(self):
        t = get_theme("dark")
        te = ToolExecution("read_file", t, "path=x")
        assert te.state == "pending"
        te.set_running()
        assert te.state == "running"
        te.set_result("done")
        assert te.state == "success"
        lines = te.render(40)
        assert all(visible_width(l) <= 40 for l in lines)

    def test_tool_execution_error(self):
        t = get_theme("dark")
        te = ToolExecution("bad", t)
        te.set_result("boom", is_error=True)
        assert te.state == "error"

    def test_footer_fields(self):
        t = get_theme("dark")
        f = Footer(t)
        f.update(session_id="abcd1234efgh", model="claude", tokens=1500, branch="main", mode="Auto")
        out = strip_ansi(f.render(80)[0])
        assert "abcd1234" in out
        assert "claude" in out
        assert "1,500" in out
        assert "main" in out
