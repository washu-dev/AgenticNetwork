# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — extract the dirctl binary from the official AGNTCY image
# ─────────────────────────────────────────────────────────────────────────────
FROM ghcr.io/agntcy/dir-ctl:v1.2.0 AS dirctl-src
# The binary lives at /dirctl (root of the filesystem) in the official image.

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — main image
# ─────────────────────────────────────────────────────────────────────────────
FROM continuumio/miniconda3:24.9.2-0

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── dirctl binary ─────────────────────────────────────────────────────────────
COPY --from=dirctl-src /dirctl /usr/local/bin/dirctl
RUN chmod +x /usr/local/bin/dirctl

# ── Repo source ───────────────────────────────────────────────────────────────
# Build context must be the repo root:
#   docker build -f deployment/Dockerfile -t agenticnetwork:latest .
WORKDIR /app

# ── Conda environments (one per agent) ───────────────────────────────────────
# Copy ONLY the dependency files first so that these expensive layers are
# cached independently of the rest of the source code. Changes to agent code
# will not invalidate the conda env layers.
RUN conda install -n base -c conda-forge mamba -y --quiet

COPY agent_directory_service/environment.yml agent_directory_service/environment.yml
RUN mamba env create -f agent_directory_service/environment.yml --quiet

COPY coding_agent/environment.yml coding_agent/environment.yml
RUN mamba env create -f coding_agent/environment.yml --quiet

COPY email_agent/environment.yml email_agent/environment.yml
RUN mamba env create -f email_agent/environment.yml --quiet

COPY intelligence_agent/environment.yml intelligence_agent/environment.yml
RUN mamba env create -f intelligence_agent/environment.yml --quiet

COPY disaster_response_agent/environment.yml disaster_response_agent/environment.yml
RUN sed 's/name: email-agent-a2a/name: disaster-agent-a2a/' \
        disaster_response_agent/environment.yml \
        > /tmp/disaster_env.yml \
    && mamba env create -f /tmp/disaster_env.yml --quiet

# Scout — needs pyproject.toml to install the package
COPY scout_agent/pyproject.toml scout_agent/pyproject.toml
COPY scout_agent/scout scout_agent/scout
RUN mamba create -n scout-agent python=3.12 pip -y --quiet \
    && /opt/conda/envs/scout-agent/bin/pip install --quiet \
        -e /app/scout_agent/

# GeoSpatial — needs requirements.txt
COPY geoSpatial_agent/attack_bundle/requirements.txt geoSpatial_agent/attack_bundle/requirements.txt
RUN mamba create -n geospatial-agent python=3.12 pip -y --quiet \
    && /opt/conda/envs/geospatial-agent/bin/pip install --quiet \
        --extra-index-url=https://buf.build/gen/python \
        -r /app/geoSpatial_agent/attack_bundle/requirements.txt \
        "agntcy-app-sdk>=0.4.6" \
        "agntcy-dir>=0.6.0" \
        "agntcy-oasf-sdk-grpc-python" \
        "agntcy-oasf-sdk-protocolbuffers-python"

# Planner — needs requirements.txt, Python 3.13
COPY planner_agent/requirements.txt planner_agent/requirements.txt
RUN mamba create -n planner-agent python=3.13 pip -y --quiet \
    && /opt/conda/envs/planner-agent/bin/pip install --quiet \
        -r /app/planner_agent/requirements.txt

# ── Full repo source (changes here won't bust the conda cache above) ──────────
COPY . .

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /tmp/ads /var/log/supervisor /var/run/supervisor

# ── Supervisord config ────────────────────────────────────────────────────────
COPY deployment/supervisord.conf /etc/supervisor/conf.d/agenticnetwork.conf

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY deployment/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Exposed ports ─────────────────────────────────────────────────────────────
# ADS gRPC          8888
# ADS gossipsub     8999
# Planner           8090
# Email agent       9103
# Scout agent       9104
# Intelligence      9105
# Coding agent      9106
# Disaster          9107  (reassigned — default was 9104, conflicted with Scout)
# GeoSpatial        9108  (reassigned — default was 9105, conflicted with Intelligence)
EXPOSE 8888 8999 8090 9103 9104 9105 9106 9107 9108

ENTRYPOINT ["/entrypoint.sh"]
