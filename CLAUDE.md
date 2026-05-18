# sunSale – development guidelines

## Code commenting standard

**All functions must have a docstring** — including private, static, and helper functions.

Use **Google-style Python docstrings**:

```python
def example(arg1: int, arg2: str) -> bool:
    """Brief one-line description.

    Args:
        arg1: What this argument represents.
        arg2: What this argument represents.

    Returns:
        What the return value means.

    Raises:
        ValueError: When and why this is raised (omit section if no exceptions).
    """
```

Rules:
- The opening line is a brief imperative statement (`Return …`, `Build …`, `Compute …`).
- Include `Args:`, `Returns:`, and `Raises:` sections for any function where they add value. Omit empty sections.
- For trivially obvious one-purpose functions (e.g. simple property getters), a single-line docstring is acceptable when the return type annotation makes the intent self-evident.
- For DAG `_compute()` overrides, a one-liner is sufficient when the parent class docstring already states the node's purpose.
- **Section comments** (e.g. `# --- Section name ---`) are acceptable to mark significant logical groups within a module. Keep them sparse.
- **Inline comments** should explain *why*, never *what*. Add one only when the logic is non-obvious, counter-intuitive, or works around a specific constraint.
- Do not write comments that merely restate what the code already says.
