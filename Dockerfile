# syntax=docker/dockerfile:1

# The image carries the server and nothing else. Boards are never copied in —
# they are bind-mounted read-only at /srv/boards, so refreshing an export is a
# file operation on the host and never a rebuild.

FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Freeform boards" \
      org.opencontainers.image.description="Static server for exported Apple Freeform boards"

# Nothing is installed: the server is standard library only. Removing pip and
# setuptools leaves an interpreter with no way to fetch code, which together
# with a read-only root filesystem means the container cannot acquire anything
# it did not ship with.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip* \
           /usr/local/lib/python3.*/site-packages/setuptools* \
           /usr/local/lib/python3.*/site-packages/pkg_resources* \
           /usr/local/lib/python3.*/site-packages/wheel* \
           /usr/local/bin/pip* /root/.cache \
 && addgroup -g 10001 -S board \
 && adduser -u 10001 -S -G board -H -h /nonexistent -s /sbin/nologin board \
 && mkdir -p /srv/boards

# Owned by root and read-only: the unprivileged user that runs the server
# cannot rewrite the server. chmod is a separate step rather than COPY --chmod
# so that this builds under the classic builder as well as BuildKit.
COPY --chown=root:root serve.py /app/serve.py
RUN chmod 0444 /app/serve.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FREEFORM_ROOT=/srv/boards \
    FREEFORM_HOST=0.0.0.0 \
    FREEFORM_PORT=8080 \
    FREEFORM_ASSET_MAX_AGE=300

USER 10001:10001
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=2s --retries=3 \
  CMD ["python3", "-c", "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "/app/serve.py"]
