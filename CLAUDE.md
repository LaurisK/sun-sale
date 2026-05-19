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

## Integration check coverage

Every pipeline module that consumes or produces data must have a corresponding deep-check in `tools/integration_check.py`. The check must validate:

- All data the module **consumes** — cross-checked against its upstream source (raw HA entity state, `snap.inputs`, or an upstream `snap.pipeline` key).
- All data the module **exposes** — every declared field in the debug API serialization, including aggregate totals (sums, counts) verified against per-slot values.

**Servicing modules** — those that only route or actuate without producing pipeline data (e.g. `event_router`, `InverterController`) — are exempt.

When adding a new pipeline module:
1. Expose its output in `debug_view.py` under `pipeline` (or `outputs` for final deliverables).
2. Add a `check_<module>()` function and result dataclass.
3. Add a `<Module>CheckWidget` and wire it into `_DEEP_CATS`, `IntegrationCheckApp`, and `compose()`.
