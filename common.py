import ipaddress
import urllib.parse


def validate_url(url, allowed_hosts=frozenset()):
    """Return an error string, or None if the URL is acceptable (blocks private/loopback targets)."""
    if not url:
        return "Missing ?url parameter"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "URL must be http(s)"
    host = parsed.hostname.lower()
    if allowed_hosts and host not in allowed_hosts:
        return "Host not allowed"
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return "Host not allowed"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return "Host not allowed"
    except ValueError:
        pass  # not an IP literal
    return None


def base_domain(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:])
