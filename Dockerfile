# Runs the live watch (or any module) on a server.
#   docker build -t integrity-analyst .
#   docker run -d -v $PWD/data:/app/data -e GRIB_ANTHROPIC_API_KEY -e GRIB_ALERT_WEBHOOK_URL \
#       integrity-analyst                                   # both live watches (Polymarket, Kalshi), every 3 hours
#   docker run --rm -v $PWD/data:/app/data integrity-analyst python -m pipeline
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
VOLUME ["/app/data"]
CMD ["python", "-m", "watch.run_all", "--every", "3"]
