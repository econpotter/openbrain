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
mcp_server_auth_provider = types.ModuleType("mcp.server.auth.provider")
mcp_server_auth_settings = types.ModuleType("mcp.server.auth.settings")


class _AccessToken:
    def __init__(self, token, client_id, scopes, expires_at):
        self.token = token
        self.client_id = client_id
        self.scopes = scopes
        self.expires_at = expires_at


class _AuthSettings:
    def __init__(self, issuer_url, resource_server_url):
        self.issuer_url = issuer_url
        self.resource_server_url = resource_server_url


mcp_server_auth_provider.AccessToken = _AccessToken
mcp_server_auth_settings.AuthSettings = _AuthSettings
sys.modules.setdefault("mcp", mcp)
sys.modules.setdefault("mcp.server", mcp_server)
sys.modules.setdefault("mcp.server.auth", mcp_server_auth)
sys.modules.setdefault("mcp.server.auth.provider", mcp_server_auth_provider)
sys.modules.setdefault("mcp.server.auth.settings", mcp_server_auth_settings)

os.environ.setdefault("DATABASE_URL", "postgresql://test/openbrain")


class ServerAuthTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        sys.modules.pop("openbrain.server", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        sys.modules.pop("openbrain.server", None)

    def test_server_builds_bearer_token_auth_when_key_is_present(self):
        os.environ["MCP_ACCESS_KEY"] = "secret-token"
        os.environ["CADDY_HOSTNAME"] = "brain.example"

        from openbrain import server

        self.assertIsNotNone(server.mcp.kwargs["auth"])
        self.assertIsNotNone(server.mcp.kwargs["token_verifier"])
        self.assertEqual(server.mcp.kwargs["auth"].resource_server_url, "https://brain.example:8443/mcp")

    def test_static_token_verifier_accepts_only_the_configured_key(self):
        from openbrain import server

        verifier = server.StaticTokenVerifier("secret-token")

        self.assertIsNone(self._run(verifier.verify_token("wrong")))
        token = self._run(verifier.verify_token("secret-token"))
        self.assertEqual(token.client_id, "openbrain")
        self.assertEqual(token.token, "secret-token")

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
