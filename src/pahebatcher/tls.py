"""Hardened TLS context for outgoing HTTPS connections."""

import ssl


def make_ssl_ctx() -> ssl.SSLContext:
    """Builds a hardened client-side TLS context.

    The context requires TLS 1.2 or newer, disables TLS compression,
    restricts ciphers to ECDHE/DHE suites with AES-GCM or ChaCha20, and
    enforces certificate and host name verification.

    Returns:
        The configured context.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.set_ciphers(
        "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
        "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
        "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
        "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384"
    )
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx
