import sys

from a2a.types import AgentCard
from agntcy_app_sdk.directory.dir import AgentDirectory


async def open_directory(endpoint: str) -> AgentDirectory:
    """Open and return a connected AgentDirectory client."""
    client = AgentDirectory.from_config(endpoint=endpoint)
    await client.setup()
    return client


async def close_directory(client: AgentDirectory) -> None:
    try:
        await client.teardown()
    except Exception as e:
        print(f"[scout.directory] teardown warning: {e}", file=sys.stderr)


async def publish_self(client: AgentDirectory, card: AgentCard) -> str:
    """Two-step publish: push to OCI store, then announce CID to the routing DHT.

    Mirrors the `dirctl push` + `dirctl routing publish $CID` flow. Without the
    second step the record exists in the directory's content store but is NOT
    discoverable via routing/search from other peers.
    """
    cid = await client.push_agent_record(card)
    await _routing_publish(client, cid)
    return cid


async def _routing_publish(client: AgentDirectory, cid: str) -> None:
    """Announce a CID to the directory's routing layer (gossip/DHT)."""
    import asyncio

    from agntcy.dir.core.v1 import record_pb2 as core_v1
    from agntcy.dir.routing.v1 import routing_service_pb2 as routing_v1

    # AgentDirectory exposes the underlying gRPC client via `_ensure_client()`.
    grpc_client = client._ensure_client()  # type: ignore[attr-defined]

    ref = core_v1.RecordRef(cid=cid)
    req = routing_v1.PublishRequest(record_refs=routing_v1.RecordRefs(refs=[ref]))
    await asyncio.to_thread(grpc_client.publish, req)
    print(f"[scout.directory] announced CID {cid} to routing layer", file=sys.stderr)


async def unpublish_cid(client: AgentDirectory, cid: str) -> None:
    """Reverse of publish_self: routing-unpublish then delete from store.

    Tolerant of failures — a partial cleanup is better than refusing to exit.
    Each step is logged separately so you can see where (if anywhere) it failed.
    """
    import asyncio

    from agntcy.dir.core.v1 import record_pb2 as core_v1
    from agntcy.dir.routing.v1 import routing_service_pb2 as routing_v1

    grpc_client = client._ensure_client()  # type: ignore[attr-defined]
    ref = core_v1.RecordRef(cid=cid)

    # Step 1: remove from routing/DHT (so other peers stop discovering it)
    try:
        req = routing_v1.UnpublishRequest(record_refs=routing_v1.RecordRefs(refs=[ref]))
        await asyncio.to_thread(grpc_client.unpublish, req)
        print(f"[scout.directory] unpublished CID {cid}", file=sys.stderr)
    except Exception as e:
        print(
            f"[scout.directory] unpublish failed (cid={cid}): {e}", file=sys.stderr
        )

    # Step 2: delete from OCI store
    try:
        await asyncio.to_thread(grpc_client.delete, [ref])
        print(f"[scout.directory] deleted CID {cid} from store", file=sys.stderr)
    except Exception as e:
        print(f"[scout.directory] delete failed (cid={cid}): {e}", file=sys.stderr)


async def cleanup_records_by_name(
    client: AgentDirectory, agent_name: str, *, limit: int = 50
) -> int:
    """Find every record whose OASF name == ``agent_name`` and unpublish+delete it.

    Used at Scout startup so a previous unclean shutdown (or a previous test
    run on a stale URL) doesn't leave ghost records in the dir. Returns the
    count of records cleaned up.
    """
    import asyncio

    from agntcy.dir.search.v1 import record_query_pb2 as srq
    from agntcy.dir.search.v1 import search_service_pb2 as sv

    grpc_client = client._ensure_client()  # type: ignore[attr-defined]
    queries = [srq.RecordQuery(type=srq.RECORD_QUERY_TYPE_NAME, value=agent_name)]
    req = sv.SearchCIDsRequest(queries=queries, limit=limit)
    try:
        responses = await asyncio.to_thread(grpc_client.search_cids, req)
    except Exception as e:
        print(
            f"[scout.directory] search_cids failed during cleanup: {e}",
            file=sys.stderr,
        )
        return 0

    cids = [r.record_cid for r in responses if r.record_cid]
    if not cids:
        return 0

    print(
        f"[scout.directory] found {len(cids)} stale record(s) named "
        f"{agent_name!r}; cleaning up...",
        file=sys.stderr,
    )
    for cid in cids:
        await unpublish_cid(client, cid)
    return len(cids)


async def discover_peers(
    client: AgentDirectory,
    *,
    skill_filters: list[str] | None = None,
    exclude_names: set[str] | None = None,
    limit: int = 100,
) -> list[AgentCard]:
    """Find peer agents in the directory, returning their AgentCards.

    Default behaviour (no skill_filters) enumerates every A2A-integrated record
    in the directory by querying ``MODULE_NAME=integration/a2a`` against the
    lower-level ``search_records`` gRPC. Every A2A agent embeds this module in
    its OASF record, so the query effectively returns "all peers". This is how
    we approximate "list everything" since the SDK's high-level
    ``list_agent_records`` is not yet implemented.

    When ``skill_filters`` is provided, those values are used to do additional
    skill-name-based searches and the result is the UNION of all matches —
    useful only if the caller wants to add narrow, skill-targeted lookups on
    top of the broad enumeration.
    """
    excludes = exclude_names or set()
    seen_keys: set[str] = set()
    cards: list[AgentCard] = []

    # 1. Broad enumeration via MODULE_NAME=integration/a2a
    for card in await _search_module_a2a(client, limit=limit):
        key = _card_key(card)
        if card.name in excludes or key in seen_keys:
            continue
        seen_keys.add(key)
        cards.append(card)

    # 2. Optional skill-based narrowing (additive — finds anything the broad
    #    enumeration missed, e.g. agents not declaring the A2A module).
    for skill in skill_filters or []:
        try:
            extras = await _search_skill(client, skill, limit=limit)
        except Exception as e:
            print(
                f"[scout.directory] skill search failed for {skill!r}: {e}",
                file=sys.stderr,
            )
            continue
        for card in extras:
            key = _card_key(card)
            if card.name in excludes or key in seen_keys:
                continue
            seen_keys.add(key)
            cards.append(card)

    return cards


async def _search_module_a2a(client: AgentDirectory, *, limit: int) -> list[AgentCard]:
    """Enumerate all records that declare the integration/a2a module."""
    import asyncio

    from agntcy.dir.search.v1 import record_query_pb2 as srq
    from agntcy.dir.search.v1 import search_service_pb2 as sv
    from google.protobuf.json_format import MessageToDict

    grpc_client = client._ensure_client()  # type: ignore[attr-defined]
    queries = [srq.RecordQuery(type=srq.RECORD_QUERY_TYPE_MODULE_NAME, value="integration/a2a")]
    req = sv.SearchRecordsRequest(queries=queries, limit=limit)

    try:
        responses = await asyncio.to_thread(grpc_client.search_records, req)
    except Exception as e:
        print(f"[scout.directory] search_records failed: {e}", file=sys.stderr)
        return []

    cards: list[AgentCard] = []
    for resp in responses:
        try:
            oasf_dict = MessageToDict(resp.record.data, preserving_proto_field_name=True)
        except Exception as e:
            print(f"[scout.directory] record decode failed: {e}", file=sys.stderr)
            continue
        card = _oasf_to_card(oasf_dict)
        if card is not None:
            cards.append(card)
    return cards


async def _search_skill(
    client: AgentDirectory, skill_value: str, *, limit: int
) -> list[AgentCard]:
    """Search by skill id or name (additive narrowing)."""
    import asyncio

    from agntcy.dir.search.v1 import record_query_pb2 as srq
    from agntcy.dir.search.v1 import search_service_pb2 as sv
    from google.protobuf.json_format import MessageToDict

    grpc_client = client._ensure_client()  # type: ignore[attr-defined]
    queries = [
        srq.RecordQuery(type=srq.RECORD_QUERY_TYPE_SKILL_NAME, value=skill_value),
    ]
    req = sv.SearchRecordsRequest(queries=queries, limit=limit)
    try:
        responses = await asyncio.to_thread(grpc_client.search_records, req)
    except Exception as e:
        print(f"[scout.directory] search_records(skill) failed: {e}", file=sys.stderr)
        return []

    cards: list[AgentCard] = []
    for resp in responses:
        try:
            oasf_dict = MessageToDict(resp.record.data, preserving_proto_field_name=True)
        except Exception as e:
            print(f"[scout.directory] record decode failed: {e}", file=sys.stderr)
            continue
        card = _oasf_to_card(oasf_dict)
        if card is not None:
            cards.append(card)
    return cards


def _oasf_to_card(oasf_dict: dict) -> AgentCard | None:
    """Convert OASF dict to AgentCard, swallowing decode errors."""
    try:
        from agntcy_app_sdk.directory.oasf_converter import oasf_to_agent_card

        return oasf_to_agent_card(oasf_dict)
    except Exception as e:
        print(f"[scout.directory] oasf->card conversion failed: {e}", file=sys.stderr)
        return None


def _card_key(card: AgentCard) -> str:
    """Deduplication key — same name+version+first-iface = same logical agent."""
    iface_url = ""
    if card.additional_interfaces:
        iface_url = card.additional_interfaces[0].url or ""
    return f"{card.name}|{card.version}|{iface_url}"


