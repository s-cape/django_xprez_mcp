# AGENTS.md

Instructions for coding agents working in this repository.

## Repository context

- Reusable Django library `mcpkit` (PyPI `django-mcpkit`): admin-style MCP servers.
- `docs/design.md` is the source of truth for the API and the roadmap. Update it
  when behaviour or public API changes.
- The library knows nothing about the projects that use it: never name them
  in code, docs, tests or commit messages.

## Change discipline

- Keep changes narrowly focused on the request; no unrelated refactors.
- Preserve public behaviour unless the task asks for a breaking change.
- Core depends on Django only. Optional integrations (DRF, pydantic) are
  imported lazily.

## Python and Django conventions

- Follow Django coding style; keep code Black-compatible (`pyproject.toml`).
- Comments and docstrings concise, about non-obvious context.

## Tests

```bash
.venv/bin/python -m django test mcpkit.tests --settings=mcpkit.tests.settings
.venv/bin/black --check .
```
