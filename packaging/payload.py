"""Keep development documents out of installers and preserve license notices."""

import hashlib
from pathlib import Path


def digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def remove_documentation(root):
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".md", ".rst"}:
            continue
        if path.is_symlink():
            path.unlink()
        elif path.is_file():
            if "licenses" in path.parts or path.name.upper().startswith(("LICENSE.", "COPYING.", "NOTICE.")):
                path.rename(path.with_suffix(".txt"))
            else:
                path.unlink()


def check_clean_payload(root):
    extra = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.suffix.lower() in {".md", ".rst"}
        or path.name.upper() in {"BUILD-MANIFEST.JSON", "SOURCE-MANIFEST.JSON"}
    ]
    if extra:
        raise ValueError("Unexpected documentation or build metadata in payload: " + ", ".join(extra))
