import os
import sys
import types
import unittest
from unittest.mock import AsyncMock


asyncpg = types.ModuleType("asyncpg")
asyncpg.Pool = object
asyncpg.create_pool = AsyncMock()
sys.modules.setdefault("asyncpg", asyncpg)

pgvector = types.ModuleType("pgvector")
pgvector_asyncpg = types.ModuleType("pgvector.asyncpg")
pgvector_asyncpg.register_vector = AsyncMock()
pgvector.asyncpg = pgvector_asyncpg
sys.modules.setdefault("pgvector", pgvector)
sys.modules.setdefault("pgvector.asyncpg", pgvector_asyncpg)

bibtexparser = types.ModuleType("bibtexparser")
bibtexparser.loads = lambda _: types.SimpleNamespace(entries=[])
sys.modules.setdefault("bibtexparser", bibtexparser)

fastmcp = types.ModuleType("fastmcp")


class _FastMCP:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn

    def run(self, *args, **kwargs):
        return None


fastmcp.FastMCP = _FastMCP
sys.modules.setdefault("fastmcp", fastmcp)

mcp = types.ModuleType("mcp")
mcp_server = types.ModuleType("mcp.server")
mcp_server_auth = types.ModuleType("mcp.server.auth")
sys.modules.setdefault("mcp", mcp)
sys.modules.setdefault("mcp.server", mcp_server)
sys.modules.setdefault("mcp.server.auth", mcp_server_auth)

fastmcp_server = types.ModuleType("fastmcp.server")
fastmcp_server_auth = types.ModuleType("fastmcp.server.auth")
fastmcp_server_auth_providers = types.ModuleType("fastmcp.server.auth.providers")
fastmcp_server_auth_providers_jwt = types.ModuleType("fastmcp.server.auth.providers.jwt")


class _StaticTokenVerifier:
    def __init__(self, tokens):
        self.tokens = tokens


fastmcp_server_auth_providers_jwt.StaticTokenVerifier = _StaticTokenVerifier
sys.modules.setdefault("fastmcp.server", fastmcp_server)
sys.modules.setdefault("fastmcp.server.auth", fastmcp_server_auth)
sys.modules.setdefault("fastmcp.server.auth.providers", fastmcp_server_auth_providers)
sys.modules.setdefault("fastmcp.server.auth.providers.jwt", fastmcp_server_auth_providers_jwt)

os.environ.setdefault("DATABASE_URL", "postgresql://test/openbrain")


class ServerAuthTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self._clear_server_module()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._clear_server_module()

    @staticmethod
    def _clear_server_module():
        sys.modules.pop("openbrain.server", None)
        package = sys.modules.get("openbrain")
        if package is not None and hasattr(package, "server"):
            delattr(package, "server")

    def test_server_builds_bearer_token_auth_when_key_is_present(self):
        os.environ["MCP_ACCESS_KEY"] = "secret-token"
        os.environ["CADDY_HOSTNAME"] = "brain.example"

        from openbrain import server

        self.assertIsNotNone(server.mcp.kwargs["auth"])
        self.assertEqual(
            server.mcp.kwargs["auth"].tokens,
            {"secret-token": {"client_id": "openbrain", "scopes": []}},
        )

    def test_server_disables_auth_when_key_is_absent(self):
        os.environ.pop("MCP_ACCESS_KEY", None)

        from openbrain import server

        self.assertIsNone(server.mcp.kwargs["auth"])


if __name__ == "__main__":
    unittest.main()
