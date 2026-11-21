"""Endpoint policy for destinations a Clip Desk user configures."""
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

import requests

from secret_store import value


PRIVATE_NETWORKS = tuple(map(ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                        "100.64.0.0/10", "fc00::/7")))


def private_host(host):
    if host in {"localhost", "umbrel"} or host.endswith(".local"):
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in network for network in PRIVATE_NETWORKS)


def endpoint(raw, *, local_http=False):
    url = raw.strip().rstrip("/")
    if not url or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url):
        return ""
    try:
        parts = urlsplit(url)
        if parts.port == 0 or not parts.hostname or "@" in parts.netloc or parts.query or parts.fragment:
            return ""
        if parts.scheme == "https" or (parts.scheme == "http" and local_http and private_host(parts.hostname)):
            return url
    except ValueError:
        pass
    return ""


def ollama_base():
    return endpoint(value("OLLAMA_URL"), local_http=not value("OLLAMA_API_TOKEN"))


def ollama_request(base, route, payload, timeout):
    token = value("OLLAMA_API_TOKEN")
    if not endpoint(base, local_http=not token):
        raise ValueError("Set a private Ollama address or a trusted HTTPS endpoint in Setup.")
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(f"{base}/api/{route}", json=payload, headers={"Authorization": f"Bearer {token}"} if token else {},
                                timeout=timeout, allow_redirects=False)
        if not response.ok or response.is_redirect:
            response.raise_for_status()
            raise ValueError("The Ollama endpoint redirected the request.")
        return response
