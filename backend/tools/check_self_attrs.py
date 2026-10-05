"""Report `self.CONST` reads that resolve against no class, base or module scope.

The bug this catches only fires on rare paths, so it survives casual reading:
a dataclass reading a constant that lives on its *owner* class.  Only
uppercase names are checked, because those are the shared constants where the
mistake actually happens; instance state typos show up as obvious NameErrors.
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys


def assigned_names(node: ast.AST) -> set[str]:
    """Every name a statement binds at its own level."""
    out: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        out.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            out |= bound_names(target)
    elif isinstance(node, ast.AnnAssign):
        out |= bound_names(node.target)
    elif isinstance(node, ast.AugAssign):
        out |= bound_names(node.target)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            out.add((alias.asname or alias.name).split(".")[0])
    return out


def bound_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in target.elts:
            out |= bound_names(elt)
        return out
    return set()


def arg_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    names = {arg.arg for arg in a.posonlyargs + a.args + a.kwonlyargs}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def main(root: pathlib.Path) -> int:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))

        module_names: set[str] = set()
        for node in tree.body:
            module_names |= assigned_names(node)

        # class name -> (own attribute names, base names, lineno)
        classes: dict[str, tuple[set[str], set[str], int]] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            own: set[str] = set()
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    own.add(item.name)
                else:
                    own |= assigned_names(item)
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            bases |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
            classes[node.name] = (own, bases, node.lineno)

        def resolve(class_name: str, seen: set[str] | None = None) -> set[str]:
            """Attributes visible through a class, its bases and the module."""
            seen = seen or set()
            if class_name in seen or class_name not in classes:
                return set()
            seen.add(class_name)
            own, bases, _ = classes[class_name]
            visible = set(own) | module_names
            for base in bases:
                visible |= resolve(base, seen)
            return visible

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            visible = resolve(node.name) | set(dir(builtins)) | {"self", "cls"}
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Names bound inside the method shadow the class scope.
                local = visible | arg_names(item)
                for sub in ast.walk(item):
                    if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                        local |= bound_names(sub.targets[0] if isinstance(sub, ast.Assign) else sub.target)
                    elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        local |= assigned_names(sub) | arg_names(sub)
                for sub in ast.walk(item):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id in ("self", "cls")
                            and isinstance(sub.ctx, ast.Load)
                            and not sub.attr.islower()
                            and sub.attr not in local):
                        problems.append(
                            f"{path}:{sub.lineno}: "
                            f"{node.name}.{item.name}() reads {sub.value.id}.{sub.attr}"
                        )
    for line in sorted(set(problems)):
        print(line)
    print(f"\n{len(set(problems))} unresolved constant read(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app")))