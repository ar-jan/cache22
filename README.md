# cache22

## Development

```sh
# Create .venv and install dependencies
uv sync --group dev
# Install git hooks for this repo
uv run --group dev pre-commit install
# Check yaml:
uv run --group dev yamllint docs/related-works.yaml
# Or run all pre-commit hooks:
uv run --group dev pre-commit run --all-files
```
