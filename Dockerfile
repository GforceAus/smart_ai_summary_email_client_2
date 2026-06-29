FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --quiet

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY src/ ./src/
COPY main.py ./

# data/ is mounted as a volume — not baked into the image
