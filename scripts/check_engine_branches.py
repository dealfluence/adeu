import ast
import json
import subprocess
import sys
from pathlib import Path

def analyze_python(file_path: Path):
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))
    
    warnings = []
    
    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            self._check_fn(node)
            self.generic_visit(node)
            
        def visit_AsyncFunctionDef(self, node):
            self._check_fn(node)
            self.generic_visit(node)
            
        def _check_fn(self, node):
            # Check nested ifs for duplicate conditions
            def find_nested_ifs(parent, depth=1):
                for child in ast.iter_child_nodes(parent):
                    if isinstance(child, ast.If):
                        if depth >= 6:
                            warnings.append({
                                "line": child.lineno,
                                "type": "deep_nesting",
                                "msg": f"If statement nested {depth} levels deep in '{node.name}'"
                            })
                        # Check if child condition is identical to parent condition
                        if isinstance(parent, ast.If) and ast.dump(parent.test) == ast.dump(child.test):
                            warnings.append({
                                "line": child.lineno,
                                "type": "redundant_if",
                                "msg": f"Redundant nested if condition identical to parent in '{node.name}'"
                            })
                        find_nested_ifs(child, depth + 1)
                    else:
                        find_nested_ifs(child, depth)

            find_nested_ifs(node)

    Visitor().visit(tree)
    return warnings

def run_ts_analysis(file_path: Path):
    cmd = ["node", "scripts/analyze_ts_branches.mjs", str(file_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.stdout

def main():
    print("=== Engine Branching Analysis ===")
    
    py_engine = Path("python/src/adeu/redline/engine.py")
    if py_engine.exists():
        py_warns = analyze_python(py_engine)
        print(f"\n[Python Engine: {py_engine}]")
        print(f"Deep/Redundant Branch Warnings: {len(py_warns)}")
        for w in py_warns[:15]:
            print(f"  Line {w['line']:<5} [{w['type']}]: {w['msg']}")
            
    ts_engine = Path("node/packages/core/src/engine.ts")
    if ts_engine.exists():
        print(f"\n[TypeScript Engine: {ts_engine}]")
        ts_out = run_ts_analysis(ts_engine)
        print("\n".join(ts_out.splitlines()[:25]))

if __name__ == "__main__":
    main()
