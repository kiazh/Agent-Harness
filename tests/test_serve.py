"""Tests for the `ah serve` CLI command."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ah.cli import app

runner = CliRunner()


class TestServeCommand:
    """Tests for the serve command registration and behavior."""

    def test_serve_registered(self):
        """The serve command is registered in the CLI."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "serve" in result.output

    def test_serve_help(self):
        """The serve command shows help with all options."""
        result = runner.invoke(app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output
        assert "--reload" in result.output

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_defaults(self, mock_create_app, mock_uvicorn_run):
        """Serve with default arguments calls uvicorn correctly."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve"])

        assert result.exit_code == 0
        mock_create_app.assert_called_once()
        mock_uvicorn_run.assert_called_once_with(
            "ah.api.app:create_app",
            host="127.0.0.1",
            port=8000,
            reload=False,
            factory=True,
        )
        assert "http://127.0.0.1:8000" in result.output

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_custom_host_port(self, mock_create_app, mock_uvicorn_run):
        """Serve with custom host and port."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "9000"])

        assert result.exit_code == 0
        mock_uvicorn_run.assert_called_once_with(
            "ah.api.app:create_app",
            host="0.0.0.0",
            port=9000,
            reload=False,
            factory=True,
        )
        assert "http://0.0.0.0:9000" in result.output

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_with_reload(self, mock_create_app, mock_uvicorn_run):
        """Serve with --reload flag."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve", "--reload"])

        assert result.exit_code == 0
        mock_uvicorn_run.assert_called_once_with(
            "ah.api.app:create_app",
            host="127.0.0.1",
            port=8000,
            reload=True,
            factory=True,
        )

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_short_flags(self, mock_create_app, mock_uvicorn_run):
        """Serve with short flags -h and -p."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve", "-h", "0.0.0.0", "-p", "5000"])

        assert result.exit_code == 0
        mock_uvicorn_run.assert_called_once_with(
            "ah.api.app:create_app",
            host="0.0.0.0",
            port=5000,
            reload=False,
            factory=True,
        )

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_create_app_failure(self, mock_create_app, mock_uvicorn_run):
        """Serve exits with error when create_app fails."""
        mock_create_app.side_effect = RuntimeError("DB connection failed")

        result = runner.invoke(app, ["serve"])

        assert result.exit_code == 1
        assert "Failed to start server" in result.output
        mock_uvicorn_run.assert_not_called()

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_keyboard_interrupt(self, mock_create_app, mock_uvicorn_run):
        """Serve handles KeyboardInterrupt gracefully."""
        mock_create_app.return_value = MagicMock()
        mock_uvicorn_run.side_effect = KeyboardInterrupt()

        result = runner.invoke(app, ["serve"])

        assert result.exit_code == 0
        assert "Server stopped" in result.output

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_shows_config(self, mock_create_app, mock_uvicorn_run):
        """Serve displays configuration before starting."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "3000"])

        assert result.exit_code == 0
        assert "0.0.0.0" in result.output
        assert "3000" in result.output
        assert "disabled" in result.output

    @patch("uvicorn.run")
    @patch("ah.api.app.create_app")
    def test_serve_shows_reload_enabled(self, mock_create_app, mock_uvicorn_run):
        """Serve shows reload status correctly."""
        mock_create_app.return_value = MagicMock()

        result = runner.invoke(app, ["serve", "--reload"])

        assert result.exit_code == 0
        assert "enabled" in result.output
