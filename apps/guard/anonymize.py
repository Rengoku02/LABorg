"""Taking people out of text that the whole lab sees (plan §11: lab totals, never names).

Pure Python (stdlib only), so the Phase 0 baseline notebook runs it without Django. Quartzy
item names often name someone ("GAPDH (14C10) Rabbit mAb - for Priya's westerns"), so any
request text in an anonymized summary goes through a NameScrubber first.
"""

import re
import unicodedata

NAME, EMAIL, ID = "[name]", "[email]", "[id]"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_JOINERS = re.compile(r"['\u2019.-]+")  # inside one name: "Jean-Luc", "O'Brien", "J.R."
# Letters NFD doesn't split into a letter and an accent, which people type without it
# ("Łukasz" as "Lukasz", "Đức" as "Duc"), and the curly apostrophe (U+2019).
_PLAIN = str.maketrans(
    {"ø": "o", "ł": "l", "đ": "d", "\u0131": "i", "æ": "ae", "œ": "oe", "\u2019": "'"}
)
# Chinese, Japanese and Korean, where a name runs into the words around it.
_SPACELESS = re.compile(r"(?:HALFWIDTH )?(?:CJK |IDEOGRAPHIC|HIRAGANA|KATAKANA|HANGUL)")
# The kana voicing marks: NFD splits "が" into "か" and one of them, but it makes another
# letter, not an accented one.
_KANA_MARKS = "\u3099\u309a"


class NameScrubber:
    """Replaces people's names, and every email address and uuid (a Quartzy id, as in a
    pasted request link), in free text.

    ``names`` are first, last and full names as Quartzy holds them. Every word in them with 2
    or more letters is replaced wherever it stands as a whole word, ignoring case and accents
    on both sides: "Priya", "PRIYA's", "José" typed as "Jose" and "Jose" typed as "José" all
    go, and so do "Łukasz" typed as "Lukasz" and "Strauß" as "STRAUSS"; "Priyanka" stays. A
    hyphenated or apostrophe name counts whole and by its parts. Everyone gets the same
    replacement, so nothing scrubbed can be linked to one person. A name that is also a
    product or vendor word ("Gold", "Fisher") goes there too: privacy first.

    In Chinese, Japanese and Korean a name runs into the words around it (no spaces between
    words, or a Korean particle: "민수의"), so in those scripts a name is found inside other
    text ("王伟的抗体"): any part of a name with 2 or more such characters ("小明", "山田"), and
    a full name, family name first ("王伟" for Quartzy's "伟 王") or last. A single character
    ("王", "김") is never replaced on its own: it is part of too many other words. A voiced
    kana is a letter of its own, not an accented one: "かずみ" doesn't find "かすみ".

    Not found: a name run into another word ("Priyas", "PriyaP"); a one-letter name or initial
    ("J."); a name spelled another way ("Mueller" for "Müller", "Jo" for "Joanna") or written
    in a script its Quartzy profile doesn't use ("王伟" for "Wei Wang"); a name in another
    script without spaces (Thai, say), which is found only as a whole word.
    """

    def __init__(self, names):
        words, runs = set(), set()
        for name in names:
            parts = _EMAIL.sub(" ", unicodedata.normalize("NFC", name or "")).split()
            spaceless = [part for part in parts if _is_spaceless(part)]
            runs |= {*spaceless, "".join(spaceless), "".join(reversed(spaceless))}
            for part in parts:
                if part in spaceless:
                    continue
                for word in {part, *_JOINERS.split(part)}:
                    if _letters(word) >= 2:
                        words.add(word)
        runs = {run for run in runs if _letters(run) >= 2}
        # A whole word: no letter or digit right before or after it (an underscore separates).
        alternatives = [rf"(?<![^\W_])(?:{_either(words)})(?![^\W_])"] if words else []
        alternatives += [_either(runs)] if runs else []
        self._names = re.compile("|".join(alternatives)) if alternatives else None

    def __call__(self, text):
        """``text`` without names, email addresses and uuids; None and "" stay as they are."""
        if not text:
            return text
        text = _UUID.sub(ID, _EMAIL.sub(EMAIL, unicodedata.normalize("NFC", text)))
        return self.replace_names(text, lambda name: NAME)

    def replace_names(self, text, replace):
        """``text`` with each name found in it replaced by ``replace(name)``. ``name`` is the
        match folded ("José's" and "JOSE's" both give "jose"), so every spelling of a name can
        get the same pseudonym. Only names: email addresses and uuids stay as they are."""
        if not text or self._names is None:
            return text
        # Names are looked for in a folded copy and replaced in the text as it was typed.
        folded, origin = _folded(text)
        kept, end = [], 0
        for match in self._names.finditer(folded):
            kept += [text[end : origin[match.start()]], replace(match[0])]
            end = origin[match.end()]
        return "".join(kept) + text[end:]


def _fold(text):
    """``text`` without case or accents: "José" -> "jose", "Łukasz" -> "lukasz"."""
    marks = unicodedata.normalize("NFD", text.casefold().translate(_PLAIN))
    kept = (c for c in marks if c in _KANA_MARKS or not unicodedata.combining(c))
    return unicodedata.normalize("NFC", "".join(kept))


def _folded(text):
    """(``text`` folded, the index in ``text`` of each folded character's source, plus one
    for the end). A character can fold to nothing (an accent typed apart that no letter
    takes, so it stays a character of its own) or to several ("ß" -> "ss")."""
    folded, origin = [], []
    for index, char in enumerate(text):
        for piece in _fold(char):
            folded.append(piece)
            origin.append(index)
    origin.append(len(text))
    return "".join(folded), origin


def _either(names):
    """A regex alternation of the folded ``names``, longest first."""
    folded = {_fold(name) for name in names}
    return "|".join(map(re.escape, sorted(folded, key=lambda name: (-len(name), name))))


def _letters(text):
    return sum(char.isalpha() for char in text)


def _is_spaceless(part):
    """Whether every letter of ``part`` is Chinese, Japanese or Korean."""
    letters = [char for char in part if char.isalpha()]
    return bool(letters) and all(_SPACELESS.match(unicodedata.name(c, "")) for c in letters)
