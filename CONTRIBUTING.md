# Contributing to jamdock-gui

Thanks for considering a contribution. The intent is to keep the project
focused and the codebase tidy — please read the short notes below before
opening a pull request.

## Reporting bugs

Use the **Bug report** issue template. The "jamdock-gui version",
"OS / WSL" and "terminal output" fields are not optional — they save
several round-trips and let us reproduce the issue locally on the first
try.

If the problem is in the underlying bash scripts (`jamlib`,
`jamreceptor`, `jamqvina`, `jamrank`, `jamresume`), open the issue
against [jamdock-suite](https://github.com/jamanso/jamdock-suite/issues)
instead. We will close cross-posted issues.

## Suggesting features

Use the **Feature request** issue template. Suggestions backed by a
concrete biological or chemical use case are easiest to evaluate. We
particularly welcome ideas that close gaps with established
workflows — but a clean rejection because the feature is out of scope
is also a likely outcome, so don't take it personally.

## Development setup

```bash
git clone https://github.com/jamanso/jamdock-gui.git
cd jamdock-gui
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

You will also need [jamdock-suite](https://github.com/jamanso/jamdock-suite)
on your `$PATH` for the GUI to launch — see the main README for that.

## Running tests

```bash
pytest tests/ -v
```

The test suite only covers the pure-Python `core/` modules at the
moment; Qt widgets are exercised manually. If you add a non-GUI helper,
please add a test for it. If you add a GUI widget that has any
business logic (not just plumbing), please factor that logic out into
`core/` so it can be tested headlessly.

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check jamdock_gui tests
```

Configuration lives in `pyproject.toml`. The most important conventions:

- Type hints on every public function and on non-obvious private ones.
- `from __future__ import annotations` at the top of every module.
- Docstrings in NumPy style for anything that isn't entirely obvious
  from the signature — the existing modules in `core/` are good
  examples.
- Avoid one-letter variable names except for the obvious cases
  (`i`, `j`, `x`, `y`, `z` in geometry; `df` for a DataFrame).

## Pull-request workflow

1. Fork the repository and create a topic branch (`git checkout -b
   fix/short-description`).
2. Make your change. Include or update tests when relevant.
3. Run `ruff check` and `pytest` locally. CI will run them on the PR
   too but it is more polite to do it before pushing.
4. Open the PR against `main`. The PR description should explain
   *why* you are making the change at least as much as *what* it does.
5. A maintainer will review. Expect comments — the goal is to keep
   the codebase coherent, not to gatekeep.

## License

The project is distributed under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) for
consistency with [jamdock-suite](https://github.com/jamanso/jamdock-suite).
By submitting a pull request you confirm that your contribution is
compatible with that license.
