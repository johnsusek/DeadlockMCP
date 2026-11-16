# Starts the server for registry introspection only; real use needs the game on a Linux host (see README).
FROM python:3.15-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY deadlock_mcp deadlock_mcp
RUN pip install --no-cache-dir .
CMD ["python", "-m", "deadlock_mcp"]
