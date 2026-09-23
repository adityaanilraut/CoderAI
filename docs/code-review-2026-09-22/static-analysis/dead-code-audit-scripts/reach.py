import ast, os, sys
ROOT = "/Users/aditya/Desktop/CoderAI-main"
os.chdir(ROOT)
mods = {}
for dp, dn, fn in os.walk("coderai"):
    for f in fn:
        if f.endswith(".py"):
            p = os.path.join(dp, f)
            m = p[:-3].replace("/", ".")
            if m.endswith(".__init__"):
                m = m[:-9]
            mods[m] = p

def resolve(cur, node):
    out = []
    if isinstance(node, ast.Import):
        for a in node.names:
            out.append(a.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            pkg = cur if mods[cur].endswith("__init__.py") else cur.rsplit(".", 1)[0]
            for _ in range(node.level - 1):
                pkg = pkg.rsplit(".", 1)[0]
            base = pkg + ("." + node.module if node.module else "")
        else:
            base = node.module or ""
        out.append(base)
        for a in node.names:
            out.append(base + "." + a.name)
    return out

graph = {}
strrefs = {}
for m, p in mods.items():
    t = ast.parse(open(p).read())
    deps = set()
    for n in ast.walk(t):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for d in resolve(m, n):
                # add all parent packages
                parts = d.split(".")
                for i in range(1, len(parts) + 1):
                    c = ".".join(parts[:i])
                    if c in mods:
                        deps.add(c)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("coderai."):
            s = n.value.split(":")[0]
            if s in mods:
                deps.add(s)
    graph[m] = deps

# yaml/string refs module:Class
import re, glob
yaml_deps = set()
for y in glob.glob("coderai/**/*.yaml", recursive=True) + glob.glob("coderai/**/*.yml", recursive=True):
    for mm in re.findall(r"(coderai(?:\.\w+)+)", open(y).read()):
        parts = mm.split(".")
        for i in range(1, len(parts) + 1):
            c = ".".join(parts[:i])
            if c in mods:
                yaml_deps.add(c)

entries = sys.argv[1:] or ["coderai.main", "coderai.acp", "coderai.__main__", "coderai.cli.__main__"]
seen = set()
stack = list(entries) + sorted(yaml_deps)
while stack:
    m = stack.pop()
    if m in seen or m not in mods:
        continue
    seen.add(m)
    # importing a submodule executes parent packages
    parts = m.split(".")
    for i in range(1, len(parts)):
        stack.append(".".join(parts[:i]))
    stack.extend(graph[m])
unreach = sorted(set(mods) - seen)
tot = 0
for m in unreach:
    n = sum(1 for _ in open(mods[m]))
    tot += n
    print(f"{n:6d}  {m}")
print("TOTAL unreachable lines:", tot, "modules:", len(unreach), "of", len(mods))
