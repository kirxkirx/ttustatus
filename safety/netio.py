"""Bounded reading of a download: ``read_capped`` for an HTTP reply, ``gunzip_capped`` for a
gzip'ed file.

``urlopen(..., timeout=T)`` bounds each wait on the socket, not the reply: a server that
trickles a byte every few seconds keeps ``r.read()`` going for as long as it likes, and the
layer's loop thread, with the layer's verdict, stays where it is meanwhile. ``r.read()``
also takes whatever size the server sends, and ``r.read(n)`` waits for all n bytes. So the
body is read here in chunks of what has arrived, against a size cap and a deadline for the
whole body, both checked after every chunk. There is no SIGALRM to fall back on (the
readers run in worker threads, where signals cannot be set): the socket timeout still
bounds a single wait, and this deadline the sum of them.

A gzip'ed file is inflated in steps for the same reason: a few kB can inflate to gigabytes,
and ``gzip.decompress`` holds all of it before anyone can look at the size.

Ports of vlbaweather's ``weather.http._read_body`` and ``vlba.mrms._gunzip``.
"""
from __future__ import annotations

import http.client
import time
import zlib

# Most bytes asked for in one read: the deadline and the cap are checked this often at
# least, and no more than this is held beyond the cap.
READ_CHUNK = 64 * 1024

# Inflated per step by gunzip_capped: what it holds beyond its cap at most.
INFLATE_STEP = 1024 * 1024

# The deadline's clock: monotonic, so a step of the wall clock (chrony at boot) neither
# cuts a reply short nor lets it run on. A name of its own for the tests to replace.
_clock = time.monotonic


class ReplyTooLarge(ValueError):
    """The reply declares, or has sent, more than the caller's cap. A ValueError, as a reply
    the caller cannot use is one."""


class ReplyTooSlow(TimeoutError):
    """The reply was not complete within the caller's deadline. A TimeoutError, so an OSError,
    as the socket's own timeout is one."""


class ReplyCutShort(http.client.IncompleteRead, ValueError):
    """The connection closed before the length the reply declared: ``read1`` ends quietly
    then, where ``read()`` would raise IncompleteRead. Both an IncompleteRead (an
    http.client.HTTPException) and a ValueError, so a caller that catches either for a bad
    reply catches this one too."""

    def __str__(self) -> str:
        return "reply cut short: %d bytes read, %d more expected" % (len(self.partial), self.expected)


def _label(what: str) -> str:
    return "%s: " % what if what else ""


def _content_length(resp):
    """The reply's ``Content-Length`` as an int, or None when it has none that reads as one
    (a test object without headers, a malformed or negative value)."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        v = headers.get("Content-Length")
        n = int(str(v).strip()) if v is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    return n if n is not None and n >= 0 else None


def read_capped(resp, max_bytes: int, deadline_s: float, what: str = "") -> bytes:
    """The body of ``resp``, read whole within ``max_bytes`` and ``deadline_s`` seconds.

    ``resp``: an http.client.HTTPResponse (what urlopen returns for http and https), urllib's
    addinfourl, or any object with ``read(n)`` (``read1(n)`` is used when it has one: it
    returns what has arrived instead of waiting for all n bytes, so a trickling server meets
    the deadline check after each piece). ``what`` names the download in the error texts.

    Raises, before reading anything, ReplyTooLarge when the declared Content-Length is over
    ``max_bytes``; while reading, after every chunk, ReplyTooLarge once more than
    ``max_bytes`` have come (a body of exactly ``max_bytes`` is accepted) and ReplyTooSlow
    once ``time.monotonic()`` is past the start plus ``deadline_s``; after the end,
    ReplyCutShort when an HTTPResponse got less than its declared length. What the transport
    raises (socket.timeout, OSError, http.client's errors) passes through."""
    start = _clock()
    declared = _content_length(resp)
    if declared is not None and declared > max_bytes:
        raise ReplyTooLarge("%sreply of %d bytes is over the limit of %d"
                            % (_label(what), declared, max_bytes))
    read = getattr(resp, "read1", None) or resp.read
    parts = []
    got = 0
    while True:
        # Ask for one byte past the cap at most: one more than max_bytes is enough to know.
        chunk = read(max(1, min(READ_CHUNK, max_bytes + 1 - got)))
        if not chunk:
            break
        got += len(chunk)
        if got > max_bytes:
            raise ReplyTooLarge("%sreply over the limit of %d bytes" % (_label(what), max_bytes))
        parts.append(chunk)
        if _clock() - start > deadline_s:
            raise ReplyTooSlow("%sreply not complete after %.0f s (%d bytes read)"
                               % (_label(what), deadline_s, got))
    body = b"".join(parts)
    # HTTPResponse counts down ``length`` from the declared Content-Length; still above 0 at
    # the end, the connection closed early and the body is a truncated one.
    short = getattr(resp, "length", None) if isinstance(resp, http.client.HTTPResponse) else None
    if isinstance(short, int) and not isinstance(short, bool) and short > 0:
        raise ReplyCutShort(body, short)
    return body


def gunzip_capped(data: bytes, max_bytes: int, step: int = INFLATE_STEP) -> bytes:
    """A gzip'ed file inflated, at most ``max_bytes`` bytes of it, ``step`` bytes at a time.

    The parts held stay within ``max_bytes`` plus one step on every Python (``decompress``
    honours its ``max_length`` on each). ValueError for a file that inflates to more than
    ``max_bytes``, for one cut short (its stream does not end), for one with anything but
    zero padding after its stream (a second gzip member included: the files read here have
    one), and for data that is no gzip or deflate stream at all (zlib.error made one)."""
    step = max(1, int(step))            # 0 would be "no limit" to zlib
    z = zlib.decompressobj(16 + zlib.MAX_WBITS)       # the gzip wrapper
    parts, size, rest = [], 0, data
    try:
        while not z.eof:
            piece = z.decompress(rest, step)
            rest = z.unconsumed_tail
            if not piece:               # nothing more to inflate, and the stream not ended
                break
            size += len(piece)
            if size > max_bytes:
                raise ValueError("the file inflates to more than %d bytes" % max_bytes)
            parts.append(piece)
    except zlib.error as e:
        raise ValueError("not a readable gzip stream: %s" % e) from None
    if not z.eof:
        raise ValueError("the gzip stream is cut short")
    if z.unused_data.strip(b"\0"):
        raise ValueError("%d bytes after the gzip stream" % len(z.unused_data))
    return b"".join(parts)
