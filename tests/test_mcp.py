import json
import os
import sys

import anyio
import pytest

from conftest import ROOT, needs_ffmpeg

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _result(res):
    if getattr(res, "structuredContent", None):
        return res.structuredContent
    return json.loads(res.content[0].text)


@needs_ffmpeg
def test_mcp_stdio_end_to_end(tmp_path, media_server):
    env = {**os.environ, "RECLIP_DOWNLOAD_DIR": str(tmp_path / "dl"), "RECLIP_ALLOW_PRIVATE_URLS": "1"}
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "mcp_server.py")], env=env)

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"download_media", "get_download", "get_media_info",
                        "expand_playlist", "list_downloads"} <= tools

                res = _result(await session.call_tool(
                    "download_media", {"url": media_server + "/clip.mp4", "format": "mp3", "wait_seconds": 60}))
                assert res["status"] == "done", res
                assert os.path.isfile(res["file_path"]) and res["file_path"].endswith(".mp3")

                bad = _result(await session.call_tool("download_media", {"url": "ftp://nope"}))
                assert bad["error"]["code"] == "invalid_url"

                listed = _result(await session.call_tool("list_downloads", {}))
                assert listed["downloads"][0]["id"] == res["id"]

    anyio.run(scenario)
