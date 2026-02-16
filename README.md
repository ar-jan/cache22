# cache22

## Development

```sh
# Create .venv and install dependencies
uv sync --group dev
source .venv/bin/activate
# Check yaml:
yamllint docs/related-works.yaml
# Or run directly without activating .venv:
uv run --group dev yamllint docs/related-works.yaml
