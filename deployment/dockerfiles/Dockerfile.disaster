# Disaster Response agent (emergency response plans) — port 19107
# Build from repo root:
#   docker build --platform linux/amd64 -f deployment/dockerfiles/Dockerfile.disaster \
#       -t amaankhan25/agenticnetwork-disaster:latest .
FROM ghcr.io/agntcy/dir-ctl:v1.2.0 AS dirctl-src

FROM continuumio/miniconda3:24.9.2-0

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dirctl-src /dirctl /usr/local/bin/dirctl
RUN chmod +x /usr/local/bin/dirctl

WORKDIR /app

RUN conda install -n base -c conda-forge mamba -y --quiet

# disaster_response_agent/environment.yml is named email-agent-a2a (shared origin).
# In this single-purpose image there is no naming conflict, so we use it as-is.
COPY disaster_response_agent/environment.yml disaster_response_agent/environment.yml
RUN mamba env create -f disaster_response_agent/environment.yml --quiet

COPY disaster_response_agent/ disaster_response_agent/
COPY ads_utils/ ads_utils/
COPY deployment/entrypoints/entrypoint-disaster.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH=/opt/conda/envs/email-agent-a2a/bin:$PATH
ENV PYTHONPATH=/app

EXPOSE 19107

ENTRYPOINT ["/entrypoint.sh"]
