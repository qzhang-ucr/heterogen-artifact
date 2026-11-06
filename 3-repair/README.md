# Simplified HeteroGen — parameterized AST edits over a dependence graph

Simplified reproduction of [HeteroGen](https://dl.acm.org/doi/10.1145/3503222.3507748)
(Zhang, Wang, Xu, Kim, ASPLOS 2022): given a graph of parameterized edits,
apply AST editing iteratively to C programs, validating after every step.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt  # pycparser, pytest
.venv/bin/python run_demo.py                              # allocation (default)
.venv/bin/python run_demo.py --task recursion              # stack branch (default)
.venv/bin/python run_demo.py --task recursion --branch iterative
.venv/bin/python run_demo.py --task pointer
.venv/bin/python -m pytest tests/ -q
```
