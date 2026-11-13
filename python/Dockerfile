FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Copy the project files
COPY . /app

# Install dependencies including the optional `mcp` extra (frozen to lockfile, no dev dependencies)
RUN uv sync --frozen --extra mcp --no-dev

# Start the MCP server using the entrypoint defined in pyproject.toml
ENTRYPOINT ["uv", "run", "adeu-server"]