# Result: PARTIAL RESULT

**Goal:** Review the code in https://github.com/karpathy/micrograd for bugs, correctness and design problems. Produce a prioritized list with evidence.

> **This result is incomplete.** Reasons:
> - s2 (Review nn.py and tests for correctness and coverage): unchecked - stopped: time limit reached (900s)
> - s3 (Review setup.py, README, and notebooks for design and documentation issues): unchecked - stopped: time limit reached (900s)
> - assembler unavailable (run: time limit reached (900s)); findings sorted by severity instead

## Findings (most important first)

### 1. __pow__ rejects Value exponents via assert  `high`  (s1.f1)
Line 36 asserts other is int/float, so Value**Value raises AssertionError instead of computing a gradient; __truediv__ (line 88) works only because -1 is a literal int, not a Value.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:36`: `assert isinstance(other, (int, float)), "only supporting int/float powers for now"`

### 2. Tests do not cover nn.py  `high`  (s2.f3) **[UNCHECKED - checker did not approve]**
test_engine.py only imports Value from engine; no tests exercise Neuron, Layer, MLP forward/backward, so nn.py correctness is unverified.

- Evidence (verified) `repos/karpathy__micrograd/test/test_engine.py:2`: `from micrograd.engine import Value`

### 3. backward() assumes a DAG; no cycle or multi-path guard  `medium`  (s1.f2)
build_topo uses a visited set so a cyclic graph silently drops edges, and gradients are accumulated with += (lines 18-19, 29-30) so multiple paths to a node are summed, but there is no explicit topological-order validation or cycle detection.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:60`: `if v not in visited:`

### 4. No Linear class in nn.py  `medium`  (s2.f1) **[UNCHECKED - checker did not approve]**
nn.py defines Neuron, Layer, MLP instead of a Linear class; forward is y = sum(wi*xi)+b, equivalent to x@w.T+b.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/nn.py:13`: `class Neuron(Module):`
- Evidence (verified) `repos/karpathy__micrograd/micrograd/nn.py:21`: `act = sum((wi*xi for wi,xi in zip(self.w, x)), self.b)`

### 5. Weight init unnormalized  `medium`  (s2.f2) **[UNCHECKED - checker did not approve]**
Weights drawn from uniform(-1,1) without scaling by fan-in (e.g. 1/sqrt(nin)), which can cause large activations/gradients for wide layers.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/nn.py:16`: `self.w = [Value(random.uniform(-1,1)) for _ in range(nin)]`

### 6. Missing edge cases in tests  `medium`  (s2.f4) **[UNCHECKED - checker did not approve]**
No tests for division by zero, negative exponents, or relu at zero; engine __truediv__ and __pow__ can produce inf/nan or assert failures untested.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:88`: `return self * other**-1`
- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:36`: `assert isinstance(other, (int, float)), "only supporting int/float powers for now"`

### 7. trace_graph.ipynb uses private API _prev and _op  `medium`  (s3.f2) **[UNCHECKED - checker did not approve]**
The notebook accesses Value._prev and Value._op directly, coupling documentation to internal implementation details that could break without notice.

- Evidence (verified) `repos/karpathy__micrograd/trace_graph.ipynb:34`: `for child in v._prev:`
- Evidence (verified) `repos/karpathy__micrograd/trace_graph.ipynb:51`: `if n._op:`

### 8. README example uses in-place += on Value objects  `medium`  (s3.f7) **[UNCHECKED - checker did not approve]**
The example mutates Values with += (e.g. c += c + 1), which is an unusual and potentially confusing API for an autograd engine and may not be intended for users.

- Evidence (verified) `repos/karpathy__micrograd/README.md:25`: `c += c + 1`

### 9. __pow__ numerical edge case at data=0 with negative exponent  `low`  (s1.f3)
self.data**other with other negative and self.data==0 raises ZeroDivisionError; the _backward also computes self.data**(other-1) which is undefined at 0 for non-positive exponents.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:40`: `self.grad += (other * self.data**(other-1)) * out.grad`

### 10. __truediv__ relies on __pow__ with literal -1  `low`  (s1.f4)
__truediv__ (line 88) and __rtruediv__ (line 91) delegate to __pow__ with -1, which passes the int/float assert, so division works, but the gradient path is indirect and inherits __pow__'s limitations.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:88`: `return self * other**-1`

### 11. grad not reset before backward; stale gradients accumulate  `low`  (s1.f5)
backward() sets self.grad=1 but does not reset grad to 0 for all other nodes in the graph, so calling backward() twice on the same graph accumulates stale gradients via += in _backward closures.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/engine.py:68`: `self.grad = 1`

### 12. Backward pass correctness relies on engine  `low`  (s2.f5) **[UNCHECKED - checker did not approve]**
nn.py has no explicit backward; gradients flow through Value ops. Correct only if engine ops are correct, which tests do verify for engine but not for nn modules.

- Evidence (verified) `repos/karpathy__micrograd/micrograd/nn.py:20`: `def __call__(self, x):`
- Evidence (verified) `repos/karpathy__micrograd/test/test_engine.py:26`: `assert xmg.grad == xpt.grad.item()`

### 13. setup.py declares no dependencies  `low`  (s3.f1) **[UNCHECKED - checker did not approve]**
setup.py has no install_requires, which is correct since micrograd has no third-party deps, but trace_graph.ipynb requires graphviz without documenting it in setup.py extras.

- Evidence (NOT VERIFIED) `repos/karpathy__micrograd/setup.py`: `install_requires=[],`
- Evidence (verified) `repos/karpathy__micrograd/trace_graph.ipynb:10`: `# pip install graphviz`

### 14. trace_graph.ipynb render call lacks extension  `low`  (s3.f3) **[UNCHECKED - checker did not approve]**
dot.render('gout') relies on graphviz default extension; the repo ships gout.svg, implying SVG output, but the code does not specify format explicitly in render.

- Evidence (verified) `repos/karpathy__micrograd/trace_graph.ipynb:100`: `dot.render('gout')`

### 15. setup.py missing install_requires for PyTorch used by tests  `low`  (s3.f4) **[UNCHECKED - checker did not approve]**
Tests require PyTorch as a reference, but setup.py declares no dependencies at all, so users must know to install it manually.

- Evidence (verified) `repos/karpathy__micrograd/setup.py:15`: `packages=setuptools.find_packages(),`
- Evidence (verified) `repos/karpathy__micrograd/README.md:65`: `To run the unit tests you will have to install [PyTorch](https://pytorch.org/), which the tests use as a reference for verifying the correctness of the calculated gradients.`

### 16. README references puppy.jpg image not present in repo  `low`  (s3.f5) **[UNCHECKED - checker did not approve]**
README embeds an image puppy.jpg that does not exist in the repository file listing, so the image will not render.

- Evidence (verified) `repos/karpathy__micrograd/README.md:4`: `![awww](puppy.jpg)`

### 17. README references moon_mlp.png not present in repo  `low`  (s3.f6) **[UNCHECKED - checker did not approve]**
README embeds moon_mlp.png which is not in the file listing, so the decision boundary image will not render.

- Evidence (verified) `repos/karpathy__micrograd/README.md:43`: `![2d neuron](moon_mlp.png)`

### 18. README microgpt explainer post date appears in the future  `low`  (s3.f8) **[UNCHECKED - checker did not approve]**
The README links to an explainer post dated 2026-02-12, which is a future date and likely a typo.

- Evidence (verified) `repos/karpathy__micrograd/README.md:47`: `See also the accompanying [explainer post](https://karpathy.github.io/2026/02/12/microgpt/) for a detailed walkthrough.`

## Subtasks

| id | title | status | attempts | note |
|---|---|---|---|---|
| s1 | Review engine.py for correctness and bugs | accepted | 1 | Reviewed engine.py; found a real bug in __pow__ (assertion blocks Value**Value), a design flaw in backward() (no DAG/mul |
| s2 | Review nn.py and tests for correctness and coverage | unchecked | 1 | stopped: time limit reached (900s) |
| s3 | Review setup.py, README, and notebooks for design and documentation issues | unchecked | 2 | stopped: time limit reached (900s) |

## Run stats

- LLM calls: 28/40  tokens: 62943/150000  seconds: 966.3/900
- Retries: 0  LLM errors: 0  injections flagged: 0  tool calls denied: 0
