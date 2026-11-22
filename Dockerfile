FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv

WORKDIR /app

COPY . .

RUN uv pip install --system --no-cache .

ENTRYPOINT ["hbd"]
