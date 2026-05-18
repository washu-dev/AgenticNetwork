from datetime import datetime, timezone
from typing import Any

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    TransportProtocol,
)
from agntcy_app_sdk.directory.oasf_converter import (
    CARD_SCHEMA_VERSION,
    MODULE_ID_A2A,
    MODULE_NAME_A2A,
    OASF_SCHEMA_VERSION,
    agent_card_to_oasf,
)

SCOUT_AGENT_NAME_DEFAULT = "Scout"

# OASF taxonomy skill IDs (from https://github.com/agntcy/oasf/tree/main/schema/skills).
# Encoding: top_uid * 10000 + sub_uid * 100 + leaf_uid for 3-level skills,
#           top_uid * 100 + leaf_uid for 2-level skills.
# These IDs are REQUIRED for the directory to emit /skills/<path>/<leaf> labels
# on our record — without them, dir indexer can't classify us.
SCOUT_OASF_SKILLS: list[dict[str, Any]] = [
    # NLP / Information retrieval & synthesis — Scout's core
    {
        "name": "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        "id": 10306,
    },
    {
        "name": "natural_language_processing/information_retrieval_synthesis/fact_extraction",
        "id": 10301,
    },
    {
        "name": "natural_language_processing/information_retrieval_synthesis/knowledge_synthesis",
        "id": 10303,
    },
    # NLP / NLG — the brief output
    {
        "name": "natural_language_processing/natural_language_generation/summarization",
        "id": 10202,
    },
    # NLP / Analytical reasoning — pattern + trend inference
    {
        "name": "natural_language_processing/analytical_reasoning/inference_deduction",
        "id": 10701,
    },
    # RAG / Retrieval of information — web/news document pulls
    {
        "name": "retrieval_augmented_generation/retrieval_of_information/document_retrieval",
        "id": 60103,
    },
]

SCOUT_SKILLS: list[AgentSkill] = [
    AgentSkill(
        id="research/open-source-intelligence",
        name="Open-source intelligence research",
        description=(
            "Multi-step OSINT research. Given a region, topic, or entity, autonomously "
            "plans queries, collects from public web/news, and returns a cited markdown "
            "brief. Environment-centric (geopolitical / economic / infrastructure / "
            "public sentiment). Does NOT perform adversary attribution or threat "
            "assessment — those belong to a separate Intelligence agent."
        ),
        tags=["osint", "research", "analysis", "open-source"],
        examples=[
            "Summarize semiconductor export controls trends over the past 60 days.",
            "What is the current state of undersea cable infrastructure in the Indo-Pacific?",
            "Brief me on shipping disruptions near the Bab el-Mandeb Strait this month.",
        ],
    ),
    AgentSkill(
        id="analysis/situational-assessment",
        name="Situational assessment",
        description=(
            "Snapshot of the current state for a given topic or region using open sources. "
            "Returns who/what/where/when with provenance."
        ),
        tags=["analysis", "situational"],
    ),
    AgentSkill(
        id="analysis/trend-detection",
        name="Trend detection",
        description=(
            "Time-series and change-point analysis of public-source signals over a "
            "specified time window."
        ),
        tags=["analysis", "trend", "time-series"],
    ),
]


def build_scout_card(
    *,
    http_endpoint: str,
    agent_name: str = SCOUT_AGENT_NAME_DEFAULT,
    version: str = "0.2.0",
) -> AgentCard:
    """Build the Scout AgentCard advertising a JSONRPC HTTP endpoint.

    The lab's network uses JSONRPC over plain HTTP (Email Agent / Coding Agent
    both declare `preferred_transport=JSONRPC`). To be both callable BY them
    and able to call them, Scout exposes a JSONRPC interface and the client
    factory talks JSONRPC out.

    `http_endpoint` is the URL peers will use to reach Scout — must include
    scheme, host, port (e.g. `http://10.229.117.50:9104/`). On a NATed host,
    use the public/LAN IP, not 127.0.0.1, otherwise other machines can't
    reach Scout (this is the Coding Agent's bug we observed in the lab dir).
    """
    return AgentCard(
        name=agent_name,
        description="Environment-centric OSINT research & trend analysis agent.",
        version=version,
        url=http_endpoint,
        provider=AgentProvider(
            organization="CSPL Scout Agent",
            url="https://example.invalid/cspl-scout-agent",
        ),
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=SCOUT_SKILLS,
        preferredTransport=TransportProtocol.jsonrpc,
        additional_interfaces=[
            AgentInterface(
                transport=TransportProtocol.jsonrpc,
                url=http_endpoint,
            ),
        ],
    )


def build_scout_oasf_record(card: AgentCard) -> dict[str, Any]:
    """Build the OASF record dict to push to the directory.

    The SDK's `agent_card_to_oasf` hard-codes `skills=[{"id": 101}]` regardless
    of what the card declares — that's why our directory record had only
    `/modules/integration/a2a` and no `/skills/*` labels. We override `skills`
    with the OASF-canonical list so the dir indexer emits proper labels and
    skill-based discovery actually finds Scout.
    """
    record = agent_card_to_oasf(card)
    # Replace the hardcoded placeholder skills with our canonical list. We
    # also stamp `created_at` to "now" so cleanup_records_by_name can dedupe
    # by recency and the dir indexer sees a fresh record.
    record["skills"] = list(SCOUT_OASF_SKILLS)
    record["created_at"] = datetime.now(timezone.utc).isoformat()
    return record
