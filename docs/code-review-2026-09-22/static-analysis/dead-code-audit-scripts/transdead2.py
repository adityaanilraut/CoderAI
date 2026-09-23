"""Name-based transitive liveness over coderai/ (production only).

A def (top-level function/class, or method) becomes live when its name is referenced
from live code. Roots: module-level statements (excluding imports and __all__),
decorated defs, dunder methods, methods of classes with external bases (overrides).
Reports defs that are dead transitively but which vulture didn't flag (because the only
references come from other dead code).
"""
import ast, os, re, json, sys
ROOT = "/Users/aditya/Desktop/CoderAI-main"
os.chdir(ROOT)
files = []
for dp, dn, fn in os.walk("coderai"):
    for f in fn:
        if f.endswith(".py") and "/skills/" not in dp:
            files.append(os.path.join(dp, f))
EXCLUDE_FILES = set(sys.argv[1:])  # treat these files as non-roots (still analyzed)

LOCAL_CLASSES=set()
for _f in files:
    for _n in ast.walk(ast.parse(open(_f).read())):
        if isinstance(_n, ast.ClassDef): LOCAL_CLASSES.add(_n.name)
defs = []  # (file, line, end, name, kind, refs:set)
root_refs = set()
text_blobs = []

def names_in(node):
    s = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            s.add(n.id)
        elif isinstance(n, ast.Attribute):
            s.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r"[A-Za-z_]\w*", n.value):
            s.add(n.value)
        elif isinstance(n, ast.keyword) and n.arg:
            s.add(n.arg)
    return s

for f in files:
    t = ast.parse(open(f).read())
    def visit_body(body, cls=None, cls_external=False):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(st, ast.ClassDef) else ("method" if cls else "function")
                deco_names = set()
                for d in st.decorator_list:
                    deco_names |= names_in(d)
                registering = bool(st.decorator_list) and not deco_names <= {"staticmethod", "classmethod", "property", "dataclass", "abstractmethod", "abc", "functools", "cache", "lru_cache", "cached_property", "override", "wraps", "contextmanager", "asynccontextmanager", "contextlib", "frozen", "True", "False", "slots", "field", "kw_only", "setter", "typing", "overload", "final", "runtime_checkable"}
                if isinstance(st, ast.ClassDef):
                    body_refs = set()
                    for b in st.bases + st.keywords:
                        body_refs |= names_in(b)
                    for d in st.decorator_list:
                        body_refs |= names_in(d)
                    for sub in st.body:
                        if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            body_refs |= names_in(sub)
                    ext = any(ast.unparse(b).split("[")[0].split(".")[-1] not in LOCAL_CLASSES | {"object","Generic","Protocol","BaseModel","Exception","RuntimeError","ValueError","str","Enum","StrEnum","IntEnum","TypedDict","NamedTuple"} for b in st.bases) or st.name in ("ACPServer","ACPKaos","_NullWritable","ACPProcess")
                    defs.append(dict(f=f, line=st.lineno, end=st.end_lineno, name=st.name, kind=kind, refs=body_refs,
                                     root=registering and f not in EXCLUDE_FILES, cls=cls))
                    visit_body(st.body, cls=st.name, cls_external=ext)
                else:
                    refs = names_in(st)
                    is_root = (registering or (cls_external and not st.name.startswith("_x")) or (st.name.startswith("__") and st.name.endswith("__"))) and f not in EXCLUDE_FILES
                    defs.append(dict(f=f, line=st.lineno, end=st.end_lineno, name=st.name, kind=kind, refs=refs,
                                     root=is_root, cls=cls, cls_external=cls_external))
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                continue
            elif isinstance(st, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id == "__all__" for tg in st.targets):
                continue
            elif cls is None:
                if f not in EXCLUDE_FILES:
                    root_refs.update(names_in(st))
    visit_body(t.body)

# non-python references (yaml, md templates, toml)
for dp, dn, fn in os.walk("coderai"):
    for f in fn:
        if f.endswith((".yaml", ".yml", ".md", ".toml", ".json")):
            root_refs.update(re.findall(r"[A-Za-z_]\w*", open(os.path.join(dp, f), errors="ignore").read()))
for extra in ("pyproject.toml", "coderai.spec"):
    root_refs.update(re.findall(r"[A-Za-z_]\w*", open(extra).read()))

by_name = {}
for i, d in enumerate(defs):
    by_name.setdefault(d["name"], []).append(i)
live = set()
live_names = set(root_refs)
queue = [i for i, d in enumerate(defs) if d["root"]]
for n in list(live_names):
    queue.extend(by_name.get(n, []))
while queue:
    i = queue.pop()
    if i in live:
        continue
    live.add(i)
    for n in defs[i]["refs"]:
        if n not in live_names:
            live_names.add(n)
            queue.extend(by_name.get(n, []))
dead = [d for i, d in enumerate(defs) if i not in live]
out = [dict(f=d["f"], line=d["line"], end=d["end"], name=d["name"], kind=d["kind"], cls=d["cls"], size=d["end"] - d["line"] + 1) for d in dead]
json.dump(out, open("/tmp/rv/audit2/transdead2.json", "w"), indent=0)
print(len(defs), "defs;", len(dead), "dead")
