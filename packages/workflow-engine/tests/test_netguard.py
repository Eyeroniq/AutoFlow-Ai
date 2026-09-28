"""The HTTP Request node's SSRF guard: private/internal addresses are refused by default,
for IP literals, internal names, names that resolve to internal addresses, and every
redirect hop; HTTP_ALLOW_PRIVATE_NETWORKS turns it off."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowforge_engine import ExecutionServices, NodeStatus, ProviderSettings, execute_node
from flowforge_engine import netguard
from flowforge_engine.netguard import BlockedDestination, check_url, ip_block_reason, name_block_reason
from flowforge_engine.testing import make_context, node


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("127.10.20.30", "loopback"),
        ("10.0.0.5", "private"),
        ("172.16.3.4", "private"),
        ("172.31.255.1", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "link-local"),  # cloud metadata
        ("100.64.0.1", "reserved"),  # carrier-grade NAT
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("::1", "loopback"),
        ("fe80::1", "link-local"),
        ("fd12:3456::1", "private"),
        ("::ffff:127.0.0.1", "loopback"),  # IPv4-mapped IPv6
        ("::ffff:10.0.0.1", "private"),
    ],
)
def test_internal_addresses_are_blocked(address, reason):
    assert reason in ip_block_reason(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.215.14", "172.32.0.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert ip_block_reason(address) is None


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST.", "api", "postgres", "redis", "host.docker.internal", "db.internal", "nas.local"]
)
def test_internal_names_are_blocked_before_dns(host):
    assert name_block_reason(host)


def test_public_names_pass_the_name_check():
    assert name_block_reason("api.example.com") is None


def test_check_url_catches_literals_and_names():
    for url in ("http://127.0.0.1:8000/", "http://[::1]/", "http://postgres:5432", "http://2130706433/",
                "https://metadata.google.internal/computeMetadata/v1/"):
        with pytest.raises(BlockedDestination):
            check_url(url)
    check_url("https://api.example.com/v1")


# --- the node, end to end ---------------------------------------------------------------


def guarded_services(**kwargs):
    return ExecutionServices(provider_settings=ProviderSettings(testing=True), allow_private_network=False, **kwargs)


async def run_http(url, services):
    return await execute_node(node("http", "http_request", url=url), make_context(services=services))


async def test_the_node_refuses_internal_urls():
    result = await run_http("http://127.0.0.1:8000/api/health", guarded_services())
    assert result.status is NodeStatus.FAILED
    assert result.error == (
        "Blocked request to '127.0.0.1': it is a loopback address. "
        "Set HTTP_ALLOW_PRIVATE_NETWORKS=true to allow internal addresses (local development only)."
    )
    docker = await run_http("http://redis:6379/", guarded_services())
    assert "single-label host name (e.g. a Docker service name)" in docker.error


async def test_names_that_resolve_to_internal_addresses_are_blocked(monkeypatch):
    # DNS rebinding / internal DNS: an innocent-looking name pointing inside.
    answers = {"rebind.example.com": ["10.1.2.3"], "mixed.example.com": ["93.184.215.14", "192.168.0.10"]}

    async def fake_resolve(host, port):
        return answers[host]

    monkeypatch.setattr(netguard, "_system_resolve", fake_resolve)
    result = await run_http("http://rebind.example.com/", guarded_services())
    assert result.status is NodeStatus.FAILED
    assert "Blocked request to 'rebind.example.com' (resolves to 10.1.2.3): it is a private network address" in result.error
    # One internal answer among public ones is enough to refuse.
    mixed = await run_http("http://mixed.example.com/", guarded_services())
    assert "(resolves to 192.168.0.10)" in mixed.error


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.seen.append((self.path, self.headers.get("Host")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://inside.example.com:{self.server.server_port}/secret")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def local_counts_as_public(monkeypatch):
    """Stand-in for "a public server": this test's server on 127.0.0.1 is allowed, and
    public.example.com resolves to it; inside.example.com resolves to a private address."""
    real = netguard.ip_block_reason
    monkeypatch.setattr(netguard, "ip_block_reason", lambda ip: None if ip == "127.0.0.1" else real(ip))

    async def fake_resolve(host, port):
        return {"public.example.com": ["127.0.0.1"], "inside.example.com": ["10.20.30.40"]}[host]

    monkeypatch.setattr(netguard, "_system_resolve", fake_resolve)


async def test_allowed_hosts_connect_to_the_checked_address(local_server, local_counts_as_public):
    port = local_server.server_port
    result = await run_http(f"http://public.example.com:{port}/ok", guarded_services())
    assert result.status is NodeStatus.SUCCESS, result.error
    assert result.output["body"] == {"ok": True}
    # The socket went to the resolved address; the Host header still names the site.
    assert local_server.seen == [("/ok", f"public.example.com:{port}")]


async def test_every_redirect_hop_is_checked(local_server, local_counts_as_public):
    port = local_server.server_port
    result = await run_http(f"http://public.example.com:{port}/redirect", guarded_services())
    assert result.status is NodeStatus.FAILED
    assert "Blocked request to 'inside.example.com' (resolves to 10.20.30.40)" in result.error
    assert [path for path, _ in local_server.seen] == ["/redirect"]  # never reached /secret


async def test_the_flag_allows_private_addresses_for_local_development(local_server):
    services = ExecutionServices(provider_settings=ProviderSettings(testing=True), allow_private_network=True)
    result = await run_http(f"http://127.0.0.1:{local_server.server_port}/ok", services)
    assert result.status is NodeStatus.SUCCESS, result.error


def test_the_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("HTTP_ALLOW_PRIVATE_NETWORKS", "true")
    assert ExecutionServices(provider_settings=ProviderSettings(testing=True)).allow_private_network is True
    monkeypatch.setenv("HTTP_ALLOW_PRIVATE_NETWORKS", "false")
    assert ExecutionServices(provider_settings=ProviderSettings(testing=True)).allow_private_network is False
    monkeypatch.delenv("HTTP_ALLOW_PRIVATE_NETWORKS")
    assert ExecutionServices(provider_settings=ProviderSettings(testing=True)).allow_private_network is False
