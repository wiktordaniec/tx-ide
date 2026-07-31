"""Static Python call-graph extraction over a set of buffered files.

Given the files currently buffered in one nvim session, this module parses each
Python file with the stdlib ``ast`` and resolves the *cross-file* relationships
between the symbols they define — not just "file A imports file B" but which
function or method of A reaches which function, class, or method of B:

- ``calls``        — a function/method body calls a function or method defined
                     in another buffered file (directly, through a module alias,
                     through a typed parameter/variable, or through ``self.attr``
                     whose type is known).
- ``instantiates`` — a function/method body constructs a class from another
                     buffered file.
- ``has_instance`` — a class holds an attribute whose type is a class from
                     another buffered file (constructor-injected or annotated).
- ``inherits``     — a class subclasses a class from another buffered file.

Resolution is intentionally static and conservative: an edge is only emitted
when the name resolves through the file's imports (or through a local binding
whose type traces back to such an import) to a symbol in another *buffered*
file. Files outside the buffer set never produce nodes or edges.
"""

import ast
from pathlib import Path


def module_name_for(relative_path):
    """Dotted module name for a repo-relative ``.py`` path, or None."""
    parts = list(Path(relative_path).parts)
    if not parts or not parts[-1].endswith(".py"):
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    if not parts:
        return None
    return ".".join(parts)


def annotation_class_name(node):
    """The dotted name a type annotation refers to, or None.

    Handles ``Foo``, ``pkg.Foo``, string annotations ``"Foo"``, and unwraps a
    single ``Optional[Foo]`` / ``Foo | None`` level.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            name = annotation_class_name(side)
            if name is not None:
                return name
        return None
    if isinstance(node, ast.Subscript):
        value = annotation_class_name(node.value)
        if value in ("Optional", "typing.Optional"):
            return annotation_class_name(node.slice)
        return None
    return dotted_name(node)


def dotted_name(node):
    """Flatten a Name / Attribute chain to ``a.b.c``, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


class FileInfo:
    """Parsed shape of one buffered Python file."""

    def __init__(self, absolute_path, relative_path):
        self.absolute_path = absolute_path
        self.relative_path = relative_path
        self.module = module_name_for(relative_path)
        self.error = None
        self.tree = None
        # symbol id -> {"kind", "line", "name"}; methods are "Class.method"
        self.symbols = {}
        # class name -> {"methods": {name: line}, "attr_types": {attr: local class ref},
        #               "bases": [dotted], "line": int}
        self.classes = {}
        # local binding name -> ("module", dotted) for import aliases
        self.module_aliases = {}
        # local binding name -> (module, symbol) for from-imports
        self.from_imports = {}

    def parse(self):
        try:
            source = Path(self.absolute_path).read_text(encoding="utf-8", errors="replace")
            self.tree = ast.parse(source)
        except (OSError, SyntaxError) as error:
            self.error = f"{type(error).__name__}: {error}"
            return
        self._collect_imports()
        self._collect_symbols()

    def _collect_imports(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    self.module_aliases[bound] = target
            elif isinstance(node, ast.ImportFrom):
                base = self._resolve_import_base(node)
                if base is None:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name
                    self.from_imports[bound] = (base, alias.name)

    def _resolve_import_base(self, node):
        if node.level == 0:
            return node.module
        if self.module is None:
            return None
        parts = self.module.split(".")
        # For a plain module, level 1 is its own package (drop the module
        # segment); for __init__.py the module name *is* the package, so level 1
        # drops nothing.
        drop = node.level - 1 if self.absolute_path.name == "__init__.py" else node.level
        base_parts = parts[:-drop] if 0 < drop < len(parts) else (parts if drop == 0 else [])
        if node.module:
            base_parts = base_parts + node.module.split(".")
        return ".".join(base_parts) if base_parts else None

    def _collect_symbols(self):
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.symbols[node.name] = {
                    "kind": "function",
                    "name": node.name,
                    "line": node.lineno,
                }
            elif isinstance(node, ast.ClassDef):
                self.symbols[node.name] = {
                    "kind": "class",
                    "name": node.name,
                    "line": node.lineno,
                }
                methods = {}
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[child.name] = child.lineno
                        self.symbols[f"{node.name}.{child.name}"] = {
                            "kind": "method",
                            "name": child.name,
                            "line": child.lineno,
                        }
                self.classes[node.name] = {
                    "methods": methods,
                    "attr_types": {},
                    "bases": [dotted for dotted in map(dotted_name, node.bases) if dotted],
                    "line": node.lineno,
                }


class Analyzer:
    """Cross-file symbol resolution over a set of FileInfo."""

    def __init__(self, file_infos):
        self.files = file_infos
        self.by_module = {
            info.module: info for info in file_infos if info.module and not info.error
        }
        self.edges = {}

    def run(self):
        parsed = [info for info in self.files if not info.error and info.tree]
        for info in parsed:
            self._collect_attr_types(info)
        for info in parsed:
            self._emit_inherits(info)
            self._emit_has_instance(info)
            self._emit_body_edges(info)
        return sorted(self.edges.values(), key=lambda edge: (
            edge["from_file"], edge["from_symbol"], edge["to_file"], edge["to_symbol"],
            edge["kind"],
        ))

    # -- name resolution ---------------------------------------------------

    def resolve_name(self, info, name):
        """Resolve a bound name to ("class"|"function"|"module", target...) or None.

        Targets point into *other buffered files only* — same-file symbols and
        unresolvable names return None.
        """
        if name in info.from_imports:
            module, symbol = info.from_imports[name]
            target = self.by_module.get(module)
            if target is not None and target is not info:
                entry = target.symbols.get(symbol)
                if entry is not None:
                    return (entry["kind"], target, symbol)
            # ``from pkg import submodule`` — the bound name may be a module.
            submodule = self.by_module.get(f"{module}.{symbol}")
            if submodule is not None and submodule is not info:
                return ("module", submodule)
            return None
        if name in info.module_aliases:
            target = self.by_module.get(info.module_aliases[name])
            if target is not None and target is not info:
                return ("module", target)
        return None

    def resolve_dotted(self, info, dotted):
        """Resolve ``a.b.c`` where ``a`` is an import alias to a symbol access.

        Returns ("class"|"function", target_info, symbol, trailing_attr|None).
        """
        parts = dotted.split(".")
        head = parts[0]
        if head in info.from_imports:
            resolved = self.resolve_name(info, head)
            if resolved is None:
                return None
            kind = resolved[0]
            if kind == "class" and len(parts) >= 2:
                return ("class", resolved[1], resolved[2], parts[1])
            if kind == "module":
                return self._resolve_in_module(resolved[1], parts[1:])
            return None
        if head in info.module_aliases:
            alias_target = info.module_aliases[head]
            # Longest dotted prefix that names a buffered module wins, so both
            # ``import a.b`` + ``a.b.func()`` and ``import a.b as m`` + ``m.func()``
            # resolve.
            full = ".".join([alias_target] + parts[1:])
            full_parts = full.split(".")
            for cut in range(len(full_parts) - 1, 0, -1):
                module = ".".join(full_parts[:cut])
                target = self.by_module.get(module)
                if target is not None and target is not info:
                    return self._resolve_in_module(target, full_parts[cut:])
        return None

    def _resolve_in_module(self, target, parts):
        if not parts:
            return None
        entry = target.symbols.get(parts[0])
        if entry is None:
            return None
        trailing = parts[1] if len(parts) > 1 else None
        return (entry["kind"], target, parts[0], trailing)

    def resolve_class_ref(self, info, dotted):
        """Resolve a dotted class reference to (target_info, class_name) or None."""
        if dotted is None:
            return None
        if "." not in dotted:
            resolved = self.resolve_name(info, dotted)
            if resolved and resolved[0] == "class":
                return (resolved[1], resolved[2])
            return None
        resolved = self.resolve_dotted(info, dotted)
        if resolved and resolved[0] == "class":
            return (resolved[1], resolved[2])
        return None

    # -- edge collection ---------------------------------------------------

    def add_edge(self, from_info, from_symbol, to_info, to_symbol, kind, line):
        key = (from_info.relative_path, from_symbol, to_info.relative_path, to_symbol, kind)
        edge = self.edges.get(key)
        if edge is None:
            self.edges[key] = {
                "from_file": from_info.relative_path,
                "from_symbol": from_symbol,
                "to_file": to_info.relative_path,
                "to_symbol": to_symbol,
                "kind": kind,
                "line": line,
                "count": 1,
            }
        else:
            edge["count"] += 1
            edge["line"] = min(edge["line"], line)

    def _emit_inherits(self, info):
        for class_name, class_info in info.classes.items():
            for base in class_info["bases"]:
                resolved = self.resolve_class_ref(info, base)
                if resolved is not None:
                    target, target_class = resolved
                    self.add_edge(info, class_name, target, target_class,
                                  "inherits", class_info["line"])

    def _collect_attr_types(self, info):
        """Fill each class's attr_types: self.attr -> (target_info, class_name)."""
        for node in info.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            class_info = info.classes[node.name]
            for child in node.body:
                # Dataclass-style annotated fields at class level.
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    resolved = self.resolve_class_ref(
                        info, annotation_class_name(child.annotation))
                    if resolved is not None:
                        class_info["attr_types"][child.target.id] = resolved
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                param_types = self._param_types(info, child)
                for statement in ast.walk(child):
                    target_attr = self._self_attr_target(statement)
                    if target_attr is None:
                        continue
                    resolved = self._infer_value_class(
                        info, statement, param_types)
                    if resolved is not None:
                        class_info["attr_types"].setdefault(target_attr, resolved)

    def _self_attr_target(self, statement):
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
        else:
            return None
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "self"):
            return target.attr
        return None

    def _infer_value_class(self, info, statement, param_types):
        if isinstance(statement, ast.AnnAssign):
            resolved = self.resolve_class_ref(
                info, annotation_class_name(statement.annotation))
            if resolved is not None:
                return resolved
            value = statement.value
        else:
            value = statement.value
        if isinstance(value, ast.Call):
            resolved = self.resolve_class_ref(info, dotted_name(value.func))
            if resolved is not None:
                return resolved
        if isinstance(value, ast.Name) and value.id in param_types:
            return param_types[value.id]
        return None

    def _param_types(self, info, function_node):
        types = {}
        arguments = function_node.args
        for argument in (arguments.posonlyargs + arguments.args + arguments.kwonlyargs):
            resolved = self.resolve_class_ref(
                info, annotation_class_name(argument.annotation))
            if resolved is not None:
                types[argument.arg] = resolved
        return types

    def _emit_has_instance(self, info):
        for class_name, class_info in info.classes.items():
            for target, target_class in class_info["attr_types"].values():
                self.add_edge(info, class_name, target, target_class,
                              "has_instance", class_info["line"])

    def _emit_body_edges(self, info):
        for node in info.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk_body(info, node.name, node, owner_class=None)
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._walk_body(info, f"{node.name}.{child.name}", child,
                                        owner_class=info.classes[node.name])

    def _walk_body(self, info, symbol, function_node, owner_class):
        local_types = self._param_types(info, function_node)
        for decorator in function_node.decorator_list:
            self._emit_call(info, symbol, decorator, local_types, owner_class,
                            getattr(decorator, "lineno", function_node.lineno))
        for statement in ast.walk(function_node):
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                self._track_local(info, statement, local_types)
            if isinstance(statement, ast.Call):
                self._emit_call(info, symbol, statement.func, local_types,
                                owner_class, statement.lineno)

    def _track_local(self, info, statement, local_types):
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            resolved = self.resolve_class_ref(
                info, annotation_class_name(statement.annotation))
            if resolved is not None:
                local_types[statement.target.id] = resolved
                return
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        value = statement.value
        if not (isinstance(value, ast.Call) and len(targets) == 1
                and isinstance(targets[0], ast.Name)):
            return
        resolved = self.resolve_class_ref(info, dotted_name(value.func))
        if resolved is not None:
            local_types[targets[0].id] = resolved

    def _emit_call(self, info, symbol, func_expr, local_types, owner_class, line):
        # Bare name: imported function call or class instantiation.
        if isinstance(func_expr, ast.Name):
            resolved = self.resolve_name(info, func_expr.id)
            if resolved is None:
                return
            kind, target, target_symbol = resolved[0], resolved[1], resolved[2]
            if kind == "function":
                self.add_edge(info, symbol, target, target_symbol, "calls", line)
            elif kind == "class":
                self.add_edge(info, symbol, target, target_symbol, "instantiates", line)
            return
        if not isinstance(func_expr, ast.Attribute):
            return
        method = func_expr.attr
        receiver = func_expr.value
        # self.attr.method() through a known attribute type.
        if (isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name)
                and receiver.value.id == "self" and owner_class is not None):
            attr_type = owner_class["attr_types"].get(receiver.attr)
            if attr_type is not None:
                self._add_method_edge(info, symbol, attr_type, method, line)
            return
        # variable.method() through a locally typed variable.
        if isinstance(receiver, ast.Name):
            if receiver.id in local_types:
                self._add_method_edge(info, symbol, local_types[receiver.id], method, line)
                return
            resolved = self.resolve_name(info, receiver.id)
            if resolved is not None and resolved[0] == "class":
                # Class-level access: staticmethod / classmethod call.
                self._add_method_edge(info, symbol, (resolved[1], resolved[2]), method, line)
                return
        # Module-qualified chains: alias.func(), alias.Class.method(), a.b.func().
        dotted = dotted_name(func_expr)
        if dotted is None:
            return
        resolved = self.resolve_dotted(info, dotted)
        if resolved is None:
            return
        kind, target, target_symbol, trailing = resolved
        if kind == "class" and trailing is not None:
            self._add_method_edge(info, symbol, (target, target_symbol), trailing, line)
        elif kind == "class":
            self.add_edge(info, symbol, target, target_symbol, "instantiates", line)
        elif kind == "function":
            self.add_edge(info, symbol, target, target_symbol, "calls", line)

    def _add_method_edge(self, info, symbol, class_ref, method, line):
        target, class_name = class_ref
        if method in target.classes.get(class_name, {}).get("methods", {}):
            to_symbol = f"{class_name}.{method}"
        else:
            # Method not defined on the class itself (inherited or dynamic):
            # anchor the edge on the class row.
            to_symbol = class_name
        self.add_edge(info, symbol, target, to_symbol, "calls", line)


def build_graph(buffer_entries, root):
    """Analyze buffered files and return the graph payload.

    ``buffer_entries`` — [{"path": str, "changed": int, "lastused": int}, ...]
    ``root`` — the nvim session's cwd; module names resolve relative to it.
    """
    root = Path(root)
    infos = []
    passthrough = []
    for entry in buffer_entries:
        absolute = Path(entry["path"])
        try:
            relative = str(absolute.relative_to(root))
        except ValueError:
            relative = absolute.name
        if absolute.suffix == ".py":
            info = FileInfo(absolute, relative)
            info.parse()
            infos.append((info, entry))
        else:
            passthrough.append((relative, entry))

    edges = Analyzer([info for info, _ in infos]).run()

    files = []
    for info, entry in infos:
        symbols = []
        for symbol_id, meta in info.symbols.items():
            if meta["kind"] == "method":
                continue
            record = dict(meta, id=symbol_id)
            if meta["kind"] == "class":
                record["children"] = [
                    {"id": f"{symbol_id}.{name}", "kind": "method",
                     "name": name, "line": line}
                    for name, line in sorted(
                        info.classes[symbol_id]["methods"].items(),
                        key=lambda item: item[1])
                ]
            symbols.append(record)
        symbols.sort(key=lambda record: record["line"])
        files.append({
            "path": str(info.absolute_path),
            "rel": info.relative_path,
            "module": info.module,
            "changed": entry.get("changed", 0),
            "lastused": entry.get("lastused", 0),
            "language": "python",
            "error": info.error,
            "symbols": symbols,
        })
    for relative, entry in passthrough:
        files.append({
            "path": entry["path"],
            "rel": relative,
            "module": None,
            "changed": entry.get("changed", 0),
            "lastused": entry.get("lastused", 0),
            "language": Path(entry["path"]).suffix.lstrip(".") or "?",
            "error": None,
            "symbols": [],
        })
    files.sort(key=lambda record: record["rel"])
    return {"files": files, "edges": edges}
