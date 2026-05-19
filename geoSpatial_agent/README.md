# GeoSpatial Agent

This directory contains the standalone GeoSpatial Agent service. It wraps the existing DeepSearch-Geo pipeline to infer likely photo location and describe visible public-safety or disaster context such as flooding, tornado damage, fire, armed threat, chemical hazard, crash, or infrastructure damage.

## What It Does

- Infers likely image location with coordinates, precision, confidence, rationale, and alternatives.
- Assesses visible public-safety context in an image.
- Accepts local image paths, A2A file parts, and structured JSON image references.
- Exposes those capabilities through an A2A HTTP endpoint.
- Can use ADS discovery and outbound A2A calls only when started with `--enable-remote-agent-tools`.

## Setup

Use the shared conda environment from the email agent. It includes the geospatial `attack_bundle` requirements:

```bash
cd <project_root>/email_agent
conda env create -f environment.yml
conda activate email-agent-a2a
cd ../geoSpatial
```

Set the Google API key for Gemini image analysis:

```bash
export GOOGLE_API_KEY="your-google-api-key"
```

If remote agent coordination is enabled, also set the OpenAI API key for the coordination planner. `OPENAI_API_KEY` is also needed if you choose a GPT model for scene assessment:

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

`GOOGLE_MAPS_API_KEY` is optional. If it is not set, the geospatial bundle falls back to `GOOGLE_API_KEY` for Maps checks.

## Run

Start the GeoSpatial Agent service:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools
```

Set the hosted port:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools --host 0.0.0.0 --port 9105
```

Set the ADS server address:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools --ads-url 10.229.117.37:8888
```

Start without publishing to ADS, useful when ADS is offline:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools --no-publish-ads
```

The Agent Card should point to the reachable host and port, for example:

```text
http://10.229.117.223:9105/
```

Use `--advertise-url` when the externally reachable URL differs from the card.

## Models

The default geolocation model is `gemini-3-flash-preview`. Change it with `--geo-model`:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools --geo-model gemini-2.5-pro
```

The default scene assessment model is also `gemini-3-flash-preview`. Change it with `--scene-model`:

```bash
python3 geospatial_agent.py serve-a2a --enable-remote-agent-tools --scene-model gemini-2.5-flash
```

For local analysis, use the same flags:

```bash
python3 geospatial_agent.py analyze --image /path/to/photo.jpg --geo-model gemini-2.5-pro --scene-model gemini-2.5-flash
```

Remote coordination uses the internal coordination model configured in code. It requires `OPENAI_API_KEY` when `--enable-remote-agent-tools` is used and the request asks for peer-agent help.

## ADS And Remote Agents

`serve-a2a` publishes the agent card to ADS by default. Use `--ads-url` to choose the ADS server and `--no-publish-ads` to skip publication.

`--enable-remote-agent-tools` controls runtime access to ADS discovery and outbound A2A request tools. Without that flag, the geospatial analysis runs locally and does not call peer agents.

## A2A Inputs

The agent accepts any of:

- JSON/data payload: `{"image_path": "/abs/path/photo.jpg"}`
- JSON/data payload: `{"local_image_path": "/abs/path/photo.jpg"}`
- A2A file part with a local `uri`
- A2A file part with base64 bytes
- Text containing `image: /abs/path/photo.jpg`

Useful skill hints:

- `geospatial/analyze_image`
- `geospatial/location_inference`
- `geospatial/incident_assessment`
- `image/geospatial_analysis`
