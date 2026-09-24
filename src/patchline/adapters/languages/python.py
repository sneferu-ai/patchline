"""Python language pack (FR-044): ast.parse with full error location."""
from __future__ import annotations

import ast
from typing import List

from .base import SyntaxResult


class PythonPack:
    def language_name(self) -> str:
        return "python"

    def file_extensions(self) -> List[str]:
        return [".py", ".pyw"]

    def parse_syntax(self, source: str) -> SyntaxResult:
        """Syntax ONLY. 'valid' does not guarantee correctness (FR-017)."""
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return SyntaxResult(
                valid=False,
                error=f"{exc.msg} (line {exc.lineno}, column {exc.offset})"
                if exc.lineno is not None
                else str(exc),
                lineno=exc.lineno,
                offset=exc.offset,
                text=(exc.text.rstrip("\n") if isinstance(exc.text, str) else None),
            )
        except (ValueError, TypeError) as exc:
            return SyntaxResult(valid=False, error=str(exc))
        return SyntaxResult(valid=True)
