# Built by .github/workflows/release.yaml (one image per architecture, published to GHCR) and pulled by
# each house via `image:` in config.yaml. Base pinned by digest; Dependabot bumps tag and digest together.
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72
RUN pip install --no-cache-dir "websockets>=13,<16"
WORKDIR /app
COPY app/ /app/
# The AppArmor profile (apparmor.txt) allows writes only under /data: precompile the App's bytecode and stop
# Python from trying to write .pyc files at run time (each attempt would be an AppArmor denial).
RUN python -m compileall -q /app
ENV PYTHONDONTWRITEBYTECODE=1
LABEL \
    org.opencontainers.image.title="Housekeeper Agent" \
    org.opencontainers.image.description="Hourly read-only health check of this home; reports to the Housekeeper service." \
    org.opencontainers.image.source="https://github.com/MoriartySmartHouse/housekeeper-agent"
CMD ["python", "-u", "/app/agent.py"]
