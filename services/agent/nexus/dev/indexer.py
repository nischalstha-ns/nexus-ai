"""Project indexing: walk a jailed root, record files + code symbols.

Symbols come from stdlib `ast` for Python; other text files are indexed
by path only. The index answers "where is X defined?" without re-reading
every file each turn.
"""
from __future__ import annotations

import ast
import json
import os

from ..core.logging import get_logger
from ..tools.builtin import _resolve

log = get_logger("dev.indexer")

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             "dist", "build", ".pytest_cache", "target"}
TEXT_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
             ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php", ".md",
             ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".css"}


def python_symbols(source: str) -> list[dict]:
    """Top-level functions/classes (+methods) from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append({"kind": "function", "name": node.name,
                        "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            out.append({"kind": "class", "name": node.name,
                        "line": node.lineno})
    return out


class ProjectIndexer:
    def __init__(self, conn, approved_roots: list[str]):
        self.conn = conn
        self.roots = approved_roots

    def index(self, root: str, max_files: int = 5000) -> dict:
        """(Re)index a project root. Returns {files, symbols, truncated}."""
        abs_root = _resolve(self.roots, root)
        if not os.path.isdir(abs_root):
            from ..core.errors import NexusError
            raise NexusError(f"not a directory: {root}")
        self.conn.execute("DELETE FROM project_index WHERE root=?", (abs_root,))
        files, symbols, truncated = 0, 0, False
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if files >= max_files:
                    truncated = True
                    break
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, abs_root)
                ext = os.path.splitext(fn)[1].lower()
                try:
                    st = os.stat(full)
                    if st.st_size > 2_000_000:
                        continue
                    syms, lang, lines = [], "", 0
                    if ext in TEXT_EXTS:
                        with open(full, encoding="utf-8",
                                  errors="replace") as f:
                            src = f.read(1_000_000)
                        lines = src.count("\n") + 1
                        if ext == ".py":
                            lang = "python"
                            syms = python_symbols(src)
                    self.conn.execute(
                        "INSERT INTO project_index(root, path, language, symbols,"
                        " lines, mtime) VALUES (?,?,?,?,?,?)",
                        (abs_root, rel, lang, json.dumps(syms), lines,
                         st.st_mtime))
                    files += 1
                    symbols += len(syms)
                except OSError:
                    continue
            if truncated:
                break
        self.conn.commit()
        log.info("indexed %s: %d files, %d symbols", abs_root, files, symbols)
        return {"root": abs_root, "files": files, "symbols": symbols,
                "truncated": truncated}

    def find_symbol(self, root: str, name: str, limit: int = 20) -> list[dict]:
        abs_root = _resolve(self.roots, root)
        rows = self.conn.execute(
            "SELECT path, symbols FROM project_index WHERE root=?",
            (abs_root,)).fetchall()
        out = []
        for r in rows:
            for s in json.loads(r["symbols"]):
                if name.lower() in s["name"].lower():
                    out.append({"path": r["path"], **s})
                    if len(out) >= limit:
                        return out
        return out

    def status(self, root: str) -> dict:
        abs_root = _resolve(self.roots, root)
        row = self.conn.execute(
            "SELECT COUNT(*) c, SUM(lines) lines, MAX(updated_at) updated"
            " FROM project_index WHERE root=?", (abs_root,)).fetchone()
        return {"root": abs_root, "files": row["c"] or 0,
                "lines": row["lines"] or 0, "updated_at": row["updated"]}
