# Research Brief: HeteroGen (ASPLOS 2022) + Argus — for a Simplified C Reproduction

**Sources verified from full text.** HeteroGen PDF obtained from the UCLA mirror (`web.cs.ucla.edu/~miryung/Publications/ASPLOS22_heterogen.pdf`; ACM direct fetch failed, but this is the same camera-ready PDF, DOI 10.1145/3503222.3507748). Argus surveyed from a fresh `git clone` of `github.com/ucr-riple/Argus` (code + README read at source level). Artifacts: HeteroGen code at `github.com/UCLA-SEAL/HeteroGen` (cited in paper §1; not cloned for this brief).

---

## 1. HeteroGen's fix patterns (pattern-oriented edits)

**Origin (§5.1):** distilled from 1,000+ Xilinx HLS Q&A forum posts (keyword search "high level synthesis error" / "C synthesis error"), grouped into **six error types**. Prose-verified shares: unsupported data types ~25% (most frequent), struct/union 14%, dynamic data structures 8%. (Fig. 3 pie-chart percentages for the other three types were garbled in PDF extraction, so I do not quote them.)

**Table 1 (error type → symptom → repair):**

| Type | Example error symptom | Repair (Table 1) |
|---|---|---|
| Dynamic Data Structures | `ERROR: Dynamic memory allocation is not supported` (array of unknown size) | Specify the array size |
| Unsupported Data Types | `long double` → `Call of overloaded 'pow()' is ambiguous` | Type transformation, then explicit type casting + operator overloading |
| Dataflow Optimization | `#pragma HLS dataflow` → `Argument 'data' failed dataflow checking` | Pragma exploration |
| Loop Parallelization | dataflow + unroll pragmas → pre-synthesis failure | Pragma exploration |
| Struct and Union | `Argument 'this' has an unsynthesizable struct type` | Insert explicit constructor + make connecting stream static |
| Top Function | `Cannot find the top function in the design` | Configuration exploration |

**Table 2 — parameterized AST edits** (the actual edit inventory; `$`-prefixed names are parameters concretized per context):

| Type | Parameterized edits (Table 2, verbatim; "etc." in paper = list is not exhaustive) |
|---|---|
| Dynamic Data Structures | `array_static($a1:arr, $i1:int)`, `insert($a1:arr, $d1:dyn)`, `resize($a1:arr)`, `stack_trans($d1:dyn)`, etc. |
| Unsupported Data Types | `pointer($v1:ptr)`, `type_trans($v1:var)`, `array_static($a1:arr)`, `type_casting($v1:var)`, etc. |
| Dataflow Optimization | `delete($p1:pragma, $f1:func)`, `move($p1:pragma, $f1:func)`, `insert($p1:pragma, $f1:func)`, etc. |
| Loop Parallelization | `index_static($l1:loop)`, `mem_reset($l1:loop)`, `init($l1:loop)`, `explore($p1:pragma, $l1:func)`, etc. |
| Struct and Union | `constructor($s1:struct)`, `flatten($s1:struct)`, `stream_static($f1:stream, $s1:struct)`, `inst_static($s1:struct, $v1:name)`, `pointer($s1:struct)`, etc. |
| Top Function | `delete/move/insert($p1:pragma, $f1:func)`, etc. |

**Concrete transformations (from §3 working example, Fig. 2, and §5.1 prose):**
- **malloc → static array:** `*root = malloc(sizeof(Node))` becomes a pre-allocated `Node Node_arr[NODE_ARR_SIZE]` plus a `Node_malloc` accessor returning an array index (`Node_ptr`); pointers `Node *` are rewritten to array indices.
- **Recursion → explicit stack iteration:** recursive `traverse()` becomes a `stack<context>` loop with `goto`-based continuation labels (Fig. 2c) — this is `stack_trans`.
- **Type finitization:** profiled max value of a variable (e.g. `ret`, max 83) → narrow type `fpga_uint<7>`; `long double` → `fpga_float<8,71>` + explicit cast via `thls::to<...>` + hand-written operator overload `sum_80` (Fig. 4).
- **Array resizing:** `resize($a1)` experiments with different static array sizes (e.g. stack size 1024 → 2048, §6.2).
- **Struct fix:** insert explicit constructor + declare the connecting `hls::stream` `static` (Fig. 5); alternative is `flatten` the struct into standalone variables/functions.
- **Pragma exploration:** insert/move/delete `dataflow`/`unroll`/`array_partition` pragmas and vary factors (e.g. unroll factor ≥50 fails only in combination with a dataflow pragma).

**Repair localization (§5.2):** NOT spectrum-based fault localization. Each HLS compiler error message is classified into one of the six types by keywords ("recursion", "dataflow", "struct", …), then type-specific localization finds repair sites (e.g. Fig. 6: a function is recursive iff a call target's defining declaration equals the function itself). Extensible: a new error type = a new localization module.

---

## 2. Dependences between edits (§5.3, Fig. 7)

**Definition:** a dependence is a *precedence constraint* between parameterized edits of the same error type — edit B can only be applied (or only makes sense) after edit A. The dependence structure is a small DAG per error type, hand-derived from the example patches (inspired by SYDIT [40] / LASE [41]).

**Worked example — Struct and Union (Fig. 7c, 10 constituent edits):**
`➊ constructor($s1)` → `➌ stream_static($f1,$s1)` (constructor must exist before the stream can be made static); `➋ flatten($s1)` → `➍ inst_update($s1)` (flatten first, then update all instances). Full node set: ➊ constructor, ➋ flatten, ➌ stream_static, ➍ inst_update, ➎ inst_static, ➏ pointer, ➐ array_static, ➑ type_trans, ➒ type_casting, ➓ op_overload. Two alternative repair *paths* through the DAG: {➊, ➌} (add constructor + static stream, Fig. 7a) vs. {➋, ➍} (flatten + update instances, Fig. 7b).

**Second example — Dynamic Data Structures:** `array_static($a1:arr, $i1:int)` (declare a static array of size `i1`) **must precede** `resize($a1:arr)` (experiment with sizes); "these two edits cannot work in a reverse order" (§5.3).

**How it guides search:** an evolutionary algorithm enumerates *dependence-respecting prefixes* of the DAG, e.g. `{➊, ➋, ➊➌, ➋➍, ➊➌➎, ➋➍➑, …}` — instead of trying edits in random order. Quantified benefit (§6.3): up to **35× faster** than the `WithoutDependence` ablation (random edit order); e.g. the naive probability of picking ➌ after ➊ is 1/10, while dependence guidance applies ➌ right after ➊. If a variant raises errors of *another* type, it is added to a queue and dependence-based search repeats for the new type.

---

## 3. Search-based repair loop (§1, §3, §5.3, §6.1)

- **Pipeline (Fig. 1):** `P_orig` → auto-generate tests → build initial broken HLS version `P_broken` (profiled, bitwidth-estimated HLS types) → compile with HLS compiler → localize repair sites from error symptom → explore dependence-ordered edits → differential-test evaluation → repeat.
- **Fitness / scoring:** two objectives, lexicographic in practice. (1) *Hard constraint:* HLS compatibility + behavior preservation — fitness = **ratio of generated tests with identical CPU-vs-FPGA-simulation behavior**. (2) *Soft constraint:* performance — compare **simulation latency** of generated tests between CPU and FPGA; among candidates that fix the error, keep the fastest. A candidate is accepted as the new "current" version if it passes all tests and outperforms the current version (§1). Compatibility is prioritized over performance.
- **Search strategy:** evolutionary/genetic-programming-style repair (after GenProg [57]); the paper does **not** name beam search or greedy — it enumerates dependence-ordered candidate sets and records simulation time per candidate, keeping the best-performing variant. **No backtracking mechanism is described** beyond the queue: variants that introduce other error types are queued and re-searched rather than rolled back.
- **Cheap pre-filter (early candidate rejection):** before invoking the expensive HLS toolchain (minutes–hours), a lightweight **LLVM front-end coding-style checker** rejects candidates violating HLS coding styles (e.g. an `array_partition` pragma outside the function defining the array; an `unroll` pragma outside a loop body). This avoids 75% of HLS invocations on P3 → **4× overall speedup** vs. `WithoutChecker` (§6.3).
- **Stopping condition:** a **user-specified time budget — 3 hours** in the evaluation (§6.1). If all errors are fixed before the budget expires, search *continues* applying performance-improving edits. On timeout it returns either an error-free faster version or an incomplete version plus generated tests to guide manual edits.
- **Headline results:** 9/10 programs fully automatic (all 10 made HLS-compatible; P1 not faster), 9–438 lines of edits, avg **1.63× faster** than original; vs. HeteroRefactor [33]: 100% vs. 20% transpilation success.

---

## 4. The 10 benchmarks (Table 3 + Table 5, §6)

| ID | Subject | LOC | ΔLOC (HeteroGen) | # tests | What makes it hard for HLS |
|---|---|---|---|---|---|
| P1 | signal transmission (RGB→YUV) | 15 | 69 | 27 | No loops/arrays → no perf edits possible; only HLS-compatible, not faster |
| P2 | arithmetic computation | 24 | 9 | 6,930 | (small kernel; details not itemized) |
| P3 | **merge sort** | 121 | 356 | 1,800 | **Recursive traversal** → stack conversion; stack-size sensitivity (1024 vs 2048, §6.2/Fig. 8) |
| P4 | image processing | 285 | 32 | 47 | Largest microbenchmark |
| P5 | **graph traversal** (binary tree) | 85 | **438** | 38 | Fig. 2 working example: malloc + pointers + recursion; most edits |
| P6 | **matrix multiplication** | **19** | 16 | **14,896** | Smallest kernel; 100% branch coverage |
| P7 | bubble sort | 50 | 25 | 399 | Sorting kernel |
| P8 | linked list | 131 | 298 | 54 | Dynamic data structure (malloc/free, pointers) |
| P9 | face detection (Rosetta, Viola-Jones) | 465 | 144 | 43 | Real-world; only 70% branch coverage; manual port needs 3,272 edits (trace-based memory banking) |
| P10 | digit recognition (Rosetta) | 117 | 35 | 133 | Real-world ML |

Sources: P1–P8 microbenchmarks from HeteroRefactor [33] or the Xilinx forum; P9–P10 from the Rosetta benchmark [67]. Subjects up to 465 LOC (vs. 200 for MachSuite, 100 for Intel t2sp).

**Chosen task for simplified reproduction: P6 — matrix multiplication (19 LOC).**
- **Why:** smallest subject by far (19 LOC vs. next-smallest P2 at 24), fewest edits (16 ΔLOC), simplest kernel shape, and the most generated tests (14,896) with 100% branch coverage — i.e. the easiest to write from scratch, the easiest to validate by differential testing, and the natural home for the requested ingredients (malloc'd matrices + pointer indexing + variable loop bounds `n`).
- **Caveat (explicitly unverified):** the paper does **not** itemize which of the six error types each benchmark exhibits, so I cannot verify from the paper that P6 specifically contains malloc/pointer/variable-bound errors — that is an inference from "matrix multiplication kernel" + its 16 edit lines. **Fallback if recursion is required:** P3 merge sort (121 LOC) — the paper *does* verify it needs recursion→stack conversion (Fig. 8) and is the canonical malloc+recursion case; or P5 graph traversal (85 LOC), the Fig. 2 worked example with all three (malloc, pointer, recursion) verified in the text.

---

## 5. Test-input generation (§4, Algorithm 1) — one paragraph

Coverage-guided fuzzing built on **AFL 2.52b**, but targeting the *kernel function only* rather than the end-to-end program. It first runs the host program on sample inputs and captures the actual argument values arriving at the kernel entry (`getKernelSeed`) — these real intermediate states, guaranteed valid, seed the fuzzing queue instead of handcrafted random seeds. It then mutates queued inputs (with a type checker derived from the kernel's argument types, so mutated inputs stay valid for HLS data types), executes the kernel, and keeps an input iff it yields **new branch coverage** (`NewCov` feedback). Fuzzing stops after 30 minutes without a new path. Result: on average **2,437 tests per application, 97% branch coverage** (vs. 36% from pre-existing tests); the generated tests are load-bearing — e.g. in P3, pre-existing tests accepted a stack size of 1024, but generated tests showed 44% behavioral divergence, forcing the size to 2048 (§6.2).

---

## 6. Argus — what it is and what's transferable to C

Argus (`ucr-riple/Argus`, author Zi Yang) is a **Python** random code-mutation engine for mutation testing / robustness studies — *not* an HLS repair tool. It mutates Python ASTs; nothing in it is C-specific. Surveyed files: `random_mutator.py` (~3,000 lines), `utils/ast_graph.py`, `utils/coverage_manager.py`, `cli.py`, `README.md`.

**Mutation operators** (11 classes; CLI names in parentheses):
`ArithmeticOperatorMutator` (+−*/ swap), `VariableRenameMutator` (scope-consistent local rename, excl. parameters), `ParamRenameMutator` + `FunctionRenameMutator` (rename param/function **with same-file call-site keyword sync**, e.g. `vip=True` → `mutated_vip_1234=True`), `StatementDeletionMutator`, `ComparisonOperatorMutator` (==, !=, <, >), `BooleanConditionFlipMutator` (`if c` → `if not c`), `LoopBoundaryMutator` (`range(n)` → `range(n±1)`), `FunctionCallParameterMutator`, `KeywordArgValueMutator` (bool flip / small numeric tweak of a keyword arg), `AttributeRenameMutator` (consistent rewrite within resolvable scope), `DataStructureMutator` (List↔Tuple, Set→List, dict key/value swap, with type guards), `NumberSignFlipMutator`.

**`utils/ast_graph.py` — AST-as-graph model:** `build_ast_graph(source)` parses with `ast.parse`, assigns stable integer node IDs in **pre-order traversal**, and returns `ASTGraph{nodes: [{id, type, lineno, col_offset, snippet (ast.unparse, ≤120 chars)}], edges: [{source, target}] (parent→child only), lineno_to_node_ids}`. `export_graph_json` adds a `mutation_path` (list of node IDs) for visualization (`graph.html`). Note: edges are **purely syntactic parent–child** — there is no data-flow, control-flow, or dependence edge in this graph.

**Prioritization (layered, in `MutationGraph.random_walk`, `random_mutator.py:2701`):** per step, applicable operators are filtered in this order — (1) **dependency-guided** (`DependencyGuidedStrategy`, `--dep-priority`): user supplies identifier names (function/var/attribute/keyword, case-insensitive); operators whose `target_nodes`/`candidates` match a dependency (via subtree scan of `Name`/`Attribute`/`FunctionDef`/`ClassDef`/`arg`/`keyword`/`alias`) are **restricted to that subset**; if no hit, fall back to the full set. (2) **Coverage-guided** (`CoverageGuidedStrategy`): operators whose target nodes lie on executed lines (from `coverage.py` JSON) get score multiplied by `1 + 0.5 × #covered targets`, then sorted descending. (3) **Semantic-aware** (`SemanticAwareStrategy`): keeps operators with meaningfulness score > 0.3 (base 0.5, +0.3–0.4 if the code actually contains arithmetic/conditionals/loops matching the operator type). Final choice is **weighted random** by operator `weight`. This is a *soft ranking + random pick*, not HeteroGen's hard dependence-DAG enumeration.

**Validation / rollback:** each step deep-copies the AST as backup; after applying an operator, the mutant is unparsed and checked by `CodeValidator`: **syntax** (`ast.parse`) then **semantics** (`SemanticValidator`: undefined variables/functions, scope-aware) — on failure the step is **rolled back** to the backup and the mutation record popped, and the walk continues. Optional `--exec-check` runs the code in a restricted `exec` sandbox (whitelisted builtins, 5 s) and writes a `*.fail.json` report on failure. `smart_mutate_code` = coverage + semantic strategies together.

**Transferable ideas for the C reproduction (not Argus's Python code):**
1. **Layered candidate filtering** (dependency → coverage → semantic → weighted random) is a cheap, implementable ordering policy for parameterized C edits — HeteroGen's DAG gives *hard* precedence; Argus's layers give *soft* priority, and the two compose naturally (DAG first, then Argus-style scoring within the frontier).
2. **Dependency = identifier affinity:** for C, a "dependency" can be a variable/function name (e.g. `buf`, `traverse`); restrict the edit frontier to edits whose AST target subtree mentions it — directly analogous to Argus's `match_dependencies` subtree scan, and simpler than HeteroGen's error-message localization.
3. **Backup-and-rollback per step** (deep-copy AST before each edit; validate; revert on failure) — Argus does this with `copy.deepcopy` + `ast.unparse`; the C equivalent is libclang/`pycparser` AST copy + `gcc -fsyntax-only` / compile check.
4. **Coverage as a soft score** (`1 + 0.5·covered_targets`) to prefer edits on executed lines — the C analogue is `gcov`/`llvm-cov` executed-line sets feeding the same formula.
5. **AST-graph export with mutation path** (`ast_graph.py`: pre-order IDs, parent–child edges, per-node snippet, `lineno → node ids`) — a ready-made visualization schema for showing *which* AST nodes a chain of C edits touched; for C, add a second edge type for HeteroGen-style *edit dependences* (Argus's graph lacks these — that is the gap the reproduction should fill).
6. **Transaction safety on renames** (param rename ⇒ call-site keyword sync in the same file) generalizes to C as: renaming a variable/function must rewrite all uses consistently — a useful invariant for the `pointer()` / `type_trans()` edits.

**What Argus does *not* have (do not copy the assumption):** no notion of an external oracle (no HLS compiler, no differential testing, no fitness function), no dependence DAG between operators, no early-rejection checker, and its "graph" is a tree, not a dependence graph. Its validation is syntactic/semantic only — behavior preservation is *not* checked.

---

## 7. Gaps / explicitly unverified items
- Per-benchmark error-type breakdown (which of the six types P6 exhibits) is **not in the paper** — the P6 choice rests on size/simplicity, not a verified error profile.
- The exact Fig. 3 pie-chart percentages for dataflow / loop-parallelization / top-function could not be reliably extracted (PDF layout interleaving); only the prose figures (~25% types, 14% struct/union, 8% dynamic) are quoted.
- HeteroGen's GitHub repo (`UCLA-SEAL/HeteroGen`) was cited but not cloned/inspected in this brief; its actual edit-implementation details (LLVM 8.0-based) were not verified beyond the paper.
- Argus's `demo_walk_engine.py` / `graph.html` rendering was not read in detail; only `ast_graph.py`'s data model and the `random_walk` loop were verified at source level.