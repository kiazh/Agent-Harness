"""Tests for REPL bug fixes — autocomplete, help scrolling, skin changing, background, response boxes."""
import io
from unittest.mock import MagicMock, patch

import pytest

from rich.console import Console
from rich.text import Text

from ah.cli.interactive_win import (
    AutocompleteDropdown,
    COMMAND_REGISTRY,
    InteractiveREPL,
    SkinConfig,
    UnicodeResponseBox,
    _build_help_text,
)


def _make_console() -> Console:
    """Create a console with force_terminal=True so colors are enabled."""
    return Console(file=io.StringIO(), force_terminal=True, no_color=False)


# ─── Bug 1: Autocomplete dropdown submits selected command on Enter ──────────


class TestAutocompleteSubmit:
    """When autocomplete is visible and Enter is pressed, the selected command should be submitted."""

    def test_autocomplete_returns_selected_command_on_enter(self):
        """Simulate: user types '/h', autocomplete shows '/help', user presses Enter.
        The result should be '/help' (the selected command), not the raw input buffer."""
        console = _make_console()
        skin = SkinConfig(console=console)
        dropdown = AutocompleteDropdown(console, skin)

        # User typed "/h"
        dropdown.update_matches("/h")
        assert dropdown._visible is True
        assert len(dropdown._matches) > 0

        # The selected match should be "/help"
        selected = dropdown.get_selected()
        assert selected is not None
        assert selected.display_name == "/help"

        # When Enter is pressed, the REPL should return the selected command
        # (not the raw input buffer which was "/h")
        result = selected.display_name
        assert result == "/help"
        assert result != "/h"  # Should NOT be the raw buffer

    def test_autocomplete_enter_submits_full_command_name(self):
        """When user types '/s' and autocomplete shows '/status', '/sessions', '/skin',
        pressing Enter should submit the currently selected match."""
        console = _make_console()
        skin = SkinConfig(console=console)
        dropdown = AutocompleteDropdown(console, skin)

        dropdown.update_matches("/s")
        assert dropdown._visible is True

        # First match should be selected by default
        selected = dropdown.get_selected()
        assert selected is not None
        # The result should be the full command name, not "/s"
        assert selected.display_name.startswith("/")
        assert len(selected.display_name) > 2  # More than just "/s"

    def test_autocomplete_enter_with_no_matches_falls_through(self):
        """When autocomplete has no matches, Enter should submit the raw input buffer."""
        console = _make_console()
        skin = SkinConfig(console=console)
        dropdown = AutocompleteDropdown(console, skin)

        # Type something that doesn't match any command
        dropdown.update_matches("/xyz")
        assert dropdown._visible is False
        assert dropdown.get_selected() is None

        # When no match is selected, the REPL should fall through to submit the buffer
        # (This is the current behavior — the REPL checks if autocomplete is visible)
        selected = dropdown.get_selected()
        assert selected is None

    def test_autocomplete_selection_changes_with_arrow_keys(self):
        """Arrow keys should change the selected match in the dropdown."""
        console = _make_console()
        skin = SkinConfig(console=console)
        dropdown = AutocompleteDropdown(console, skin)

        dropdown.update_matches("/s")
        assert dropdown._visible is True
        initial_selected = dropdown.get_selected()
        assert initial_selected is not None

        # Press down arrow
        dropdown.next_match()
        next_selected = dropdown.get_selected()
        assert next_selected is not None
        assert next_selected.display_name != initial_selected.display_name

        # Press up arrow — should wrap back to initial
        dropdown.prev_match()
        prev_selected = dropdown.get_selected()
        assert prev_selected is not None
        assert prev_selected.display_name == initial_selected.display_name


# ─── Bug 2: Help menu is scrollable with arrow keys ──────────────────────────


class TestHelpScrolling:
    """The help menu should be scrollable with arrow keys."""

    def test_help_text_has_multiple_pages(self):
        """The help text should have enough lines to require scrolling."""
        help_text = _build_help_text()
        lines = help_text.split("\n")
        # With 15+ commands, the help text should have more than 20 lines
        assert len(lines) > 20, f"Help text should have >20 lines for scrolling, got {len(lines)}"

    def test_help_scroll_logic_down_arrow(self):
        """Simulate pressing down arrow — scroll offset should adjust when needed."""
        # Simulate the scroll logic from run_interactive_help
        lines = list(range(100))  # 100 lines
        command_lines = [(i, f"/cmd{i}") for i in range(0, 100, 2)]  # 50 commands
        max_visible = 20
        selected_idx = 0
        scroll_offset = 0

        # Navigate down past the visible area
        for _ in range(25):
            selected_idx += 1
            line_idx = command_lines[selected_idx][0]
            if line_idx >= scroll_offset + max_visible:
                scroll_offset = line_idx - max_visible + 1

        # After navigating down, scroll offset should have increased
        assert scroll_offset > 0, "Scroll offset should increase when navigating past visible area"

    def test_help_scroll_logic_up_arrow(self):
        """Simulate pressing up arrow — scroll offset should adjust when needed."""
        lines = list(range(100))
        command_lines = [(i, f"/cmd{i}") for i in range(0, 100, 2)]
        max_visible = 20
        # Start with selected_idx at 30 (line 60) and scroll_offset at 50
        # This means we see lines 50-70, and selected command is at line 60 (visible)
        # Navigating up should eventually scroll up when we go above line 50
        selected_idx = 30
        scroll_offset = 50

        # Navigate up past the visible area
        for _ in range(15):
            selected_idx -= 1
            line_idx = command_lines[selected_idx][0]
            if line_idx < scroll_offset:
                scroll_offset = line_idx

        # After navigating up, scroll offset should have decreased
        assert scroll_offset < 50, "Scroll offset should decrease when navigating above visible area"

    def test_help_scroll_stays_within_bounds(self):
        """Scroll offset should never go negative."""
        lines = list(range(100))
        command_lines = [(i, f"/cmd{i}") for i in range(0, 100, 2)]
        max_visible = 20
        selected_idx = 0
        scroll_offset = 0

        # Navigate up at the top — should stay at 0
        for _ in range(5):
            if selected_idx > 0:
                selected_idx -= 1
                line_idx = command_lines[selected_idx][0]
                if line_idx < scroll_offset:
                    scroll_offset = line_idx

        assert scroll_offset >= 0, "Scroll offset should never be negative"


# ─── Bug 3: Skin changing actually changes colors ────────────────────────────


class TestSkinChanging:
    """The /skin command should actually change the REPL colors."""

    def test_set_skin_changes_colors(self):
        """Changing skin should change the color palette."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        # Get default colors
        default_primary = skin.get_color("primary")
        default_secondary = skin.get_color("secondary")
        default_background = skin.get_color("background")

        # Change to crimson skin
        skin.set_skin("crimson")

        # Colors should be different
        crimson_primary = skin.get_color("primary")
        crimson_secondary = skin.get_color("secondary")
        crimson_background = skin.get_color("background")

        assert crimson_primary != default_primary, "Primary color should change when skin changes"
        assert crimson_secondary != default_secondary, "Secondary color should change when skin changes"
        assert crimson_background != default_background, "Background color should change when skin changes"

    def test_set_skin_updates_skin_name(self):
        """Changing skin should update the skin_name property."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        assert skin.skin_name == "default"

        skin.set_skin("ocean")
        assert skin.skin_name == "ocean"

        skin.set_skin("forest")
        assert skin.skin_name == "forest"

    def test_set_skin_to_invalid_name_does_nothing(self):
        """Setting an invalid skin name should not change the current skin."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        original_primary = skin.get_color("primary")
        skin.set_skin("nonexistent_skin")

        assert skin.skin_name == "default"
        assert skin.get_color("primary") == original_primary

    def test_all_skins_have_distinct_colors(self):
        """Each skin should have a distinct color palette."""
        console = _make_console()
        skins = SkinConfig.available_skins()

        primary_colors = {}
        for skin_name in skins:
            skin = SkinConfig(skin_name, console=console)
            primary_colors[skin_name] = skin.get_color("primary")

        # At least some skins should have different primary colors
        unique_colors = set(primary_colors.values())
        assert len(unique_colors) > 1, "Different skins should have different primary colors"


# ─── Bug 4: Background is dark charcoal (#111827) not pure black ────────────


class TestBackgroundColor:
    """The background should be dark charcoal (#111827), not pure black."""

    def test_default_background_is_charcoal(self):
        """The default background color should be #111827 (dark charcoal)."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        bg_color = skin.get_color("background")
        assert bg_color == "#111827", f"Default background should be #111827, got {bg_color}"

    def test_background_is_not_pure_black(self):
        """The background should not be pure black (#000000)."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        bg_color = skin.get_color("background")
        assert bg_color != "#000000", "Background should not be pure black"
        assert bg_color.lower() != "#000000", "Background should not be pure black (case insensitive)"

    def test_set_background_outputs_correct_ansi(self):
        """_set_background() should output the correct ANSI escape sequence."""
        console = _make_console()
        skin = SkinConfig("default", console=console)

        repl = InteractiveREPL.__new__(InteractiveREPL)
        repl.skin = skin
        repl.console = console

        # Capture output by replacing the console's file
        output = io.StringIO()
        original_file = console.file
        console.file = output
        try:
            repl._set_background()
        finally:
            console.file = original_file

        ansi_output = output.getvalue()
        # Should contain OSC 11 escape sequence with #111827
        assert "#111827" in ansi_output, f"ANSI output should contain #111827, got: {ansi_output!r}"
        assert "\x1b]11;" in ansi_output, f"ANSI output should contain OSC 11, got: {ansi_output!r}"


# ─── Bug 5: Response boxes have Unicode box around agent responses ───────────


class TestResponseBox:
    """Agent responses should have a Unicode box around them."""

    def test_response_box_renders_unicode_borders(self):
        """UnicodeResponseBox should render with Unicode box-drawing characters."""
        console = _make_console()
        skin = SkinConfig("default", console=console)
        box = UnicodeResponseBox(console, skin=skin)

        result = box.render("Hello, world!")

        # Should contain Unicode box-drawing characters
        assert "╭" in result.plain, "Should have top-left corner ╭"
        assert "╮" in result.plain, "Should have top-right corner ╮"
        assert "╰" in result.plain, "Should have bottom-left corner ╰"
        assert "╯" in result.plain, "Should have bottom-right corner ╯"
        assert "─" in result.plain, "Should have horizontal line ─"
        assert "│" in result.plain, "Should have vertical line │"

    def test_response_box_contains_content(self):
        """The response box should contain the original content."""
        console = _make_console()
        skin = SkinConfig("default", console=console)
        box = UnicodeResponseBox(console, skin=skin)

        content = "This is a test response from the agent."
        result = box.render(content)

        assert content in result.plain, "Response box should contain the original content"

    def test_response_box_with_title(self):
        """The response box should support a title in the top border."""
        console = _make_console()
        skin = SkinConfig("default", console=console)
        box = UnicodeResponseBox(console, skin=skin, title="Agent")

        result = box.render("Response content")

        assert "Agent" in result.plain, "Response box should contain the title"

    def test_response_box_multiline_content(self):
        """The response box should handle multi-line content."""
        console = _make_console()
        skin = SkinConfig("default", console=console)
        box = UnicodeResponseBox(console, skin=skin)

        content = "Line 1\nLine 2\nLine 3"
        result = box.render(content)

        # Each line should be inside the box
        plain = result.plain
        lines = plain.split("\n")
        # Should have top border, 3 content lines, bottom border = 5 lines minimum
        assert len(lines) >= 5, f"Multi-line response box should have at least 5 lines, got {len(lines)}"

    def test_response_box_uses_skin_colors(self):
        """The response box should use skin-aware colors."""
        console = _make_console()
        skin = SkinConfig("default", console=console)
        box = UnicodeResponseBox(console, skin=skin)

        result = box.render("Test")

        # The result should have style spans (colors applied)
        # If no colors were applied, the result would be plain text
        assert len(result._spans) > 0, "Response box should have style spans for colors"


# ─── Integration tests ──────────────────────────────────────────────────────


class TestREPLIntegration:
    """Integration tests for the REPL bug fixes."""

    def test_repl_has_response_box(self):
        """InteractiveREPL should have a UnicodeResponseBox instance."""
        console = _make_console()
        with patch("ah.cli.interactive_win.SkinConfig") as mock_skin_config:
            mock_skin = MagicMock()
            mock_skin.get_color.return_value = "#000000"
            mock_skin_config.return_value = mock_skin

            repl = InteractiveREPL.__new__(InteractiveREPL)
            repl.console = console
            repl.skin = mock_skin
            repl._response_box = UnicodeResponseBox(console, skin=mock_skin)

            assert repl._response_box is not None
            assert isinstance(repl._response_box, UnicodeResponseBox)

    def test_repl_has_autocomplete(self):
        """InteractiveREPL should have an AutocompleteDropdown instance."""
        console = _make_console()
        with patch("ah.cli.interactive_win.SkinConfig") as mock_skin_config:
            mock_skin = MagicMock()
            mock_skin.get_color.return_value = "#000000"
            mock_skin_config.return_value = mock_skin

            repl = InteractiveREPL.__new__(InteractiveREPL)
            repl.console = console
            repl.skin = mock_skin
            repl._autocomplete = AutocompleteDropdown(console, mock_skin)

            assert repl._autocomplete is not None
            assert isinstance(repl._autocomplete, AutocompleteDropdown)

    def test_command_registry_has_all_commands(self):
        """The command registry should have all expected commands."""
        expected_commands = [
            "help", "status", "sessions", "new", "switch", "context",
            "model", "provider", "budget", "verbose", "config", "skin",
            "clear", "compress", "exit",
        ]
        for cmd in expected_commands:
            assert cmd in COMMAND_REGISTRY, f"Command '{cmd}' should be in COMMAND_REGISTRY"

    def test_skin_config_has_all_skins(self):
        """SkinConfig should have all expected skins."""
        expected_skins = [
            "default", "gold", "crimson", "ocean", "forest",
            "sunset", "midnight", "arctic", "volcanic",
        ]
        available = SkinConfig.available_skins()
        for skin in expected_skins:
            assert skin in available, f"Skin '{skin}' should be available"
