import ast
import sys
from pathlib import Path

class BranchVisitor(ast.NodeVisitor):
    def __init__(self):
        self.functions = []
        self.current_fn = None
        self.current_depth = 0
        self.max_depth = 0
        self.complexity = 1
        self.if_nodes = []

    def visit_FunctionDef(self, node):
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_fn(node)

    def _visit_fn(self, node):
        old_fn = self.current_fn
        old_depth = self.current_depth
        old_complexity = self.complexity
        old_ifs = self.if_nodes

        self.current_fn = node.name
        self.current_depth = 0
        self.max_depth = 0
        self.complexity = 1
        self.if_nodes = []

        self.generic_visit(node)

        self.functions.append({
            "name": self.current_fn,
            "line": node.lineno,
            "complexity": self.complexity,
            "max_depth": self.max_depth,
            "if_count": len(self.if_nodes)
        })

        self.current_fn = old_fn
        self.current_depth = old_depth
        self.complexity = old_complexity
        self.if_nodes = old_ifs

    def visit_If(self, node):
        if self.current_fn:
            self.complexity += 1
            self.if_nodes.append(node)
            self.current_depth += 1
            if self.current_depth > self.max_depth:
                self.max_depth = self.current_depth
            self.generic_visit(node)
            self.current_depth -= 1
        else:
            self.generic_visit(node)

    def visit_For(self, node):
        self._visit_loop(node)

    def visit_While(self, node):
        self._visit_loop(node)

    def _visit_loop(self, node):
        if self.current_fn:
            self.complexity += 1
            self.current_depth += 1
            if self.current_depth > self.max_depth:
                self.max_depth = self.current_depth
            self.generic_visit(node)
            self.current_depth -= 1
        else:
            self.generic_visit(node)

    def visit_BoolOp(self, node):
        if self.current_fn:
            self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def visit_IfExp(self, node):
        if self.current_fn:
            self.complexity += 1
        self.generic_visit(node)

def analyze_file(file_path: Path):
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))
    visitor = BranchVisitor()
    visitor.visit(tree)
    
    fns = sorted(visitor.functions, key=lambda x: x["complexity"], reverse=True)
    print(f"Analysis for {file_path}")
    print(f"{'Line':<8} {'Complexity':<12} {'Max Depth':<11} {'If Count':<10} {'Function'}")
    print("=" * 70)
    for fn in fns[:20]:
        print(f"{fn['line']:<8} {fn['complexity']:<12} {fn['max_depth']:<11} {fn['if_count']:<10} {fn['name']}")

if __name__ == "__main__":
    p = Path(sys.argv[1] if len(sys.argv) > 1 else "python/src/adeu/redline/engine.py")
    analyze_file(p)
