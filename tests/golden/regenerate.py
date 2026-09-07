"""Regenerate the golden dumps in this directory.

The goldens are the whole public surface of ONE single-product deployment --
`serverInfo`, `instructions`, every tool as `tools/list` serialises it, and
/health -- captured over raw ASGI, the way a host drives the app.

They were taken from the code as it stood BEFORE host routing existed, with
`MCP_PRODUCTS` set, which is the point: `tests/test_host_routing.py` builds one
combined process and asserts that each hostname still serves exactly what its
own deployment served. That is the whole promise of the merge -- two Smithery
listings, two official-registry entries and two RapidAPI subscriptions describe
distinct tool sets, and a tool that gains or loses a parameter description on
the way through is a listing that has quietly stopped being accurate.

So: regenerate ONLY when the tool surface is meant to change, and say so in the
commit message. A golden that moves silently is the failure this file exists to
make loud.

    cd mcp_server_paid
    MCP_PRODUCTS=flights MCP_PUBLIC_URL=https://flights.golden.test/mcp \
        python tests/golden/regenerate.py flights
    MCP_PRODUCTS=hotels MCP_PUBLIC_URL=https://hotels.golden.test/mcp \
        python tests/golden/regenerate.py hotels

The `*.golden.test` hostnames are placeholders, not the real aliases: every URL
in the dump is derived from MCP_PUBLIC_URL, and a golden carrying the live
hostnames would go stale the day an alias changes for reasons that have nothing
to do with the tool surface.
"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "golden", "version": "1.0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def first_json_payload(body: str) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    raise AssertionError(f"no JSON payload in response body: {body!r}")


async def capture(app, host: str) -> dict:
    """initialize + tools/list + /health, over raw ASGI, as one dict."""
    headers = dict(HEADERS)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://{host}"
        ) as client:
            initialized = await client.post("/mcp", json=INITIALIZE, headers=headers)
            session = initialized.headers.get("mcp-session-id")
            if session:
                headers["mcp-session-id"] = session
            listed = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
            health = await client.get("/health")

    init = first_json_payload(initialized.text)["result"]
    return {
        "serverInfo": init["serverInfo"],
        "instructions": init.get("instructions"),
        "tools": first_json_payload(listed.text)["result"]["tools"],
        "health": health.json(),
    }


def path_for(product: str) -> pathlib.Path:
    return HERE / f"single_deployment_{product}.json"


def dump(document: dict, destination: pathlib.Path) -> None:
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    product = sys.argv[1]
    import api.index as entrypoint

    document = asyncio.run(capture(entrypoint.app, f"{product}.golden.test"))
    dump(document, path_for(product))
    print(path_for(product), len(document["tools"]), document["serverInfo"]["name"])
