# Scout agent (OSINT web research) — port 9104
# Build from repo root:
#   docker build --platform linux/amd64 -f deployment/dockerfiles/Dockerfile.scout \
#       -t amaankhan25/agenticnetwork-scout:latest .
FROM ghcr.io/agntcy/dir-ctl:v1.2.0 AS dirctl-src

FROM continuumio/miniconda3:24.9.2-0

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dirctl-src /dirctl /usr/local/bin/dirctl
RUN chmod +x /usr/local/bin/dirctl

WORKDIR /app

RUN conda install -n base -c conda-forge mamba -y --quiet

COPY scout_agent/pyproject.toml scout_agent/pyproject.toml
COPY scout_agent/scout scout_agent/scout
RUN mamba create -n scout-agent python=3.12 pip -y --quiet \
    && /opt/conda/envs/scout-agent/bin/pip install --quiet \
        -e /app/scout_agent/

COPY scout_agent/ scout_agent/
COPY deployment/entrypoints/entrypoint-scout.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH=/opt/conda/envs/scout-agent/bin:$PATH
ENV PYTHONPATH=/app

EXPOSE 9104

ENTRYPOINT ["/entrypoint.sh"]
