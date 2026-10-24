FROM python:3.14.2-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src
RUN uv sync --locked
CMD ["rt-trading", "v3-serve", "--host", "0.0.0.0"]
