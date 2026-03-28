# Contributing to casectl

## Development Setup

```bash
git clone https://github.com/cadfan/casectl.git
cd casectl
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.11+. Hardware dependencies are not needed for development --
tests use mock fixtures, so no Pi or I2C bus is required.

To install everything (hardware, TUI, dev tools):

```bash
pip install -e ".[all]"
```

## Running Tests

```bash
pytest                       # all tests
pytest --cov=casectl         # with coverage
pytest -x --tb=short         # stop on first failure
```

## Code Style

- **Linting:** `ruff check src/`
- **Type checking:** `mypy src/casectl/`
- **Line length:** 100 characters
- **Target:** Python 3.11+

Ruff rules: E, F, I, N, W, UP (see `pyproject.toml` for details).

All I2C and hardware calls must go through `asyncio.to_thread()` -- never
block the event loop.

## Making Changes

1. Fork the repo and create a feature branch from `master`.
2. Make your changes. Keep commits focused on one thing.
3. Add or update tests for any new/changed behaviour.
4. Run `pytest`, `ruff check src/`, and `mypy src/casectl/` before pushing.
5. Open a pull request against `master`.

## Plugin Development

casectl uses entry-point-based plugin discovery. See the example plugin at
[`examples/plugins/casectl-example-plugin/`](examples/plugins/casectl-example-plugin/)
for a working template with step-by-step documentation.

Full plugin docs: https://casectl.griffiths.cymru/

Plugins implement the `CasePlugin` protocol (structural subtyping -- no
inheritance required) and register via a `pyproject.toml` entry point:

```toml
[project.entry-points."casectl.plugins"]
my-plugin = "my_package.plugin:MyPlugin"
```

## Project Layout

- `src/casectl/` -- main package (hardware, plugins, daemon, CLI, web, config)
- `tests/` -- pytest test suite with mock hardware fixtures

## Reporting Issues

Use the GitHub issue templates for [bug reports](.github/ISSUE_TEMPLATE/bug_report.md)
and [feature requests](.github/ISSUE_TEMPLATE/feature_request.md). For bugs,
include the output of `casectl doctor`.

## License

By contributing, you agree that your contributions will be licensed under MIT.
