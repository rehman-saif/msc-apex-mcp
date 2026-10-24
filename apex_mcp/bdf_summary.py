"""Summarise and lint a Nastran input deck without reading it into the conversation.

Runs in the MCP server (not in Apex), streaming the file once. Returns the executive and
case control lines, card counts and lint warnings for the mistakes that cost a solver run:
  * contact / glue cards (BCONECT, BCTABLE, BCSURF, ...) with no BCONTACT in the case control,
    or a BCONTACT id that no BCONECT / BCTABLE / BCTABL1 defines;
  * a subcase without LOAD or SPC (after the global case control), or one whose set id
    matches no load / SPC card;
  * RBE2 dependent nodes that are also SPC'd, or dependent in two RBE2s;
  * elements whose property is missing (or of the wrong family), properties whose material
    is missing, and GRID ids referenced but not defined; duplicate GRID / element ids.
Small-field, large-field (*) and free-field (comma) cards are read; INCLUDE is followed.
"""
import os
import re

MAX_CASE_LINES = 150
MAX_EXAMPLES = 8

CONTACT_CARDS = {"BCONECT", "BCTABLE", "BCTABL1", "BCBODY", "BCBODY1", "BCSURF", "BSURF", "BSURFS",
                 "BCPROP", "BCPROPS", "BCONP", "BCTSET", "BGSET", "BCONPRG", "BCONPRP", "BGPARM"}
BCONTACT_TARGETS = {"BCONECT", "BCTABLE", "BCTABL1"}
LOAD_CARDS = {"FORCE", "FORCE1", "FORCE2", "MOMENT", "MOMENT1", "MOMENT2", "PLOAD", "PLOAD1",
              "PLOAD2", "PLOAD4", "PLOADX1", "GRAV", "RFORCE", "RFORCE1", "LOAD", "SPCD", "SLOAD",
              "ACCEL", "ACCEL1", "TEMP", "TEMPD", "DEFORM"}
SPC_CARDS = {"SPC", "SPC1", "SPCADD"}
MAT_CARDS = {"MAT1", "MAT2", "MAT3", "MAT4", "MAT5", "MAT8", "MAT9", "MAT10", "MAT11"}

# element -> (number of grid fields after EID, PID, property family)
ELEMENTS = {
    "CTETRA": (10, "solid"), "CPENTA": (15, "solid"), "CHEXA": (20, "solid"), "CPYRAM": (13, "solid"),
    "CQUAD4": (4, "shell"), "CTRIA3": (3, "shell"), "CQUAD8": (8, "shell"), "CTRIA6": (6, "shell"),
    "CQUADR": (4, "shell"), "CTRIAR": (3, "shell"), "CQUAD": (9, "shell"),
    "CBEAM": (2, "beam"), "CBAR": (2, "bar"), "CROD": (2, "rod"), "CTUBE": (2, "tube"),
    "CBUSH": (2, "bush"), "CSHEAR": (4, "shear"), "CGAP": (2, "gap"),
}
PROPERTIES = {
    "PSOLID": "solid", "PLSOLID": "solid", "PCOMPLS": "solid",
    "PSHELL": "shell", "PCOMP": "shell", "PCOMPG": "shell", "PLPLANE": "shell",
    "PBEAM": "beam", "PBEAML": "beam", "PBCOMP": "beam",
    "PBAR": "bar", "PBARL": "bar", "PROD": "rod", "PTUBE": "tube", "PBUSH": "bush",
    "PSHEAR": "shear", "PGAP": "gap",
}


# ------------------------------------------------------------------ reading
def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _ids(fields):
    """Integers in fields, expanding 'a THRU b'; stops at the first real or other text."""
    out, i = [], 0
    while i < len(fields):
        f = fields[i].upper()
        if f == "":
            i += 1
            continue
        if f == "THRU" and out and i + 1 < len(fields) and _int(fields[i + 1]) is not None:
            out.extend(range(out[-1] + 1, _int(fields[i + 1]) + 1))
            i += 2
            continue
        v = _int(f)
        if v is None:
            break
        out.append(v)
        i += 1
    return out


def _split_fixed(line, large):
    data = line[8:72]
    width = 16 if large else 8
    return [data[i:i + width].strip() for i in range(0, 64, width)]


def _lines(path, seen=None):
    """Lines of the deck, following INCLUDE (single-line, quoted or bare file name)."""
    seen = seen if seen is not None else set()
    real = os.path.realpath(path)
    if real in seen:
        return
    seen.add(real)
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line[:7].upper() == "INCLUDE":
                name = line[7:].strip().strip("'\"")
                yield from _lines(os.path.join(os.path.dirname(path), name), seen)
            else:
                yield line


def read_deck(path):
    """(head_lines, cards): the lines before BEGIN BULK and an iterator of (NAME, fields)."""
    it = _lines(path)
    head = []
    for line in it:
        if line.upper().lstrip().startswith("BEGIN") and "BULK" in line.upper():
            break
        head.append(line)
    return head, _cards(it)


def _cards(lines):
    name, fields = None, []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("$"):
            continue
        if "$" in line:
            line = line[:line.index("$")]
        if line.upper().startswith("ENDDATA"):
            break
        first = line[:1]
        if "," in line[:10] or (first in "+*," and "," in line):
            toks = [t.strip() for t in line.split(",")]
            head_tok, toks = toks[0], toks[1:]
            if len(toks) == 9:
                toks = toks[:8]  # trailing continuation marker
            cont = head_tok == "" or head_tok[:1] in "+*"
        else:
            head_tok = line[:8].strip()
            cont = head_tok == "" or first in "+*"
            toks = _split_fixed(line, large=head_tok.endswith("*") or (cont and first == "*"))
        if cont and name is not None:
            fields.extend(toks)
            continue
        if name is not None:
            yield name, fields
        name, fields = head_tok.rstrip("*").upper(), toks
    if name is not None:
        yield name, fields


# ------------------------------------------------------------------ case control
_CC = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*(?:\(([^)]*)\))?\s*=\s*(.*?)\s*$")


def parse_case_control(head):
    """(sol, case_lines, global dict, [(subcase id, dict)], has_cend) from the lines before
    BEGIN BULK."""
    sol, in_case, case, glob, subs = None, False, [], {}, []
    cur = glob
    for line in head:
        s = line.strip()
        if not s or s.startswith("$"):
            continue
        u = s.upper()
        if not in_case:
            if u.startswith("SOL "):
                sol = s.split(None, 1)[1].strip()
            if u.startswith("CEND"):
                in_case = True
            continue
        case.append(s)
        m = re.match(r"^SUBCASE\s+(\d+)", u)
        if m:
            cur = {}
            subs.append((int(m.group(1)), cur))
            continue
        m = _CC.match(s)
        if m:
            cur[m.group(1).upper()] = m.group(3).strip()
    return sol, case, glob, subs, in_case


# ------------------------------------------------------------------ lint
def _examples(ids):
    ids = sorted(ids)
    return ids[:MAX_EXAMPLES] + (["..."] if len(ids) > MAX_EXAMPLES else [])


def summarize(path):
    head, cards = read_deck(path)
    sol, case, glob, subs, has_cend = parse_case_control(head)
    counts = {}
    grids, grid_dups = set(), 0
    elem_ids, elem_dups = set(), 0
    elem_pid = {}            # family -> {pid: [eid,...]}
    props = {}               # pid -> family
    prop_mids = []           # (card, pid, mid)
    mats = set()
    load_sids, spc_sids, bcontact_ids = set(), set(), set()
    spc_nodes = set()
    rbe2_dep = {}            # node -> [rbe2 eid]
    refs = {}                # card -> set(grid ids referenced)

    for name, f in cards:
        counts[name] = counts.get(name, 0) + 1
        if name == "GRID":
            g = _int(f[0] if f else None)
            if g in grids:
                grid_dups += 1
            grids.add(g)
        elif name in ELEMENTS:
            ngrid, fam = ELEMENTS[name]
            eid, pid = _int(f[0] if f else None), _int(f[1] if len(f) > 1 else None)
            pid = eid if pid is None else pid  # blank PID defaults to the element id
            if eid in elem_ids:
                elem_dups += 1
            elem_ids.add(eid)
            elem_pid.setdefault(fam, {}).setdefault(pid, []).append(eid)
            refs.setdefault(name, set()).update(g for g in (_int(x) for x in f[2:2 + ngrid]) if g)
        elif name in PROPERTIES:
            pid = _int(f[0] if f else None)
            props[pid] = PROPERTIES[name]
            if name == "PSHELL":
                mid_fields = [f[i] for i in (1, 3, 5) if i < len(f)]
            elif name == "PCOMP":
                mid_fields = f[8::4]
            elif name == "PCOMPG":
                mid_fields = f[9::8]
            else:
                mid_fields = f[1:2]
            for m in mid_fields:
                if _int(m):
                    prop_mids.append((name, pid, _int(m)))
        elif name in MAT_CARDS:
            mats.add(_int(f[0] if f else None))
        if name in LOAD_CARDS:
            load_sids.add(_int(f[0] if f else None))
            if name in ("FORCE", "MOMENT", "FORCE1", "FORCE2", "MOMENT1", "MOMENT2", "SPCD", "SLOAD"):
                g = _int(f[1] if len(f) > 1 else None)
                if g:
                    refs.setdefault(name, set()).add(g)
        if name in SPC_CARDS:
            spc_sids.add(_int(f[0] if f else None))
            if name == "SPC":
                nodes = [_int(f[i]) for i in (1, 4) if i < len(f) and _int(f[i])]
            elif name == "SPC1":
                nodes = _ids(f[2:])
            else:
                nodes = []
            spc_nodes.update(nodes)
            refs.setdefault(name, set()).update(nodes)
        if name in BCONTACT_TARGETS:
            bcontact_ids.add(_int(f[0] if f else None))
        if name == "RBE2":
            eid = _int(f[0] if f else None)
            gn = _int(f[1] if len(f) > 1 else None)
            dep = _ids(f[3:])
            for g in dep:
                rbe2_dep.setdefault(g, []).append(eid)
            refs.setdefault(name, set()).update(dep + ([gn] if gn else []))

    lint = []
    contact = sorted(c for c in counts if c in CONTACT_CARDS)
    # case control: global values apply to every subcase that does not set its own
    cases = [(sid, dict(glob, **d)) for sid, d in subs] or ([("global", glob)] if has_cend else [])
    if contact:
        missing = [sid for sid, d in cases if "BCONTACT" not in d]
        what = ", ".join("%s x%d" % (c, counts[c]) for c in contact)
        if cases and len(missing) == len(cases):
            lint.append("contact/glue cards (%s) but no BCONTACT in the case control: Nastran ignores "
                        "them and the parts are not connected" % what)
        elif missing:
            lint.append("BCONTACT missing in subcase(s) %s (contact cards: %s)" % (_examples(missing), what))
    for sid, d in cases:
        tag = "subcase %s" % sid
        for key, sids, kind in (("LOAD", load_sids, "load"), ("SPC", spc_sids, "SPC/SPC1/SPCADD")):
            v = d.get(key)
            if v is None:
                if not (key == "LOAD" and sol and sol.strip().upper() in ("103", "SEMODES", "105", "SEBUCKL")):
                    lint.append("%s has no %s" % (tag, key))
            elif _int(v) is not None and _int(v) not in sids:
                lint.append("%s: %s = %s but no %s card has set id %s" % (tag, key, v, kind, v))
        b = d.get("BCONTACT")
        if b is not None and _int(b) is not None and _int(b) not in bcontact_ids:
            lint.append("%s: BCONTACT = %s but no BCONECT/BCTABLE/BCTABL1 has id %s" % (tag, b, b))
    both = spc_nodes.intersection(rbe2_dep)
    if both:
        lint.append("%d RBE2 dependent node(s) are also SPC'd (RBE2 %s; nodes %s): Nastran fatal "
                    "(dependent DOF cannot be constrained)"
                    % (len(both), _examples({e for g in both for e in rbe2_dep[g]}), _examples(both)))
    twice = [g for g, es in rbe2_dep.items() if len(es) > 1]
    if twice:
        lint.append("%d node(s) are dependent in more than one RBE2 (nodes %s, RBE2 %s)"
                    % (len(twice), _examples(twice), _examples({e for g in twice for e in rbe2_dep[g]})))
    for fam, by_pid in sorted(elem_pid.items()):
        for pid, eids in sorted(by_pid.items(), key=lambda kv: str(kv[0])):
            if pid not in props:
                lint.append("%d %s element(s) reference property %s, which is not defined (elements %s)"
                            % (len(eids), fam, pid, _examples(eids)))
            elif props[pid] != fam:
                lint.append("%d %s element(s) reference property %s, which is a %s property"
                            % (len(eids), fam, pid, props[pid]))
    for card, pid, mid in prop_mids:
        if mid not in mats:
            lint.append("%s %s references material %s, which is not defined" % (card, pid, mid))
    for card, ids in sorted(refs.items()):
        gone = ids - grids
        if gone:
            lint.append("%s references %d undefined GRID id(s): %s" % (card, len(gone), _examples(gone)))
    if grid_dups:
        lint.append("%d duplicate GRID id(s)" % grid_dups)
    if elem_dups:
        lint.append("%d duplicate element id(s)" % elem_dups)

    out = {"path": os.path.abspath(path), "bytes": os.path.getsize(path), "sol": sol,
           "case_control": case[:MAX_CASE_LINES],
           "cards": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
           "lint": lint or ["no problems found by these checks"]}
    if len(case) > MAX_CASE_LINES:
        out["case_control"].append("... %d more lines" % (len(case) - MAX_CASE_LINES))
    return out


if __name__ == "__main__":  # python bdf_summary.py deck.bdf
    import json
    import sys
    print(json.dumps(summarize(sys.argv[1]), indent=1))
