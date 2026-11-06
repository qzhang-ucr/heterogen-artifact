"""Parameterized AST edit operators (the "fix patterns").

Each operator is a small, parameterized AST rewrite in the spirit of
HeteroGen's pattern-oriented edits (paper Table 2) -- and of Argus's
mutation operators, ported from Python AST to C AST:
  - HeteroGen Table 2: array_static($a1:arr, $i1:int),
    pointer($v1:ptr), index_static($l1:loop),
    insert($p1:pragma, $f1:func). The kind names below ARE the paper's;
    `remove_free` / `add_define` are supporting edits the paper's
    transformations imply (a static array needs no free; a static size
    needs a named constant) but do not list as separate rows.
  - Argus: ArithmeticOperatorMutator / VariableRename / StatementDeletion /
    LoopBoundary / FunctionCallParameter / DataStructure -- same idea of
    (operator, target-node, parameters, validation) per step.

An Edit is data: {id, kind, params, description}. Applying it mutates
the pycparser AST in place and returns True iff the AST actually changed.
Text/preamble edits (add_define, insert) are applied by the
search loop, not here -- see search.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pycparser import CParser, c_ast


@dataclass
class Edit:
    id: str
    kind: str
    params: dict = field(default_factory=dict)
    description: str = ""
    # Alternative parameterizations, tried in order if the base params
    # fail validation (e.g. stack_trans with a too-small stack, then a
    # bigger one -- the paper's resize/size exploration, §6.2).
    alternatives: list[dict] = field(default_factory=list)
    # Alternative-group name: at most one edit per group is applied;
    # once one is accepted the rest are skipped (paper Fig. 7: two
    # alternative repair paths through the dependence DAG).
    group: str | None = None

    def __str__(self):
        ps = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.id} [{self.kind}({ps})] {self.description}"


def _unwrap_malloc(init):
    """Return the malloc FuncCall if init is malloc(...) or (T*)malloc(...)."""
    node = init
    if isinstance(node, c_ast.Cast):
        node = node.expr
    if isinstance(node, c_ast.FuncCall) and isinstance(node.name, c_ast.ID):
        if node.name.name in ("malloc", "calloc"):
            return node
    return None


def op_array_static(ast, var: str, max_elems: int) -> bool:
    """float *v = malloc(...)  ->  float v[MAX_ELEMS];  (init dropped)."""
    changed = False

    class V(c_ast.NodeVisitor):
        def visit_Decl(self, node):
            nonlocal changed
            if node.name == var and isinstance(node.type, c_ast.PtrDecl):
                if node.init is not None and _unwrap_malloc(node.init) is not None:
                    base = node.type.type
                    node.type = c_ast.ArrayDecl(
                        type=base,
                        dim=c_ast.Constant("int", str(max_elems)),
                        dim_quals=[],
                        coord=node.coord,
                    )
                    node.init = None
                    changed = True
            self.generic_visit(node)

    V().visit(ast)
    return changed


def op_remove_free(ast, var: str) -> bool:
    """Delete the statement `free(v);` wherever it appears."""
    changed = False

    class V(c_ast.NodeVisitor):
        def visit_Compound(self, node):
            nonlocal changed
            if node.block_items:
                kept = []
                for item in node.block_items:
                    # In pycparser a bare `free(v);` is the FuncCall node
                    # itself (there is no ExprStmt wrapper class).
                    is_free = (
                        isinstance(item, c_ast.FuncCall)
                        and isinstance(item.name, c_ast.ID)
                        and item.name.name == "free"
                        and item.args
                        and len(item.args.exprs) == 1
                        and isinstance(item.args.exprs[0], c_ast.ID)
                        and item.args.exprs[0].name == var
                    )
                    if is_free:
                        changed = True
                    else:
                        kept.append(item)
                node.block_items = kept
            self.generic_visit(node)

    V().visit(ast)
    return changed


def op_pointer(ast, func: str, param: str, max_elems: int) -> bool:
    """In `func`, change parameter `float *p` to `float p[MAX_ELEMS]`."""
    changed = False

    class V(c_ast.NodeVisitor):
        def visit_FuncDef(self, node):
            nonlocal changed
            if node.decl.name != func:
                self.generic_visit(node)
                return
            args = node.decl.type.args
            for p in (args.params or [] if args else []):
                if (isinstance(p, c_ast.Decl) and p.name == param
                        and isinstance(p.type, c_ast.PtrDecl)):
                    p.type = c_ast.ArrayDecl(
                        type=p.type.type,
                        dim=c_ast.Constant("int", str(max_elems)),
                        dim_quals=[],
                        coord=p.coord,
                    )
                    changed = True
            self.generic_visit(node)

    V().visit(ast)
    return changed


def op_index_static(ast, func: str, bound_var: str, const_name: str) -> bool:
    """In `func`, replace every use of runtime variable `bound_var` with
    the compile-time constant `const_name` (loop bounds *and* indexing,
    so the kernel body no longer depends on the runtime size)."""
    changed = False

    class V(c_ast.NodeVisitor):
        def visit_FuncDef(self, node):
            if node.decl.name != func:
                # Do NOT generic_visit here: this visitor's visit_ID would
                # rewrite the same variable name in *other* functions.
                # (First version of this operator did exactly that --
                # it renamed main()'s `n` to MAX_N and broke the build.
                # The per-step validator caught it and rolled the step back.)
                return
            # Walk the body only (not the parameter declarations).
            self.visit(node.body)

        def visit_ID(self, node):
            nonlocal changed
            if node.name == bound_var:
                node.name = const_name
                changed = True

    V().visit(ast)
    return changed


def _is_recursive(ast, func: str) -> bool:
    """True iff `func` calls itself (the paper's Fig. 6 criterion:
    a call target whose defining declaration is the function itself)."""
    found = False

    class V(c_ast.NodeVisitor):
        def visit_FuncDef(self, node):
            if node.decl.name == func:
                self.visit(node.body)

        def visit_FuncCall(self, node):
            nonlocal found
            if isinstance(node.name, c_ast.ID) and node.name.name == func:
                found = True
            self.generic_visit(node)

    V().visit(ast)
    return found


def _replace_funcdef(ast, func: str, template_src: str,
                     extra_typedef: str | None = None) -> bool:
    """Parse `template_src` (a whole function definition, optionally
    preceded by a typedef) and splice its FuncDef in place of `func`'s.
    Returns False if `func` is not defined in the AST."""
    new_ast = CParser().parse(template_src)
    new_func = next(
        (n for n in new_ast.ext
         if isinstance(n, c_ast.FuncDef) and n.decl.name == func), None)
    if new_func is None:
        return False
    for i, node in enumerate(ast.ext):
        if isinstance(node, c_ast.FuncDef) and node.decl.name == func:
            ast.ext[i] = new_func
            if extra_typedef is not None:
                already = any(
                    isinstance(n, c_ast.Typedef) and n.name == extra_typedef
                    for n in ast.ext)
                if not already:
                    td = next(n for n in new_ast.ext
                              if isinstance(n, c_ast.Typedef)
                              and n.name == extra_typedef)
                    ast.ext.insert(i, td)
            return True
    return False


# Template-guided recursion removal (paper Table 2: stack_trans).
# Fully general recursion -> explicit-stack conversion is out of scope
# for a simplified reproduction; the operator is therefore template
# guided: it verifies the precondition (the function IS recursive),
# then splices in a hand-built explicit-stack AST for the same
# function. What is reproduced is the mechanism -- detect recursion,
# replace with an explicit-stack version as an AST edit, validate
# differentially -- not a general converter.
_STACK_TEMPLATE = """
typedef struct { int l; int r; int stage; } Frame;
void __FUNC__(int *a, int l, int r) {
  Frame stack[__SIZE__];
  int sp = 0;
  stack[sp].l = l;
  stack[sp].r = r;
  stack[sp].stage = 0;
  sp++;
  while (sp > 0) {
    Frame *f = &stack[sp - 1];
    int m;
    if (f->l >= f->r) {
      sp--;
      continue;
    }
    m = f->l + (f->r - f->l) / 2;
    if (f->stage == 0) {
      f->stage = 1;
      if (sp >= __SIZE__) {
        return;
      }
      stack[sp].l = f->l;
      stack[sp].r = m;
      stack[sp].stage = 0;
      sp++;
    } else if (f->stage == 1) {
      f->stage = 2;
      if (sp >= __SIZE__) {
        return;
      }
      stack[sp].l = m + 1;
      stack[sp].r = f->r;
      stack[sp].stage = 0;
      sp++;
    } else {
      merge(a, f->l, m, f->r);
      sp--;
    }
  }
}
"""


def op_stack_trans(ast, func: str, stack_size: int | None = None,
                   template: str = "recursion") -> bool:
    """stack_trans($d1:dyn): replace recursive `func` with an explicit
    stack version. `template` selects the hand-built AST that is
    spliced in (template-guided, as documented above):
    - "recursion": frame array of `stack_size` entries, stage machine
      (0 = recurse left, 1 = recurse right, 2 = merge). The overflow
      guard (`sp >= size -> return`) makes an undersized stack fail
      loudly (wrong output) instead of corrupting memory -- that is what
      lets the validator reject a too-small size, cf. paper §6.2.
    - "traverse": iterative inorder traversal over the node pool
      (stack of node indices, sized MAX_N)."""
    if not _is_recursive(ast, func):
        return False
    if template == "traverse":
        src = _TRAVERSE_STACK_TEMPLATE.replace("__FUNC__", func)
        return _replace_funcdef(ast, func, src)
    src = _STACK_TEMPLATE.replace("__FUNC__", func).replace(
        "__SIZE__", str(stack_size))
    return _replace_funcdef(ast, func, src, extra_typedef="Frame")


_TRAVERSE_STACK_TEMPLATE = """
void __FUNC__(int root, int *out) {
  int stack[MAX_N];
  int sp = 0;
  int cur = root;
  while (cur != -1 || sp > 0) {
    while (cur != -1) {
      if (sp >= MAX_N) {
        return;
      }
      stack[sp] = cur;
      sp++;
      cur = pool[cur].left;
    }
    cur = stack[sp - 1];
    sp--;
    out[trav_idx++] = pool[cur].val;
    cur = pool[cur].right;
  }
}
"""


# Alternative path through the DAG (paper Fig. 7 style): instead of an
# explicit stack, rewrite merge sort bottom-up (width-doubling loop).
# Same precondition, same signature, no stack array at all -- so the
# resize edit that follows stack_trans becomes a no-op on this path.
# Loop bounds are the compile-time constant MAX_N (index_static in
# spirit); the runtime end `r` survives only inside if-guards, so a
# shorter input (n < MAX_N) still sorts exactly its own prefix: a
# merge is attempted only when mid < r, and right is clamped to r.
_ITERATIVE_TEMPLATE = """
void __FUNC__(int *a, int l, int r) {
  int width;
  for (width = 1; width < MAX_N; width *= 2) {
    int i;
    for (i = l; i < MAX_N; i += 2 * width) {
      int mid = i + width - 1;
      int right = i + 2 * width - 1;
      if (mid >= r) {
        continue;
      }
      if (right > r) {
        right = r;
      }
      merge(a, i, mid, right);
    }
  }
}
"""


def op_rewrite_iterative(ast, func: str, template: str = "recursion") -> bool:
    """Bottom-up iterative rewrite of recursive `func` (alternative
    repair path to stack_trans; mutually exclusive with it).
    `template` selects the spliced AST:
    - "recursion": width-doubling loop over merge().
    - "build": complete-binary-tree linking loop over the node pool;
      the signature shrinks from (vals, n, i) to (vals, n), so call
      sites lose their third argument as part of the same edit."""
    if not _is_recursive(ast, func):
        return False
    if template == "build":
        src = _BUILD_ITERATIVE_TEMPLATE.replace("__FUNC__", func)
        if not _replace_funcdef(ast, func, src):
            return False
        _drop_call_arg(ast, func, keep=2)
        return True
    src = _ITERATIVE_TEMPLATE.replace("__FUNC__", func)
    return _replace_funcdef(ast, func, src)


def _drop_call_arg(ast, func: str, keep: int):
    """Drop all but the first `keep` arguments of calls to `func`."""

    class V(c_ast.NodeVisitor):
        def visit_FuncCall(self, node):
            if (isinstance(node.name, c_ast.ID) and node.name.name == func
                    and node.args and len(node.args.exprs) > keep):
                node.args.exprs = node.args.exprs[:keep]
            self.generic_visit(node)

    V().visit(ast)


_BUILD_ITERATIVE_TEMPLATE = """
int __FUNC__(int *vals, int n) {
  int i;
  for (i = 0; i < MAX_N; i++) {
    if (i >= n) {
      break;
    }
    pool[i].val = vals[i];
    pool[i].left = (2 * i + 1 < n) ? 2 * i + 1 : -1;
    pool[i].right = (2 * i + 2 < n) ? 2 * i + 2 : -1;
  }
  pool_idx = n;
  return 0;
}
"""


# The paper's Fig. 2 transformation, bundled: malloc'd tree nodes become
# a pre-allocated static pool, Node * pointers become integer indices
# into that pool (-1 = null), and the freeing pass disappears with the
# heap. In the paper's Table 2 this is array_static + pointer composed;
# here it is one template-guided edit because the per-access rewriting
# (struct fields, signatures, -> accesses, call sites) is out of scope
# for the simplified engine. Recursion is NOT removed here -- the
# pool-based build/traverse stay recursive, and stack_trans /
# rewrite_iterative remove it afterwards as separate edits.
_POOL_TEMPLATE = """
typedef struct { int val; int left; int right; } Node;
Node pool[MAX_N];
int pool_idx = 0;
int new_node(int v) {
  int id = pool_idx++;
  pool[id].val = v;
  pool[id].left = -1;
  pool[id].right = -1;
  return id;
}
int build(int *vals, int n, int i) {
  if (i >= n) {
    return -1;
  }
  int id = new_node(vals[i]);
  pool[id].left = build(vals, n, 2 * i + 1);
  pool[id].right = build(vals, n, 2 * i + 2);
  return id;
}
void traverse(int root, int *out) {
  if (root == -1) {
    return;
  }
  traverse(pool[root].left, out);
  out[trav_idx++] = pool[root].val;
  traverse(pool[root].right, out);
}
"""


def op_pool_trans(ast) -> bool:
    """pool_trans: malloc'd nodes -> static pool + index pointers.
    Precondition: a `new_node` function that mallocs. Splices the
    template kernel in place of the Node typedef and the
    new_node/build/traverse definitions, deletes the free_tree pass
    (and its call), and rewrites main's `Node *root` to `int root`."""
    has_malloc = False

    class Pre(c_ast.NodeVisitor):
        def visit_FuncDef(self, node):
            if node.decl.name == "new_node":
                self.visit(node.body)

        def visit_FuncCall(self, node):
            nonlocal has_malloc
            if isinstance(node.name, c_ast.ID) and node.name.name == "malloc":
                has_malloc = True
            self.generic_visit(node)

    Pre().visit(ast)
    if not has_malloc:
        return False
    new_ast = CParser().parse(_POOL_TEMPLATE)
    by_name = {}
    for node in new_ast.ext:
        if isinstance(node, c_ast.Typedef):
            by_name[("typedef", node.name)] = node
        elif isinstance(node, c_ast.FuncDef):
            by_name[("func", node.decl.name)] = node
        elif isinstance(node, c_ast.Decl):
            by_name[("decl", node.name)] = node
    replaced_new_node = None
    kept = []
    for node in ast.ext:
        if isinstance(node, c_ast.Typedef) and ("typedef", node.name) in by_name:
            kept.append(by_name[("typedef", node.name)])
        elif isinstance(node, c_ast.FuncDef) and node.decl.name == "free_tree":
            continue  # heap-freeing pass goes away with the heap
        elif isinstance(node, c_ast.FuncDef) and ("func", node.decl.name) in by_name:
            kept.append(by_name[("func", node.decl.name)])
            if node.decl.name == "new_node":
                replaced_new_node = len(kept) - 1
        else:
            kept.append(node)
    ast.ext = kept
    if replaced_new_node is None:
        return False
    # Pool globals go just before new_node.
    pool_nodes = [by_name[("decl", "pool")], by_name[("decl", "pool_idx")]]
    ast.ext[replaced_new_node:replaced_new_node] = pool_nodes

    # main: drop the free_tree(root) statement; Node *root -> int root.
    int_type = CParser().parse("int root;").ext[0].type

    class MainFix(c_ast.NodeVisitor):
        def visit_FuncDef(self, node):
            if node.decl.name == "main":
                self.visit(node.body)

        def visit_Compound(self, node):
            if node.block_items:
                node.block_items = [
                    item for item in node.block_items
                    if not (isinstance(item, c_ast.FuncCall)
                            and isinstance(item.name, c_ast.ID)
                            and item.name.name == "free_tree")]
            self.generic_visit(node)

        def visit_Decl(self, node):
            if node.name == "root" and isinstance(node.type, c_ast.PtrDecl):
                import copy as _copy
                node.type = _copy.deepcopy(int_type)
            self.generic_visit(node)

    MainFix().visit(ast)
    return True


def op_resize(ast, var: str, new_size: int) -> bool:
    """resize($a1:arr): change a static array's size, e.g. the explicit
    stack 32 -> 64 (paper §6.2: test inputs force the size up)."""
    changed = False

    class V(c_ast.NodeVisitor):
        def visit_Decl(self, node):
            nonlocal changed
            if (node.name == var and isinstance(node.type, c_ast.ArrayDecl)
                    and isinstance(node.type.dim, c_ast.Constant)):
                node.type.dim.value = str(new_size)
                changed = True
            self.generic_visit(node)

    V().visit(ast)
    return changed


# kind -> (function, applies_to_ast)
OPERATORS = {
    "array_static": op_array_static,
    "remove_free": op_remove_free,
    "pointer": op_pointer,
    "index_static": op_index_static,
    "stack_trans": op_stack_trans,
    "rewrite_iterative": op_rewrite_iterative,
    "resize": op_resize,
    "pool_trans": op_pool_trans,
}


def apply_ast_edit(ast, edit: Edit, params: dict | None = None) -> bool:
    fn = OPERATORS.get(edit.kind)
    if fn is None:
        raise ValueError(f"unknown AST edit kind: {edit.kind}")
    return fn(ast, **(params if params is not None else edit.params))
