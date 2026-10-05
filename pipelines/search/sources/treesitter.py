"""Tree-sitter chunkers for Scala and Java (`scala_ts`, `java_ts`): the same output shape as the heuristic chunkers in gitsrc.py, but
the boundaries come from a parse tree, so they are the ones a reader would draw.

- A declaration (class, def, val, type...) with its doc comment and annotations is one unit. A container (class, object, trait, enum,
  given, extension, interface, record) that fits `max_chars` stays whole, however many members it has.
- A container that does not fit becomes a header chunk (doc, signature, and a `// members:` outline) plus its members, recursively, each
  titled with the enclosing chain. Local definitions inside a method body are never split out of it.
- A method that does not fit is cut between statements (or between `case` clauses of a big match), and every continuation starts with the
  method's signature, so it can be understood and found on its own.
- Runs of small siblings (vals, one-liners, imports) are packed into one chunk up to `pack_chars`, instead of one near-empty embedding each.
- A file the parser reports errors for returns None; the caller falls back to the heuristic chunker for it.

Chunks are (id, title, body, first line, meta) with meta = {"end": last line, "sym": what it is}; ids look like `Enclosing.name#n` and titles
`path  package  Enclosing.name` exactly as the heuristic chunkers write them."""
import re

import tree_sitter_java
import tree_sitter_scala
from tree_sitter import Language, Parser

MAX = 2400                    # chars per chunk, before the title
PACK = 1000                   # adjacent small units are packed into one chunk up to this many chars
FIT = 1.5                     # a single declaration (not a container) up to MAX * FIT chars is not cut


def configure(max_chars=None, pack_chars=None):
    global MAX, PACK
    MAX = max_chars or MAX
    PACK = pack_chars or PACK


class Unit:
    __slots__ = ("name", "enclosing", "text", "line", "end", "sym", "packable", "members")

    def __init__(self, name, enclosing, text, line, end, sym, packable=True, members=None):
        self.name, self.enclosing, self.text, self.line, self.end, self.sym, self.packable, self.members = name, enclosing, text, line, end, sym, packable, members


def _split_text(text, limit):
    """Cut over-long text at blank lines (hard at line ends when a stretch has none)."""
    out, cur, n = [], [], 0
    for ln in text.split("\n"):
        if n > limit and (not ln.strip() or n > limit * 1.5):
            out.append("\n".join(cur)); cur, n = [], 0
        cur.append(ln); n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _clean(s, n=60):
    return " ".join(s.split())[:n]


class Lang:
    """What differs between grammars: which nodes are containers, how to find a node's name and its body, what is a comment."""
    comments = ("comment", "block_comment")
    containers = ()
    transparent = ()                  # containers that only extend the package (`package a { ... }`): no header chunk, not part of the enclosing chain
    bodies = ()                       # body node types of containers
    scoped = ()                       # containers whose name enters the enclosing chain
    skip = ()                         # nodes with no chunk of their own (package statements: they are in every title)
    imports = ("import_declaration",)
    language = None

    def __init__(self):
        self.parser = Parser(Language(self.language()))

    def name(self, n):
        raise NotImplementedError

    def sym(self, n):
        if n.type in self.imports:
            return "imports"
        return n.type.replace("_definition", "").replace("_declaration", "")

    def body(self, n):
        """(body node or None, children to chunk, byte offset where the header ends)."""
        b = next((c for c in n.children if c.type in self.bodies), None)
        if b is None:
            return None, [], n.end_byte
        return b, [c for c in b.children if c.is_named], b.start_byte

    def function_body(self, n):
        return n.child_by_field_name("body")

    def package(self, root, text):
        return ""


class ScalaLang(Lang):
    language = tree_sitter_scala.language
    containers = ("class_definition", "object_definition", "trait_definition", "enum_definition", "given_definition", "extension_definition", "package_clause")
    transparent = ("package_clause",)
    bodies = ("template_body", "with_template_body", "enum_body")
    scoped = containers
    skip = ("package_clause",)        # only a package clause WITHOUT a body is skipped (see `Chunker._kids`)

    def name(self, n):
        t = n.type
        nm = n.child_by_field_name("name")
        if nm is not None:
            return _clean(nm.text.decode())
        if t in ("val_definition", "var_definition", "val_declaration", "var_declaration"):
            p = n.child_by_field_name("pattern")
            return _clean(p.text.decode(), 40) if p is not None else t
        if t == "extension_definition":
            p = n.child_by_field_name("parameters") or next((c for c in n.children if c.type in ("parameters", "class_parameters")), None)
            return "extension" + (" " + _clean(p.text.decode(), 40) if p is not None else "")
        if t == "given_definition":
            return "given"
        if t == "enum_case_definitions":
            first = next((c for c in n.children if c.is_named), None)
            return "case " + (_clean(first.text.decode(), 30) if first is not None else "")
        if t == "import_declaration":
            return "(imports)"
        return f"({t})"

    def body(self, n):
        b, kids, end = super().body(n)
        if b is None and n.type == "extension_definition":              # braceless extension: the members are the node's own children
            kids = [c for c in n.children if c.is_named and c.type in ("function_definition", "function_declaration", "val_definition", "comment", "block_comment")]
            return n, kids, kids[0].start_byte if kids else n.end_byte
        return b, kids, end

    def package(self, root, text):
        names = []
        for c in root.children:
            if c.type == "package_clause":
                p = next((x for x in c.children if x.type == "package_identifier"), None)
                if p is not None:
                    names.append(p.text.decode())
        return ".".join(names)


class JavaLang(Lang):
    language = tree_sitter_java.language
    comments = ("line_comment", "block_comment")
    containers = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "annotation_type_declaration")
    bodies = ("class_body", "interface_body", "enum_body", "annotation_type_body")
    scoped = containers
    skip = ("package_declaration",)

    def name(self, n):
        t = n.type
        nm = n.child_by_field_name("name")
        if nm is not None:
            return _clean(nm.text.decode())
        if t == "field_declaration":
            d = n.child_by_field_name("declarator")
            nm = d.child_by_field_name("name") if d is not None else None
            return _clean(nm.text.decode()) if nm is not None else "field"
        if t == "static_initializer":
            return "<static>"
        if t == "block":
            return "<init>"
        if t == "import_declaration":
            return "(imports)"
        return f"({t})"

    def body(self, n):
        b = n.child_by_field_name("body")
        if b is None:
            return None, [], n.end_byte
        kids = []
        for c in b.children:
            if c.type == "enum_body_declarations":
                kids += [x for x in c.children if x.is_named]
            elif c.is_named:
                kids.append(c)
        return b, kids, b.start_byte

    def package(self, root, text):
        p = next((c for c in root.children if c.type == "package_declaration"), None)
        if p is None:
            return ""
        i = next((c for c in p.children if c.type in ("scoped_identifier", "identifier")), None)
        return i.text.decode() if i is not None else ""


_LANGS = {}


def lang(name):
    if name not in _LANGS:
        _LANGS[name] = {"scala": ScalaLang, "java": JavaLang}[name]()
    return _LANGS[name]


_LICENSE = re.compile(r"copyright|licen[sc]e", re.I)


class Chunker:
    def __init__(self, lg, path, data):
        self.lg, self.path, self.data = lg, path, data
        self.text = data.decode("utf-8", "replace")

    def s(self, a, b):
        return self.data[a:b].decode("utf-8", "replace")

    def line_start(self, i):
        j = self.data.rfind(b"\n", 0, i) + 1
        return j if not self.data[j:i].strip() else i

    def row(self, i):
        return self.data.count(b"\n", 0, i)

    def attach(self, kids):
        """[(node, byte where its doc comment starts)]: comments directly above a node (no blank line) belong to it; a long loose comment
        is a unit of its own; a license header and short loose comments are dropped."""
        out, pending = [], []
        for k in kids:
            if k.type in self.lg.comments:
                if pending and k.start_point[0] > pending[-1].end_point[0] + 1:
                    out += self._loose(pending); pending = []
                pending.append(k)
                continue
            start = k.start_byte
            if pending and pending[-1].end_point[0] >= k.start_point[0] - 1:
                start = pending[0].start_byte
            else:
                out += self._loose(pending)
            pending = []
            out.append((k, start))
        out += self._loose(pending)
        return out

    def _loose(self, comments):
        if not comments:
            return []
        a, b = comments[0].start_byte, comments[-1].end_byte
        if len(self.s(a, b)) < 200 or (a == 0 and _LICENSE.search(self.s(a, b))):
            return []
        return [(comments[0], a, b)]

    def process(self, node, start, enclosing, end=None, loose=False):
        """Chunk units for one declaration (a list: a container that does not fit yields a header and its members)."""
        lg = self.lg
        end = end if end is not None else node.end_byte
        ds = self.line_start(start)
        text = self.s(ds, end)
        line, last = self.row(ds) + 1, node.end_point[0] + 1
        name, sym = ("(comment)", "comment") if loose else (lg.name(node), lg.sym(node))
        if node.type in lg.transparent:
            b, kids, hend = lg.body(node)
            if b is None:                                                  # `package a.b`: nothing to chunk
                return []
            return self.units(kids, enclosing)
        if len(text) <= (MAX if node.type in lg.containers else MAX * FIT):          # a method may run a bit over before it is cut; a class is cut into members sooner
            whole_class = node.type in lg.containers and lg.body(node)[0] is not None      # a class with a body is a unit of its own; one-liners pack
            return [Unit(name, enclosing, text, line, last, sym, packable=len(text) < PACK and not whole_class)]
        if node.type in lg.containers:
            b, kids, hend = lg.body(node)
            if b is not None:
                inner = enclosing + [name] if node.type in lg.scoped else enclosing
                child = self.units(kids, inner)
                outline = [n for u in child if u.enclosing == inner and u.sym not in ("imports", "comment") for n in (u.members or [u.name]) if not n.startswith("(")]
                head = self.s(ds, hend).rstrip()
                if outline:
                    head += "\n  // members: " + ", ".join(dict.fromkeys(outline))[:300]
                heads = [Unit(name, enclosing, h, line, self.row(hend) + 1, sym, packable=False) for h in _split_text(head, MAX)]
                for u in heads[1:]:
                    u.name = name
                return heads + child
        return self.split_function(node, ds, enclosing, name, sym)

    def split_function(self, node, ds, enclosing, name, sym):
        """A declaration too big for one chunk, cut between its statements; each continuation starts with the signature."""
        body = self.lg.function_body(node)
        points = self._points(body) if body is not None else []
        out = []
        if points:
            sig = _clean(self.s(node.start_byte, body.start_byte), 400)
            parts, cur = [], []
            for p in points:
                size = (p.end_byte - cur[0].start_byte) if cur else 0
                if cur and size + (p.end_byte - p.start_byte) > MAX * FIT:
                    parts.append(cur); cur = []
                cur.append(p)
            parts.append(cur)
            for i, grp in enumerate(parts):
                a, b = (ds if i == 0 else grp[0].start_byte), grp[-1].end_byte
                text = self.s(self.line_start(a), b)
                if i:
                    text = f"{sig}\n  // ... continued\n{text}"
                for j, piece in enumerate(_split_text(text, MAX) if len(text) > MAX * FIT else [text]):
                    if j:
                        piece = f"{sig}\n  // ... continued\n{piece}"
                    out.append(Unit(name, enclosing, piece, self.row(a) + 1 if not (i or j) else self.row(grp[0].start_byte) + 1, self.row(b) + 1, sym, packable=False))
        else:
            for i, piece in enumerate(_split_text(self.s(ds, node.end_byte), MAX)):
                out.append(Unit(name, enclosing, piece, self.row(ds) + 1, node.end_point[0] + 1, sym, packable=False))
        return out

    def _points(self, body):
        """The statements of a method body: the children of the first block or case block, looking through single-child wrappers."""
        n = body
        for _ in range(5):
            kids = [c for c in n.children if c.is_named and c.type not in self.lg.comments]
            if len(kids) >= 2:
                return kids
            if len(kids) == 1 and n.type in ("block", "indented_block", "case_block", "match_expression", "case_clause", "switch_expression", "switch_block", "try_expression", "if_expression"):
                n = kids[0]
            elif len(kids) == 1:
                n = kids[0]
            else:
                return []
        return []

    def units(self, kids, enclosing):
        out = []
        for item in self.attach([k for k in kids if k.type not in self.lg.skip or (k.type in self.lg.transparent and self.lg.body(k)[0] is not None)]):
            if len(item) == 3:                                             # a loose comment worth keeping
                out += self.process(item[0], item[1], enclosing, end=item[2], loose=True)
            else:
                out += self.process(item[0], item[1], enclosing)
        return self.pack(out)

    def pack(self, units):
        """Consecutive small units of one level become one chunk, up to PACK chars."""
        out, cur = [], []
        def flush():
            nonlocal cur
            if len(cur) == 1:
                out.append(cur[0])
            elif cur:
                first = next((u for u in cur if u.sym != "imports"), cur[0])           # named after its first real member, not an import
                out.append(Unit(first.name, first.enclosing, "\n\n".join(u.text for u in cur), cur[0].line, cur[-1].end, "imports" if first.sym == "imports" else "members",
                                packable=False, members=[u.name for u in cur if u.sym != "imports"]))
            cur = []
        for u in units:
            if not u.packable:
                flush(); out.append(u)
            elif cur and sum(len(x.text) + 2 for x in cur) + len(u.text) > PACK or cur and cur[0].enclosing != u.enclosing:
                flush(); cur = [u]
            else:
                cur.append(u)
        flush()
        return out


def chunk(language, path, text):
    """[(id, title, body, line, meta)] for one file, or None when the parser reports errors (use the heuristic chunker)."""
    lg = lang(language)
    data = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text
    tree = lg.parser.parse(data)
    if tree.root_node.has_error:
        return None
    ck = Chunker(lg, path, data)
    pkg = lg.package(tree.root_node, ck.text)
    units = ck.units([c for c in tree.root_node.children if c.is_named], [])
    seen, out = {}, []
    for u in units:
        body = u.text.strip("\n")
        if len(body.strip()) < 20:
            continue
        qual = ".".join(u.enclosing + [u.name])
        n = seen[qual] = seen.get(qual, 0) + 1
        title = f"{path}  {pkg}  {qual}" + (f" (+{len(u.members) - 1} more)" if u.members and len(u.members) > 1 else "")
        out.append((f"{qual}#{n}", title, body, u.line, {"end": u.end, "sym": u.sym}))
    return out
