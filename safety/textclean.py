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

# The control characters that Python's str.split() treats as whitespace: vertical tab, form
# feed, the four information separators U+001C-U+001F and NEL U+0085. In a text of words
# they separate words, so by default each run of them becomes ONE space rather than
# nothing: dropping them joined the words on either side, and an event name
# "High<VT>Wind Warning" read "highwind warning", which no longer matched the configured
# veto events — a High Wind Warning that did not veto. In a single token (a time, a status,
# a VTEC string) there are no words to separate, and a blank there breaks the token:
# "...T22:12:00<VT>-05:00" is no ISO time with a blank before the offset, "Act<VT>ual" no
# status "Actual". Such a field is read with sep="" (the run is dropped, as everywhere
# before the blank rule). Where it cannot be told which a separator was — inside an event
# name — the caller reads the text both ways (see nws_alerts.normalize).
_SPACE = re.compile("[\x0b\x0c\x1c-\x1f\x85]+")

# Dropped: the other C0 controls but tab and newline (the carriage return is turned into a
# newline first, see clean_text), DEL, the other C1 controls U+0080-U+009F, and the
# bidirectional controls: the Arabic letter mark U+061C, the left-to-right and right-to-left
# marks U+200E/U+200F, the embeddings, overrides and their pop U+202A-U+202E, the isolates
# and their pop U+2066-U+2069. (The whitespace controls of _SPACE are replaced before this
# runs, so its ranges no longer meet them.)
_DROP = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")


def clean_text(value, sep: str = " ") -> str:
    """``value`` with what cannot or must not be shown taken out; "" for a non-str.

    - a lone surrogate becomes U+FFFD (the replacement character), so the text encodes to
      UTF-8; two surrogates that do form a pair (high then low) become their one character;
    - a carriage return becomes a newline: "\\r\\n" one newline, a lone "\\r" one too, so a
      text written with old Mac line ends keeps its lines apart instead of running together;
    - each run of the control characters that str.split() counts as whitespace (vertical
      tab, form feed, U+001C-U+001F, NEL U+0085) becomes ``sep``: by default one space, so
      the words they separate stay apart; "" for a single token such as a time or a status,
      which a blank would break (see ``_SPACE``);
    - the other control characters (C0 but tab and newline, DEL, C1) and the bidirectional
      controls are dropped (see ``_DROP``);
    - everything else is kept: letters of any script, symbols, emoji, U+FFFD itself.

    Three linear scans of the text for a typical alert; the surrogate repair only when one
    is there."""
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
    return _DROP.sub("", _SPACE.sub(sep, s))
