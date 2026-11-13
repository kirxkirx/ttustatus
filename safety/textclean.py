"""``clean_text``: a feed's text made safe to keep, write and show.

The texts of NWS alerts and the hazard feeds go into the state files, the status page and
the /setup page. A lone surrogate in one of them (JSON's "\\ud800" escape decodes to it)
cannot be encoded to UTF-8: the page write raised UnicodeEncodeError on every run and the
/setup page answered 500 until the alert expired. Control characters do no harm to Python
but garble a log line or a terminal, and the bidirectional controls can make a shown text
read other than it is (an "Extreme" that reads backwards, a time that seems to be another).
So every such text passes through here once, where it comes in.
"""
from __future__ import annotations

import re

# A surrogate code point (U+D800-U+DFFF): in a Python str always half of a pair that is not
# one character, as only a decoder with "surrogatepass" or "surrogateescape", or a JSON
# "\\udXXX" escape, puts it there. UTF-8 refuses to encode it.
_SURROGATE = re.compile("[\ud800-\udfff]")

# Dropped: the C0 controls but tab and newline (the carriage return is turned into a newline
# first, see clean_text), DEL, the C1 controls U+0080-U+009F, and the bidirectional controls:
# the Arabic letter mark U+061C, the left-to-right and right-to-left marks U+200E/U+200F, the
# embeddings, overrides and their pop U+202A-U+202E, the isolates and their pop U+2066-U+2069.
_DROP = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")


def clean_text(value) -> str:
    """``value`` with what cannot or must not be shown taken out; "" for a non-str.

    - a lone surrogate becomes U+FFFD (the replacement character), so the text encodes to
      UTF-8; two surrogates that do form a pair (high then low) become their one character;
    - a carriage return becomes a newline: "\\r\\n" one newline, a lone "\\r" one too, so a
      text written with old Mac line ends keeps its lines apart instead of running together;
    - the other control characters (C0 but tab and newline, DEL, C1) and the bidirectional
      controls are dropped (see ``_DROP``);
    - everything else is kept: letters of any script, symbols, emoji, U+FFFD itself.

    Two linear scans of the text for a typical alert; the surrogate repair only when one is
    there."""
    if not isinstance(value, str):
        return ""
    s = value
    if _SURROGATE.search(s):
        # UTF-16 with surrogatepass writes every code point as its 16-bit units, a lone
        # surrogate included; decoding them back joins each true pair into its character and
        # reports each unpaired unit as an error, which "replace" makes U+FFFD.
        s = s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if "\r" in s:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _DROP.sub("", s)
