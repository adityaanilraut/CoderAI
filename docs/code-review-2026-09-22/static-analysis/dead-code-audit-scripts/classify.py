import ast, re, subprocess, sys, os, json
from functools import lru_cache
ROOT = "/Users/aditya/Desktop/CoderAI-main"
os.chdir(ROOT)
pat = re.compile(r"^(\S+?):(\d+): unused (\w+) '([^']+)'")
def load(p):
    out = {}
    for l in open(p):
        m = pat.match(l)
        if m:
            out[(m.group(1), int(m.group(2)), m.group(4))] = m.group(3)
    return out
v60 = load("/tmp/rv/vulture60.txt")
vt = load("/tmp/rv/vulture_with_tests.txt")

@lru_cache(None)
def tree(f):
    src = open(f).read()
    return ast.parse(src), src.splitlines()

def ctx(f, line, name):
    t, lines = tree(f)
    res = {"decos": [], "cls": None, "bases": []}
    def walk(node, cls=None):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                if ch.name == name and start <= line <= ch.lineno:
                    res["decos"] = [ast.unparse(d) for d in ch.decorator_list]
                    if cls is not None:
                        res["cls"] = cls.name
                        res["bases"] = [ast.unparse(b) for b in cls.bases]
                    if isinstance(ch, ast.ClassDef):
                        res["bases"] = [ast.unparse(b) for b in ch.bases]
                    return True
                if walk(ch, ch if isinstance(ch, ast.ClassDef) else cls):
                    return True
            else:
                if walk(ch, cls):
                    return True
        return False
    walk(t)
    return res

@lru_cache(None)
def rg(name, paths):
    try:
        out = subprocess.run(["rg", "-n", "--no-heading", "-w", "-F", name, *paths.split()],
                             capture_output=True, text=True).stdout
    except Exception:
        return []
    return [l for l in out.splitlines() if l]

rows = []
for (f, line, name), kind in sorted(v60.items()):
    if kind in ("import",):
        continue
    if name == "_":
        continue
    in_vt = (f, line, name) in vt
    c = ctx(f, line, name) if kind in ("function", "method", "class", "property") else {"decos": [], "cls": None, "bases": []}
    hits = rg(name, "coderai tests tests_e2e scripts sdks pyproject.toml coderai.spec")
    others = [h for h in hits if not h.startswith(f"{f}:{line}:")]
    prod = [h for h in others if h.startswith("coderai/")]
    tst = [h for h in others if h.startswith(("tests/", "tests_e2e/"))]
    scr = [h for h in others if h.startswith(("scripts/", "sdks/", "pyproject", "coderai.spec"))]
    strref = [h for h in prod if re.search(r"""['"]%s['"]""" % re.escape(name), h)]
    cfg = [h for h in prod if not h.split(":")[0].endswith(".py")]
    rows.append(dict(f=f, line=line, name=name, kind=kind, in_vt=in_vt, decos=c["decos"], cls=c["cls"], bases=c["bases"],
                     n_prod=len(prod), n_test=len(tst), n_scr=len(scr), n_str=len(strref), n_cfg=len(cfg),
                     prod=prod[:6], tst=tst[:3], scr=scr[:3]))
json.dump(rows, open("/tmp/rv/audit2/rows.json", "w"), indent=1)
print(len(rows))
