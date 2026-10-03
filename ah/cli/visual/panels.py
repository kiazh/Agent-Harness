"""
Panel styles for AgentHarness visual system.

Pre-configured panel styles for different semantic uses.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelStyles:
    """Pre-configured panel styles for different semantic uses.

    Each style defines border color, border box style, padding, and title
    alignment for a specific panel type.
    """

    # Border style (Rich box constant)
    border_style: str = "rounded"
    padding: tuple[int, int] = (1, 2)
    title_align: str = "left"
    expand: bool = True

    # Semantic overrides
    border_color: str = ""
    title_color: str = ""
    background_color: str = ""


# Pre-built panel style presets
PANEL_STYLES: dict[str, PanelStyles] = {
    "info": PanelStyles(
        border_color="info",
        title_color="info",
    ),
    "success": PanelStyles(
        border_color="success",
        title_color="success",
    ),
    "warning": PanelStyles(
        border_color="warning",
        title_color="warning",
    ),
    "error": PanelStyles(
        border_color="error",
        title_color="error",
    ),
    "agent": PanelStyles(
        border_color="primary",
        title_color="primary",
        border_style="double",
    ),
    "tool": PanelStyles(
        border_color="secondary",
        title_color="secondary",
        border_style="rounded",
    ),
    "muted": PanelStyles(
        border_color="muted",
        title_color="muted",
        border_style="rounded",
    ),
}
