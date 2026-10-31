"""AST-based analysis of gold SQL (sqlglot): evidence steps, join keys, implicit joins,
set operations, negation and ranking. Replaces the regex heuristics for anything that depends
on subquery scope.

Everything is resolved to *physical* tables/columns using the DB schema, so aliases, unqualified
columns and case variants (SQLite identifiers are case-insensitive) all map to the schema's own
spelling. Derived tables (FROM (SELECT ...)) are traced through to their base tables only for the
read-set; a join key whose side is a derived column is recorded with `derived: true`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope

DIALECT = "sqlite"


def load_schema(db_path: Path):
    """-> (schema {table: {col: type}}, pk {table: set(cols)}, fks [(ct, cc, pt, pc)])."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    schema, pk, fks = {}, {}, []
    try:
        tabs = [t for (t,) in con.execute("select name from sqlite_master where type='table'")]
        for t in tabs:
            info = con.execute(f'pragma table_info("{t}")').fetchall()
            schema[t] = {r[1]: (r[2] or "TEXT") for r in info}
            pk[t] = {r[1] for r in info if r[5]}
        for t in tabs:
            for (_i, _s, ref, frm, to, *_r) in con.execute(f'pragma foreign_key_list("{t}")'):
                if to is None and len(pk.get(ref, ())) == 1:
                    to = next(iter(pk[ref]))
                fks.append((t, frm, ref, to))
    finally:
        con.close()
    return schema, pk, fks


class Analyzer:
    def __init__(self, schema: dict):
        self.schema = schema
        self.tmap = {t.lower(): t for t in schema}
        self.cmap = {t.lower(): {c.lower(): c for c in cols} for t, cols in schema.items()}
        # sqlglot wants a plain nested dict; lowercase because sqlite names are case-insensitive
        self.glot_schema = {t.lower(): {c.lower(): "TEXT" for c in cols} for t, cols in schema.items()}

    # -- helpers ---------------------------------------------------------------------------
    def _phys(self, table_lower: str, col_lower: str | None = None):
        t = self.tmap.get(table_lower)
        if t is None:
            return None
        if col_lower is None:
            return t
        c = self.cmap[table_lower].get(col_lower)
        return f"{t}.{c}" if c else None

    def _base_tables(self, source):
        """Physical tables behind a scope source (Table node or derived Scope)."""
        if isinstance(source, exp.Table):
            t = self._phys(source.name.lower())
            return [t] if t else []
        out = []
        for _alias, (_n, src) in source.selected_sources.items():
            out += self._base_tables(src)
        return out

    def _resolve_col(self, col: exp.Column, scope, _depth=0):
        """-> ('Table.col' | None, derived: bool).

        Looks in this scope, then outward through parent scopes (correlated subqueries), and
        traces derived tables / CTEs to the base column behind the projection with that name.
        `derived` is True when the base column sits behind a derived table or expression.
        """
        alias = col.table.lower() if col.table else None
        sc = scope
        while sc is not None and alias:
            if alias in sc.selected_sources:
                src = sc.selected_sources[alias][1]
                if isinstance(src, exp.Table):
                    return self._phys(src.name.lower(), col.name.lower()), False
                if _depth > 6:
                    return None, True
                for proj in src.expression.expressions:      # derived table / CTE scope
                    if proj.alias_or_name.lower() == col.name.lower():
                        base = proj.unalias() if isinstance(proj, exp.Alias) else proj
                        target = base if isinstance(base, exp.Column) else base.find(exp.Column)
                        if target is None:
                            return None, True
                        phys, _ = self._resolve_col(target, src, _depth + 1)
                        return phys, True
                return None, True
            sc = sc.parent
        return None, False

    def _rowid_sql(self, node, scope, root_node):
        """Rewrite one SELECT into a query returning the rowid of every real table it reads.

        Keeps FROM/JOIN/WHERE (so the rows are exactly those that pass joins and filters) and
        drops GROUP BY/HAVING/ORDER BY/LIMIT/aggregation. Returns (sql, [physical table per
        output column], note) or (None, [], None) when the scope reads no base table directly.

        Correlated subqueries reference aliases of the enclosing query. We pull in just those
        outer tables (plus the outer WHERE conjuncts that touch only them and contain no
        subquery) so the step runs standalone. That yields a *superset* of the rows strictly
        needed (noted as `correlated_superset`); the sufficiency check still holds.
        """
        sources = [(a, src) for a, (_n, src) in scope.selected_sources.items()
                   if isinstance(src, exp.Table) and self._phys(src.name.lower())]
        if not sources:
            return None, [], None
        q = node.copy()
        q.set("expressions", [
            exp.alias_(exp.Column(this=exp.to_identifier("rowid"), table=exp.to_identifier(a)), f"r{i}")
            for i, (a, _src) in enumerate(sources)])
        for k in ("group", "having", "order", "limit", "offset", "qualify", "windows"):
            q.set(k, None)
        q.set("distinct", exp.Distinct())
        note = None
        # Negation (NOT IN / NOT EXISTS): the rows that matter are every candidate that passes the
        # *other* filters plus the complete inner set -- not just the survivors, which are the
        # answer itself. Drop negated conjuncts from this step's WHERE.
        where = q.args.get("where")
        if where is not None:
            conj = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
            keep = [c for c in conj if not (isinstance(c, exp.Not) and isinstance(
                c.this, (exp.In, exp.Exists)) and c.find(exp.Select, exp.Subquery))]
            if len(keep) != len(conj):
                note = "negation_candidates"
                q.set("where", exp.Where(this=exp.and_(*keep)) if keep else None)
        ext = {c.table.lower() for c in scope.external_columns if c.table}
        if ext and scope.parent is not None:
            note = (note + "+" if note else "") + "correlated_superset"
            pscope = scope.parent
            pulled = {}
            for al in ext:
                src = pscope.selected_sources.get(al, (None, None))[1]
                if isinstance(src, exp.Table):
                    pulled[al] = src
                    q = q.join(exp.Table(this=exp.to_identifier(src.name), alias=exp.TableAlias(
                        this=exp.to_identifier(al))), join_type="CROSS", copy=False)
            pwhere = pscope.expression.args.get("where")
            if pwhere is not None and pulled:
                conj = list(pwhere.this.flatten()) if isinstance(pwhere.this, exp.And) else [pwhere.this]
                for c in conj:
                    refs = {x.table.lower() for x in c.find_all(exp.Column) if x.table}
                    if refs and refs <= set(pulled) and not c.find(exp.Subquery, exp.Select, exp.Exists):
                        q = q.where(c.copy(), copy=False)
        w = root_node.args.get("with_")
        if node is not root_node and w:
            q.set("with_", w.copy())                # CTE steps need their siblings
        return q.sql(dialect=DIALECT, identify=True), [self._phys(src.name.lower()) for _a, src in sources], note

    def normalise_quotes(self, sql: str) -> str:
        """SQLite treats "x" as an identifier only if it names a column/table, else as a string.
        Spider writes string values as "Asia"; sqlglot would parse (and case-fold) them as
        identifiers, silently changing the query. Make the literal reading explicit."""
        names = set(self.tmap) | {c for cm in self.cmap.values() for c in cm}
        out, i, n = [], 0, len(sql)
        while i < n:
            ch = sql[i]
            if ch in "'`[":
                close = {"'": "'", "`": "`", "[": "]"}[ch]
                j = i + 1
                while j < n and sql[j] != close:
                    j += 1
                    if ch == "'" and j < n and sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                        j += 2          # escaped quote ''
                out.append(sql[i:j + 1]); i = j + 1
            elif ch == '"':
                j = sql.find('"', i + 1)
                j = n - 1 if j < 0 else j
                body = sql[i + 1:j]
                out.append(sql[i:j + 1] if body.lower() in names else "'" + body.replace("'", "''") + "'")
                i = j + 1
            else:
                out.append(ch); i += 1
        return "".join(out)

    # -- main ------------------------------------------------------------------------------
    def analyze(self, sql: str) -> dict:
        try:
            tree = sqlglot.parse_one(self.normalise_quotes(sql), read=DIALECT)
            tree = qualify(tree, schema=self.glot_schema, dialect=DIALECT,
                           validate_qualify_columns=False, quote_identifiers=False,
                           identify=False)
            root = build_scope(tree)
        except Exception as e:  # parse/qualify failure is recorded, caller falls back
            return {"parse_ok": False, "parse_error": f"{type(e).__name__}: {str(e)[:120]}"}
        if root is None:
            return {"parse_ok": False, "parse_error": "no scope"}

        res = {"parse_ok": True, "steps": [], "join_keys": [], "implicit_joins": [],
               "set_ops": [], "flags": {}}
        reads_all: dict[str, set] = {}
        flags = dict(aggregate=False, group=False, having=False, distinct=False, ranking=False,
                     negation=False, nested=False, set_op=False, order=False, limit=False)

        scopes = list(root.traverse())
        step_ids = {id(s): f"s{i}" for i, s in enumerate(scopes)}
        for s in scopes:
            node = s.expression
            sid = step_ids[id(s)]
            parent = step_ids.get(id(s.parent)) if s.parent else None
            if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
                op = type(node).__name__.upper()
                res["set_ops"].append({"step": sid, "op": op, "distinct": bool(node.args.get("distinct", True))})
                flags["set_op"] = True
                if op == "EXCEPT":
                    flags["negation"] = True
                continue
            if not isinstance(node, exp.Select):
                continue
            if s.parent is not None and s.scope_type in (
                    sqlglot.optimizer.scope.ScopeType.SUBQUERY, sqlglot.optimizer.scope.ScopeType.DERIVED_TABLE):
                flags["nested"] = True
            reads: dict[str, set] = {}
            for alias, (_n, src) in s.selected_sources.items():
                for t in self._base_tables(src):
                    reads.setdefault(t, set())
            for col in s.columns:
                phys, derived = self._resolve_col(col, s)
                if phys:
                    t, c = phys.split(".", 1)
                    reads.setdefault(t, set()).add(c)
                elif not derived and not col.table:
                    # unqualified but un-resolved: attribute to a unique owning table in scope
                    owners = [t for t in reads if col.name.lower() in self.cmap[t.lower()]]
                    if len(owners) == 1:
                        reads[owners[0]].add(self.cmap[owners[0].lower()][col.name.lower()])
            for t, cs in reads.items():
                reads_all.setdefault(t, set()).update(cs)

            has_agg = any(True for _ in node.find_all(exp.AggFunc)
                          if _.find_ancestor(exp.Select) is node)
            grp = node.args.get("group") is not None
            order = node.args.get("order") is not None
            lim = node.args.get("limit") is not None
            flags["aggregate"] |= has_agg
            flags["group"] |= grp
            flags["having"] |= node.args.get("having") is not None
            flags["distinct"] |= node.args.get("distinct") is not None
            flags["order"] |= order
            flags["limit"] |= lim
            flags["ranking"] |= (order and lim)
            ev_sql, ev_tables, ev_note = self._rowid_sql(node, s, tree)
            res["steps"].append({
                "id": sid, "parent": parent, "kind": "root" if parent is None else "inner",
                "evidence_sql": ev_sql, "evidence_tables": ev_tables, "evidence_note": ev_note,
                "reads": {t: sorted(c) for t, c in sorted(reads.items())},
                "aggregate": has_agg, "group": grp, "ranking": order and lim,
            })

            # join keys / implicit joins from predicates in this scope
            for eq in list(node.find_all(exp.EQ)):
                if eq.find_ancestor(exp.Select) is not node:
                    continue
                l, r = eq.this, eq.expression
                if isinstance(l, exp.Column) and isinstance(r, exp.Column):
                    a, da = self._resolve_col(l, s)
                    b, db = self._resolve_col(r, s)
                    if a and b and a.split(".")[0] != b.split(".")[0]:
                        res["join_keys"].append({"pair": sorted([a, b]), "step": sid,
                                                 "kind": "join_derived" if (da or db) else "join",
                                                 "derived": bool(da or db)})
            for sub in list(node.find_all(exp.In, exp.EQ, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.NEQ)):
                if sub.find_ancestor(exp.Select) is not node:
                    continue
                outer_side, inner = sub.this, None
                rhs = sub.args.get("query") or sub.args.get("expression")
                if isinstance(rhs, exp.Subquery):
                    inner = rhs.this
                elif isinstance(rhs, exp.Select):
                    inner = rhs
                if inner is None or not isinstance(outer_side, exp.Column):
                    continue
                neg = isinstance(sub.parent, exp.Not)
                kind = ("not_in" if neg else "in") if isinstance(sub, exp.In) else "scalar"
                flags["negation"] |= neg
                a, _ = self._resolve_col(outer_side, s)
                inner_scope = next((c for c in s.subquery_scopes if c.expression is inner), None)
                b = None
                if inner_scope is not None and inner.expressions:
                    proj = inner.expressions[0]
                    pc = proj.unalias() if isinstance(proj, exp.Alias) else proj
                    pc = pc if isinstance(pc, exp.Column) else pc.find(exp.Column)
                    if pc is not None:
                        b, _ = self._resolve_col(pc, inner_scope)
                res["implicit_joins"].append({
                    "kind": kind, "outer": a, "inner": b, "outer_step": sid,
                    "inner_step": step_ids.get(id(inner_scope)) if inner_scope else None,
                    "inner_ranking": bool(inner.args.get("order") and inner.args.get("limit")),
                    "inner_aggregate": any(True for _ in inner.find_all(exp.AggFunc)),
                })
            for ex in node.find_all(exp.Exists):
                if ex.find_ancestor(exp.Select) is node:
                    flags["negation"] |= isinstance(ex.parent, exp.Not)
            if any(isinstance(n, exp.Not) and isinstance(n.this, (exp.In, exp.Exists))
                   for n in node.find_all(exp.Not)):
                flags["negation"] = True

        res["reads"] = {t: sorted(c) for t, c in sorted(reads_all.items())}
        res["flags"] = flags
        return res
