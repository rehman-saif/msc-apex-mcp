"""Read-only access to the apex-scripting skill from the nastran-fea-skills repo.

setup.bat clones https://github.com/rehman-saif/nastran-fea-skills into vendor/. The
server serves that skill's rules and generated API reference through two MCP tools, so
any MCP client (not only ones that load skills) can ground run_apex_python code in the
real Iberian Lynx FP2 signatures instead of recalled ones.
"""
import glob
import os
import re

SKILLS_REPO = "https://github.com/rehman-saif/nastran-fea-skills"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "vendor", "nastran-fea-skills")

MAX_BLOCK = 7000      # best-matching signature or class block (createSurfaceMesh is ~6k)
MAX_MINOR = 2500      # the other blocks
MAX_ENUM_LINE = 1500  # apex.EntityType alone is ~9k chars
MAX_TOTAL = 14000     # whole lookup response

FUNC_HEAD = re.compile(r"^### `(apex(?:\.\w+)*)\.(\w+)\(")
CLASS_HEAD = re.compile(r"^## `(apex(?:\.\w+)*)\.(\w+)`")
INDEX_LINE = re.compile(r"^- `([\w.]+)`(?:\s+extends [^—]*)?(?:\s+—\s+(.*))?$")
ENUM_LINE = re.compile(r"^\s*- `([\w.]+)`: (.*)$")  # indented: one line of a wrapped enum
ENUM_WRAPPED = re.compile(r"^- `([\w.]+)` — \d+ members, wrapped below(?: — (.*))?$")
SIG_HEAD = re.compile(r"^### `(apex(?:\.\w+)*)\.(\w+)\((.*)\)(?: -> .*)?`\s*$")
METHOD_LINE = re.compile(r"^- `(\w+)\(")
METHOD_INDEX = re.compile(r"^- `((?:apex\.)?[\w.]+)\.(\w+)`(.*)$")
INHERITED = re.compile(r"\(inherited by (.*)\)\s*$")
MAX_SIG = 1200  # one signature line in an error report


def split_params(text):
    """Parameter names of 'a: [int], b: {str:[int]} = None' (commas inside brackets do not split)."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [n for n in (p.split(":")[0].split("=")[0].strip() for p in out) if n]


def find_skill(cfg=None):
    """Return the apex-scripting folder, or None. Accepts the repo root or the skill folder."""
    for d in ((cfg or {}).get("skills_dir"), os.environ.get("APEX_SKILLS_DIR"), DEFAULT_DIR):
        if not d:
            continue
        for cand in (os.path.join(d, "skills", "apex-scripting"), d):
            if os.path.isfile(os.path.join(cand, "SKILL.md")) and os.path.isdir(os.path.join(cand, "api")):
                return cand
    return None


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _cut(text, n):
    return text if len(text) <= n else text[:n] + "\n… (truncated)"


class ApexScriptingSkill:
    def __init__(self, path):
        self.path = path
        self.api = os.path.join(path, "api")
        self._functions = self._classes = self._enums = None

    # ------------------------------------------------------------------ guide
    def topics(self):
        t = {"overview": os.path.join(self.path, "SKILL.md"),
             "api-readme": os.path.join(self.api, "README.md")}
        for f in sorted(glob.glob(os.path.join(self.path, "references", "*.md"))):
            t[os.path.splitext(os.path.basename(f))[0]] = f
        return t

    def guide(self, topic):
        topics = self.topics()
        key = (topic or "").strip().lower().replace("_", "-").replace(" ", "-")
        if key.endswith(".md"):
            key = key[:-3]
        if key not in topics:
            return "Unknown topic %r. Topics: %s" % (topic, ", ".join(topics))
        return _read(topics[key])

    # ----------------------------------------------------------------- lookup
    def _index(self, fname):
        out = []
        for line in _read(os.path.join(self.api, fname)).splitlines():
            m = INDEX_LINE.match(line)
            if m:
                out.append((m.group(1), m.group(2) or ""))
        return out

    def functions(self):
        if self._functions is None:
            self._functions = self._index("functions-index.md")
        return self._functions

    def classes(self):
        if self._classes is None:
            self._classes = self._index("classes.md")
        return self._classes

    def enums(self):
        if self._enums is None:
            members, notes = {}, {}  # a long enum comes as a head line plus several lines of members
            for line in _read(os.path.join(self.api, "enums.md")).splitlines():
                m = ENUM_WRAPPED.match(line)
                if m:
                    members.setdefault(m.group(1), [])
                    notes[m.group(1)] = m.group(2) + " — " if m.group(2) else ""
                    continue
                m = ENUM_LINE.match(line)
                if m:
                    members.setdefault(m.group(1), []).append(m.group(2))
            self._enums = [(n, notes.get(n, "") + ", ".join(p)) for n, p in members.items()]
        return self._enums

    # ------------------------------------------------- structured index (preflight, error reports)
    def doc_index(self):
        """{'modules': set, 'functions': {qual: [[param names] per overload]}, 'classes': set,
        'enums': {qual: [members]}} parsed from the generated reference."""
        if getattr(self, "_doc", None) is None:
            functions, modules = {}, set()
            for f in sorted(glob.glob(os.path.join(self.api, "modules", "*.md"))):
                modules.add(os.path.splitext(os.path.basename(f))[0])
                for line in _read(f).splitlines():
                    m = SIG_HEAD.match(line)
                    if m:
                        functions.setdefault(m.group(1) + "." + m.group(2), []).append(split_params(m.group(3)))
            enums = {}
            for line in _read(os.path.join(self.api, "enums.md")).splitlines():
                m = ENUM_LINE.match(line)
                if m:
                    enums.setdefault(m.group(1), []).extend(re.findall(r"`(\w+)`", m.group(2).split(" — ")[0]))
                    continue
                m = INDEX_LINE.match(line)  # an enum that fits one line: - `apex.X`: `A`, `B`
                if not m and line.startswith("- `") and "`: " in line:
                    name, rest = line[3:].split("`: ", 1)
                    enums.setdefault(name, []).extend(re.findall(r"`(\w+)`", rest.split(" — ")[0]))
            self._doc = {"modules": modules, "functions": functions,
                         "classes": {n for n, _ in self.classes()}, "enums": enums,
                         "methods": set(self._method_index())}
        return self._doc

    def signature(self, qual):
        """The documented signature line(s) of a module function, e.g. 'apex.getEntities(pathNames: [str])
        -> apex.EntityCollection' (all overloads)."""
        module = qual.rsplit(".", 1)[0]
        path = os.path.join(self.api, "modules", module + ".md")
        if not os.path.isfile(path):
            return []
        out = []
        for line in _read(path).splitlines():
            m = SIG_HEAD.match(line)
            if m and m.group(1) + "." + m.group(2) == qual:
                out.append(_cut(line[5:].rstrip().rstrip("`"), MAX_SIG))
        return out

    def _method_index(self):
        if getattr(self, "_methods", None) is None:
            idx = {}
            for line in _read(os.path.join(self.api, "methods-index.md")).splitlines():
                m = METHOD_INDEX.match(line)
                if m:
                    inh = INHERITED.search(m.group(3))
                    heirs = set(re.findall(r"`(\w+)`", inh.group(1))) if inh else set()
                    idx.setdefault(m.group(2), []).append((m.group(1), heirs))
            self._methods = idx
        return self._methods

    def documented_methods(self, cls, bases=()):
        """Method names the reference documents for a class, its own and inherited ones. bases (the
        runtime MRO) matter: the index often says only 'inherited by 404 classes'."""
        owners = {cls, *bases}
        return {m for m, entries in self._method_index().items()
                for decl, heirs in entries if decl.rsplit(".", 1)[-1] in owners or cls in heirs}

    def class_brief(self, cls):
        """(qualified name, one-line description) of a documented class by its short name, or None."""
        for name, brief in self.classes():
            if name.rsplit(".", 1)[-1] == cls:
                return name, brief
        return None

    def method_signature(self, cls, method, bases=()):
        """Documented signature line(s) of cls.method, found on cls or on the class it inherits it from
        (bases: the runtime MRO, nearest first)."""
        rank = {c: i for i, c in enumerate([cls] + [b for b in bases if b != cls])}
        entries = [(decl, heirs) for decl, heirs in self._method_index().get(method, [])
                   if decl.rsplit(".", 1)[-1] in rank or cls in heirs]
        entries.sort(key=lambda e: rank.get(e[0].rsplit(".", 1)[-1], len(rank)))
        for decl, heirs in entries:
            block = self._block("classes", decl, CLASS_HEAD, limit=10 ** 6)
            lines = [ln[2:] for ln in block.splitlines() if METHOD_LINE.match(ln) and METHOD_LINE.match(ln).group(1) == method]
            if lines:
                where = "" if decl.rsplit(".", 1)[-1] == cls else " (inherited from %s)" % decl
                return ["%s.%s%s" % (cls, _cut(ln.strip("`").replace("` — ", " — ", 1), MAX_SIG), where)
                        for ln in lines]
        return []

    def _block(self, folder, full_name, head_re, limit=MAX_BLOCK):
        """The `###`/`##` section for full_name in api/<folder>/<module>.md, all overloads."""
        module, short = full_name.rsplit(".", 1)
        path = os.path.join(self.api, folder, module + ".md")
        if not os.path.isfile(path):
            return ""
        level = "###" if folder == "modules" else "##"
        out, on = [], False
        for line in _read(path).splitlines():
            m = head_re.match(line)
            if m:
                on = (m.group(1), m.group(2)) == (module, short)
            elif on and (line.startswith(level + " ") or (level == "###" and line.startswith("## "))):
                on = False
            if on:
                out.append(line)
        return _cut("\n".join(out).strip(), limit)

    @staticmethod
    def _score(name, brief, query, tokens):
        n, short = name.lower(), name.rsplit(".", 1)[-1].lower()
        if n == query or short == query:
            s = 100
        elif all(t in n for t in tokens):
            s = 20 + (10 if short.startswith(tokens[0]) else 0)
        elif all(t in (n + " " + brief.lower()) for t in tokens):
            s = 5
        else:
            return 0
        if "deprecated" in brief.lower() or "no longer supported" in brief.lower():
            s -= 4
        return s - len(short) / 100.0  # shorter (more exact) names first

    def lookup(self, query, kind="any", max_results=5):
        query = (query or "").strip()
        if not query:
            return "Give a function, class or enum name, or a few words (e.g. 'surface mesh')."
        kind = (kind or "any").lower()
        q = query.lower()
        tokens = [t for t in re.split(r"[^\w]+", q) if t and t != "apex"] or [q]
        max_results = max(1, min(int(max_results or 5), 15))
        parts = []

        if kind in ("any", "function"):
            hits = sorted(((self._score(n, b, q, tokens), n, b) for n, b in self.functions()), reverse=True)
            hits = [h for h in hits if h[0] > 0][:max_results]
            if hits:
                parts.append("## Functions")
                for i, (_, n, b) in enumerate(hits):
                    block = self._block("modules", n, FUNC_HEAD, MAX_BLOCK if i == 0 else MAX_MINOR) if i < 3 else ""
                    parts.append(block or "- `%s` — %s" % (n, _cut(b, 300)))

        if kind in ("any", "class"):
            hits = sorted(((self._score(n, b, q, tokens), n, b) for n, b in self.classes()), reverse=True)
            hits = [h for h in hits if h[0] > 0][:max_results]
            if hits:
                parts.append("## Classes")
                for i, (s, n, b) in enumerate(hits):
                    block = self._block("classes", n, CLASS_HEAD) if (i == 0 and s >= 20) else ""
                    parts.append(block or "- `%s` — %s" % (n, _cut(b, 300)))

        if kind in ("any", "enum"):
            member = re.compile(r"`%s`" % re.escape(query.rsplit(".", 1)[-1]), re.I)
            hits = []
            for n, members in self.enums():
                s = self._score(n, "", q, tokens)
                if not s and member.search(members):
                    s = 15  # query is a member name, e.g. 'Quad4'
                if s:
                    hits.append((s, n, members))
            hits = sorted(hits, reverse=True)[:max_results]
            if hits:
                parts.append("## Enums")
                for _, n, m in hits:
                    text = _cut(m, MAX_ENUM_LINE)
                    hit = member.search(m)
                    if hit and not member.search(text):
                        text += " Includes %s." % hit.group(0)  # the member asked for was cut off
                    parts.append("- `%s`: %s" % (n, text))

        if not parts:
            return ("Nothing in the Apex API reference matches %r. Do not guess the call: try "
                    "another word (verbs like create/assign/get, or the entity type), or "
                    "apex_scripting_guide('module-selection')." % query)
        head = "Apex API reference (Iberian Lynx FP2) for %r. Pass every argument by keyword.\n" % query
        return _cut(head + "\n\n".join(parts), MAX_TOTAL)
