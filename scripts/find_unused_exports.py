import ast
import re
from pathlib import Path

def find_python_unused():
    py_files = list(Path("python/src").rglob("*.py"))
    
    # Collect all function definitions
    defs = {}
    for p in py_files:
        content = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = node.name
                    # Only analyze private helpers (starting with _) or internal utility functions
                    if name.startswith("_") and not name.startswith("__"):
                        defs[name] = defs.get(name, 0)
        except Exception:
            pass

    # Count usages across all python files
    for p in py_files:
        content = p.read_text(encoding="utf-8")
        for name in list(defs.keys()):
            # Count occurrences of exact word boundary
            matches = len(re.findall(r'\b' + re.escape(name) + r'\b', content))
            defs[name] += matches

    print("=== Unused or Single-Occurrence Private Python Helpers ===")
    for name, count in sorted(defs.items(), key=lambda x: x[1]):
        # Def itself is 1 occurrence
        if count <= 1:
            print(f"  {name}: {count} total occurrence(s) (Dead function candidate)")

if __name__ == "__main__":
    find_python_unused()
