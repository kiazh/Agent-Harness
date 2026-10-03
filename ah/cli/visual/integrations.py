"""Integrations: prompt_toolkit styles and syntax highlighting rules."""
from __future__ import annotations

from ah.cli.visual.themes import ColorScheme

# ─── prompt_toolkit Style Generation ────────────────────────────────────────


def generate_prompt_toolkit_style(
    scheme: ColorScheme,
    light: bool = False,
) -> dict[str, str]:
    """Generate a prompt_toolkit style dictionary from a ColorScheme.

    Creates a mapping of prompt_toolkit style names to color strings
    that can be used with prompt_toolkit's Style.from_dict().

    Args:
        scheme: The ColorScheme to derive styles from.
        light: Whether to use light-mode colors.

    Returns:
        Dictionary mapping style names to color strings.
    """
    return {
        "prompt": scheme.get("primary", light=light),
        "prompt-scheme": scheme.get("secondary", light=light),
        "prompt-session": scheme.get("secondary", light=light),
        "prompt-separator": scheme.get("text", light=light),
        "prompt-input": scheme.get("text", light=light),
        "prompt-suggestion": scheme.get("muted", light=light),
        "prompt-continuation": scheme.get("muted", light=light),
        "prompt-banner-border": scheme.get("primary", light=light),
        "prompt-banner-title": scheme.get("primary", light=light),
        "prompt-banner-text": scheme.get("text", light=light),
        "status-success": scheme.get("success", light=light),
        "status-warning": scheme.get("warning", light=light),
        "status-error": scheme.get("error", light=light),
        "status-info": scheme.get("info", light=light),
        "status-muted": scheme.get("muted", light=light),
        "completion-menu": scheme.get("text", light=light),
        "completion-menu.completion.current": scheme.get("highlight", light=light),
        "completion-menu.meta.completion.current": scheme.get("muted", light=light),
        "scrollbar.background": scheme.get("muted", light=light),
        "scrollbar.button": scheme.get("primary", light=light),
    }



# ─── Syntax Highlighting Rules ───────────────────────────────────────────────


def generate_syntax_highlight_rules(
    scheme: ColorScheme,
    light: bool = False,
) -> dict[str, str]:
    """Generate syntax highlighting rules from a ColorScheme.

    Creates a mapping of Pygments token types to style strings
    that can be used with Rich's Syntax or Pygments directly.

    Args:
        scheme: The ColorScheme to derive colors from.
        light: Whether to use light-mode colors.

    Returns:
        Dictionary mapping token type names to style strings.
    """
    return {
        "Token": scheme.get("text", light=light),
        "Token.Keyword": scheme.get("secondary", light=light),
        "Token.Keyword.Constant": scheme.get("accent", light=light),
        "Token.Keyword.Declaration": scheme.get("secondary", light=light),
        "Token.Keyword.Namespace": scheme.get("secondary", light=light),
        "Token.Keyword.Pseudo": scheme.get("muted", light=light),
        "Token.Keyword.Reserved": scheme.get("secondary", light=light),
        "Token.Keyword.Type": scheme.get("info", light=light),
        "Token.Name": scheme.get("text", light=light),
        "Token.Name.Attribute": scheme.get("info", light=light),
        "Token.Name.Builtin": scheme.get("info", light=light),
        "Token.Name.Builtin.Pseudo": scheme.get("muted", light=light),
        "Token.Name.Class": scheme.get("accent", light=light),
        "Token.Name.Constant": scheme.get("accent", light=light),
        "Token.Name.Decorator": scheme.get("warning", light=light),
        "Token.Name.Entity": scheme.get("info", light=light),
        "Token.Name.Exception": scheme.get("error", light=light),
        "Token.Name.Function": scheme.get("primary", light=light),
        "Token.Name.Function.Magic": scheme.get("primary", light=light),
        "Token.Name.Label": scheme.get("muted", light=light),
        "Token.Name.Namespace": scheme.get("text", light=light),
        "Token.Name.Other": scheme.get("text", light=light),
        "Token.Name.Tag": scheme.get("secondary", light=light),
        "Token.Name.Variable": scheme.get("text", light=light),
        "Token.Name.Variable.Class": scheme.get("text", light=light),
        "Token.Name.Variable.Global": scheme.get("text", light=light),
        "Token.Name.Variable.Instance": scheme.get("text", light=light),
        "Token.Name.Variable.Magic": scheme.get("accent", light=light),
        "Token.Literal": scheme.get("success", light=light),
        "Token.Literal.Date": scheme.get("success", light=light),
        "Token.String": scheme.get("success", light=light),
        "Token.String.Affix": scheme.get("success", light=light),
        "Token.String.Backtick": scheme.get("success", light=light),
        "Token.String.Char": scheme.get("success", light=light),
        "Token.String.Delimiter": scheme.get("success", light=light),
        "Token.String.Doc": scheme.get("muted", light=light),
        "Token.String.Double": scheme.get("success", light=light),
        "Token.String.Escape": scheme.get("warning", light=light),
        "Token.String.Heredoc": scheme.get("success", light=light),
        "Token.String.Interpol": scheme.get("warning", light=light),
        "Token.String.Other": scheme.get("success", light=light),
        "Token.String.Regex": scheme.get("warning", light=light),
        "Token.String.Single": scheme.get("success", light=light),
        "Token.String.Symbol": scheme.get("accent", light=light),
        "Token.Number": scheme.get("warning", light=light),
        "Token.Number.Bin": scheme.get("warning", light=light),
        "Token.Number.Float": scheme.get("warning", light=light),
        "Token.Number.Hex": scheme.get("warning", light=light),
        "Token.Number.Integer": scheme.get("warning", light=light),
        "Token.Number.Integer.Long": scheme.get("warning", light=light),
        "Token.Number.Oct": scheme.get("warning", light=light),
        "Token.Operator": scheme.get("secondary", light=light),
        "Token.Operator.Word": scheme.get("secondary", light=light),
        "Token.Punctuation": scheme.get("text", light=light),
        "Token.Comment": scheme.get("muted", light=light),
        "Token.Comment.Hashbang": scheme.get("muted", light=light),
        "Token.Comment.Multiline": scheme.get("muted", light=light),
        "Token.Comment.Preproc": scheme.get("warning", light=light),
        "Token.Comment.PreprocFile": scheme.get("warning", light=light),
        "Token.Comment.Single": scheme.get("muted", light=light),
        "Token.Comment.Special": scheme.get("muted", light=light),
        "Token.Generic": scheme.get("text", light=light),
        "Token.Generic.Deleted": scheme.get("error", light=light),
        "Token.Generic.Emph": scheme.get("text", light=light),
        "Token.Generic.Error": scheme.get("error", light=light),
        "Token.Generic.Heading": scheme.get("primary", light=light),
        "Token.Generic.Inserted": scheme.get("success", light=light),
        "Token.Generic.Output": scheme.get("info", light=light),
        "Token.Generic.Prompt": scheme.get("primary", light=light),
        "Token.Generic.Strong": scheme.get("text", light=light),
        "Token.Generic.Subheading": scheme.get("secondary", light=light),
        "Token.Generic.Traceback": scheme.get("error", light=light),
        "Token.Whitespace": scheme.get("text", light=light),
        "Token.Error": scheme.get("error", light=light),
    }



