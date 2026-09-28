# Contributing

Stonks is source-available under an all-rights-reserved [license](LICENSE). Issues and pull requests are welcome; by opening one you agree that the owner may use your change.

## Set up

```bash
uv sync                           # Python 3.12+
cd web && npm ci                  # Node 22+
```

## Before you open a pull request

- **Tests first.** Write a failing test, then the code. `uv run pytest -n auto` and `cd web && npm test` must pass.
- **Lint and format.** `uv run ruff check . && uv run ruff format .`, `cd web && npm run lint && npm run format`. To run ruff on every commit, `uvx pre-commit install` once (`.pre-commit-config.yaml`).
- **Generated files.** After changing API routes or MCP tools, regenerate them (see [CLAUDE.md](CLAUDE.md), Commands) and commit the result.
- **No secrets.** Keys and tokens live in `.env`, never in code, config or tests. Push protection blocks known secret formats.
- **Principles.** Changes respect [docs/principles.md](docs/principles.md).

CI runs tests, lint, `pip-audit`, `npm audit`, CodeQL, an image build, and checks of the deploy files. Accepted audit findings go in `.github/audit/` with a reason and a review date.

## Commit messages

One line in the imperative, starting with a verb: `Add ...`, `Fix ...`, `Remove ...`, `Document ...`. The changelog sorts commits into sections by that first word (`cliff.toml`).

## Releases (owner)

Versions follow [Semantic Versioning](https://semver.org): breaking change = major, feature = minor, fix = patch.

```bash
# 1. bump version in pyproject.toml and src/stonks/__init__.py
# 2. list the commits since the last tag, then write the "## [1.2.3]"
#    section of CHANGELOG.md by hand, grouped by area
git cliff --unreleased
git commit -am "Release v1.2.3"
git tag v1.2.3 && git push origin main v1.2.3
```

The tag builds the image, publishes a GitHub release with that CHANGELOG section (or the `git cliff` list when there is none) and deploys it ([docs/deploy.md](docs/deploy.md)).

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
