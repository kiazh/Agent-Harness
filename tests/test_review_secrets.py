"""Mounted secret paths must resolve to files."""

import pytest

from ah.security.secrets import _read_file


def test_secret_reader_rejects_directory(tmp_path):
    with pytest.raises(ValueError, match="file"):
        _read_file(str(tmp_path))
