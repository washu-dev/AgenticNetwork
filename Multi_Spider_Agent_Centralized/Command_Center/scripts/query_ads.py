"""Query ADS for registered agents."""

import json
import sys

from agntcy.dir_sdk.client import Client, Config
from agntcy.dir_sdk.models import search_v1
from google.protobuf.json_format import MessageToDict


def main():
    address = sys.argv[1] if len(sys.argv) > 1 else "localhost:8888"
    print(f"Querying ADS at {address} ...\n")

    config = Config(server_address=address)
    client = Client(config)

    search_req = search_v1.SearchRecordsRequest(limit=20)
    responses = client.search_records(search_req)

    count = 0
    for resp in responses:
        if not (resp.record and resp.record.data):
            continue
        record = MessageToDict(resp.record.data)
        count += 1

        name = record.get("name", "?")
        url = ""
        tags = []
        for module in record.get("modules", []):
            if module.get("name") == "integration/a2a":
                card = module.get("data", {}).get("card_data", {})
                url = card.get("url", "")
                for skill in card.get("skills", []):
                    tags.extend(skill.get("tags", []))

        print(f"[{count}] {name}")
        print(f"    URL:  {url}")
        print(f"    Tags: {', '.join(tags)}")
        print(f"    Time: {record.get('created_at', '?')}")
        print()

    if count == 0:
        print("No agents registered.")
    else:
        print(f"Total: {count} agent(s)")


if __name__ == "__main__":
    main()
