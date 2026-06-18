# GeoSpatial agent (photo geolocation + incident classification) — port 19108
# Build from repo root:
#   docker build --platform linux/amd64 -f deployment/dockerfiles/Dockerfile.geospatial \
#       -t amaankhan25/agenticnetwork-geospatial:latest .
FROM ghcr.io/agntcy/dir-ctl:v1.2.0 AS dirctl-src

FROM continuumio/miniconda3:24.9.2-0

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dirctl-src /dirctl /usr/local/bin/dirctl
RUN chmod +x /usr/local/bin/dirctl

WORKDIR /app

RUN conda install -n base -c conda-forge mamba -y --quiet

COPY geoSpatial_agent/attack_bundle/requirements.txt geoSpatial_agent/attack_bundle/requirements.txt
RUN mamba create -n geospatial-agent python=3.12 pip -y --quiet \
    && /opt/conda/envs/geospatial-agent/bin/pip install --quiet \
        --extra-index-url=https://buf.build/gen/python \
        -r /app/geoSpatial_agent/attack_bundle/requirements.txt \
        "agntcy-app-sdk>=0.4.6" \
        "agntcy-dir>=0.6.0" \
        "agntcy-oasf-sdk-grpc-python" \
        "agntcy-oasf-sdk-protocolbuffers-python"

COPY geoSpatial_agent/ geoSpatial_agent/
COPY ads_utils/ ads_utils/
COPY deployment/entrypoints/entrypoint-geospatial.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH=/opt/conda/envs/geospatial-agent/bin:$PATH
ENV PYTHONPATH=/app

EXPOSE 19108

ENTRYPOINT ["/entrypoint.sh"]
