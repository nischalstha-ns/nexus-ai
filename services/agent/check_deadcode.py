"""Whole-program dead-code check for the NEXUS agent (heuristic).

Reports unused imports and definitions never referenced by name or
attribute anywhere in nexus/. Every finding needs human review.
"""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "nexus")
ENTRY_POINTS = {"serve", "main", "backup", "restore", "list_backups"}


def main():
    files = {}
    for dirpath, _, filenames in os.walk(ROOT):
        for fn in filenames:
            if fn.endswith(".py"):
                p = os.path.join(dirpath, fn)
                with open(p) as f:
                    files[p] = ast.parse(f.read(), p)

    name_refs, attr_refs = set(), set()
    imported = {}
    defined = []

    for path, tree in files.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name_refs.add(node.id)
            elif isinstance(node, ast.Attribute):
                attr_refs.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    full = (f"{node.module}.{a.name}"
                            if isinstance(node, ast.ImportFrom) else a.name)
                    imported.setdefault(path, {})[local] = (node.lineno, full)

    def walk(node, klass=None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                defined.append((child.lineno, "class", child.name, child.name))
                walk(child, klass=child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if klass else "function"
                qual = f"{klass}.{child.name}" if klass else child.name
                defined.append((child.lineno, kind, qual, child.name))
                walk(child, klass=klass or child.name)
            else:
                walk(child, klass=klass)

    print("=== UNUSED IMPORTS ===")
    for path in sorted(files):
        tree = files[path]
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        # attribute roots: `import x` + `x.y()` counts via Name load of x
        for local, (lineno, full) in sorted(imported.get(path, {}).items()):
            if local not in used and local != "*":
                print(f"{os.path.relpath(path)}:{lineno}: {full} as '{local}'")

    print("=== POSSIBLY UNUSED DEFINITIONS ===")
    for path in sorted(files):
        defined.clear()
        walk(files[path])
        for lineno, kind, qual, name in defined:
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in ENTRY_POINTS:
                continue
            if kind == "method":
                if name in attr_refs:
                    continue
            elif name in name_refs or name in attr_refs:
                continue
            print(f"{os.path.relpath(path)}:{lineno}: {kind} {qual}")


if __name__ == "__main__":
    main()
