"""FR-044 language pack tests (Python pack + registry)."""
from __future__ import annotations

from patchline.adapters.languages import get_pack, installed_pack_names, is_installed
from patchline.adapters.languages.python import PythonPack


class TestPythonPack:
    def test_valid_source(self):
        pack = PythonPack()
        result = pack.parse_syntax("x = 1\n")
        assert result.valid is True
        assert result.error is None

    def test_invalid_source_reports_location(self):
        pack = PythonPack()
        result = pack.parse_syntax("if True::\n    pass\n")
        assert result.valid is False
        assert result.lineno == 1
        assert result.offset is not None and result.offset >= 5
        assert result.text is not None and "if True::" in result.text
        assert "line 1" in (result.error or "")

    def test_invalid_deep_line(self):
        pack = PythonPack()
        result = pack.parse_syntax("x = 1\ny = 2\ndef broken(:\n")
        assert result.valid is False
        assert result.lineno == 3

    def test_identity(self):
        pack = PythonPack()
        assert pack.language_name() == "python"
        assert ".py" in pack.file_extensions()


class TestRegistry:
    def test_python_installed(self):
        assert is_installed("python")
        assert is_installed("Python")
        assert "python" in installed_pack_names()

    def test_go_not_installed_mvp(self):
        # A13: only the Python pack ships for the MVP pilot.
        assert not is_installed("go")
        assert get_pack("go") is None
