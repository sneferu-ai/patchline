"""Installed language packs (CP-6/FR-026/FR-044).

Only the Python pack ships for the MVP pilot (A13); the protocol supports
future packs without product changes.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import LanguagePack, SyntaxResult
from .python import PythonPack

_PACKS: Dict[str, LanguagePack] = {}


def installed_packs() -> Dict[str, LanguagePack]:
    if not _PACKS:
        _PACKS["python"] = PythonPack()
    return dict(_PACKS)


def installed_pack_names() -> List[str]:
    return sorted(installed_packs().keys())


def get_pack(name: str) -> Optional[LanguagePack]:
    if not name:
        return None
    return installed_packs().get(name.strip().lower())


def is_installed(name: str) -> bool:
    return get_pack(name) is not None


__all__ = [
    "LanguagePack",
    "SyntaxResult",
    "PythonPack",
    "installed_packs",
    "installed_pack_names",
    "get_pack",
    "is_installed",
]
