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
# Container config = the paper-mode example with the dashboard listening on all interfaces, so the
# published port works. A non-loopback dashboard requires DESK_DASHBOARD_TOKEN (24+ characters) at run time:
#   docker run -p 127.0.0.1:8765:8765 -e DESK_DASHBOARD_TOKEN=<random> -v desk-data:/app/data <image>
# Mount your own file over /app/config/desk.yaml to change anything else.
RUN python -c "import yaml, pathlib; \
cfg = yaml.safe_load(pathlib.Path('config/desk.example.yaml').read_text()); \
cfg['dashboard'].update(host='0.0.0.0', allowed_hosts=['127.0.0.1', 'localhost']); \
pathlib.Path('config/desk.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))" \
    && mkdir -p /app/data && chown -R desk:desk /app
USER desk

EXPOSE 8765
VOLUME ["/app/data"]
ENTRYPOINT ["desk"]
CMD ["run", "-c", "/app/config/desk.yaml"]
