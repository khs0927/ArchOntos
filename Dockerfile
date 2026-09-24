FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps
RUN pip install --no-cache-dir .

CMD ["uvicorn", "apps.action:app", "--host", "0.0.0.0", "--port", "8005"]
