"""
Table styles for AgentHarness visual system.

Pre-configured table styles for different use cases.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableStyles:
    """Pre-configured table styles for different use cases."""

    # Box drawing style
    box_style: str = "ROUNDED"
    show_header: bool = True
    show_edge: bool = True
    show_lines: bool = False
    pad_edge: bool = True
    collapse_padding: bool = False

    # Colors
    header_style: str = ""
    border_style: str = ""
    row_styles: tuple[str, ...] = ("", "")
    title_justify: str = "left"

    # Compact mode
    compact: bool = False


TABLE_STYLES: dict[str, TableStyles] = {
    "standard": TableStyles(
        box_style="ROUNDED",
        show_header=True,
        show_edge=True,
        show_lines=False,
        header_style="primary bold",
        border_style="muted",
        row_styles=("", "dim"),
    ),
    "compact": TableStyles(
        box_style="SIMPLE",
        show_header=True,
        show_edge=False,
        show_lines=False,
        header_style="primary",
        border_style="",
        row_styles=("", ""),
        compact=True,
    ),
    "status": TableStyles(
        box_style="HEAVY_HEAD",
        show_header=True,
        show_edge=True,
        show_lines=True,
        header_style="primary bold",
        border_style="muted",
        row_styles=("", "dim"),
    ),
}
