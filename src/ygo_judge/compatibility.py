"""Identify the immutable engine and card assets loaded by this server process.

Restart the server after changing these assets. Image updates naturally do this.
"""

import hashlib
from functools import cache
from pathlib import Path

from .cards import CDB_PATTERN
from .engine import LIBRARY_PATH


@cache
def simulation_fingerprint(data_dir: Path, library: Path = LIBRARY_PATH) -> str:
    digest = hashlib.sha256()
    files = [("engine", library), ("strings.conf", data_dir / "strings.conf")]
    files += [(f"BabelCDB/{p.name}", p) for p in sorted((data_dir / "BabelCDB").glob("*.cdb")) if CDB_PATTERN.match(p.name)]
    files += [(f"CardScripts/{p.relative_to(data_dir / 'CardScripts')}", p) for p in sorted((data_dir / "CardScripts").rglob("*.lua"))]
    for name, path in files:
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
