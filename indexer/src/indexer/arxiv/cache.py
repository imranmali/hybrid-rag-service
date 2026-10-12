"""Filesystem cache for arXiv API and version-pinned HTML responses."""

import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class ArxivCache:
    """Atomic cache with explicit negative HTML entries."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "api").mkdir(parents=True, exist_ok=True)
        (root / "html").mkdir(parents=True, exist_ok=True)

    def api_path(self, url: str) -> Path:
        return self.root / "api" / f"{hashlib.sha1(url.encode()).hexdigest()}.xml"

    def html_path(self, external_id: str, version: int) -> Path:
        return self.root / "html" / f"{external_id}v{version}.html"

    def missing_path(self, external_id: str, version: int) -> Path:
        return self.root / "html" / f"{external_id}v{version}.missing"

    def read_api(self, url: str) -> str | None:
        return self._read_text(self.api_path(url))

    def write_api(self, url: str, content: str) -> None:
        self._atomic_write(self.api_path(url), content)

    def read_html(self, external_id: str, version: int) -> str | None:
        return self._read_text(self.html_path(external_id, version))

    def html_is_missing(self, external_id: str, version: int) -> bool:
        return self.missing_path(external_id, version).exists()

    def write_html(self, external_id: str, version: int, content: str) -> None:
        path = self.html_path(external_id, version)
        self._atomic_write(path, content)
        self.missing_path(external_id, version).unlink(missing_ok=True)

    def mark_missing(self, external_id: str, version: int) -> None:
        self._atomic_write(self.missing_path(external_id, version), "")

    @staticmethod
    def _read_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
