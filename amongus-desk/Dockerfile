# Paper / simulation mode only. Real UniTrader automation needs the Windows desktop session on the VPS
# (see docs/WINDOWS_VPS.md) and cannot run in a container.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 desk
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY config/desk.example.yaml ./config/desk.example.yaml
RUN mkdir -p /app/data && chown -R desk:desk /app
USER desk

# The dashboard must bind 0.0.0.0 inside the container, which requires DESK_DASHBOARD_TOKEN.
EXPOSE 8765
VOLUME ["/app/data"]
ENTRYPOINT ["desk"]
CMD ["run", "-c", "/app/config/desk.yaml"]
