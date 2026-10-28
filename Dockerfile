FROM python:3.12 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN pip install --no-cache-dir poetry
RUN poetry config virtualenvs.in-project true
COPY pyproject.toml poetry.lock ./
# Modification ici : on utilise --without dev ou on retire --no-dev
RUN poetry install --without dev --no-root --no-interaction --no-ansi

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv .venv/
COPY . .

EXPOSE 8085
CMD ["/app/.venv/bin/streamlit", "run", "app.py", "--server.port=8085", "--server.address=0.0.0.0"]