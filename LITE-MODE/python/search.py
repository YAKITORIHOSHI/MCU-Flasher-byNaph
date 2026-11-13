"""Bounded indexed board search and consistent package result ranking."""
import difflib
import re
import unicodedata


def normalize(value):
    text = "".join(char for char in unicodedata.normalize("NFKD", str(value))
                   if not unicodedata.combining(char)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def query_words(query):
    # Registry qualifiers remain intact in the network request, but their
    # values (rather than the qualifier labels) determine local relevance.
    text = re.sub(r"\b(?:name|owner|vendor|platform|mcu|id|framework|type):", "", str(query), flags=re.I)
    return normalize(text[:200]).split()[:12]


def relevance(words, name, identity, document):
    phrase = " ".join(words)
    compact = phrase.replace(" ", "")
    if not words:
        return 0
    if not all(word in document or word in document.replace(" ", "") for word in words):
        return None
    if phrase == identity or compact == identity.replace(" ", ""):
        return 1200
    if phrase == name or compact == name.replace(" ", ""):
        return 1100
    if name.startswith(phrase):
        return 900
    if phrase in name:
        return 800
    if all(word in name for word in words):
        return 650
    return 300 + sum(40 for word in words if word in identity or word in name)


class BoardSearch:
    def __init__(self):
        self.revision = None
        self.rows = []

    def find(self, catalog, query, limit=80):
        revision, snapshot = catalog.snapshot()
        if revision != self.revision:
            self.rows = []
            for name, info in snapshot.items():
                frameworks = info.get("frameworks") or [info.get("framework") or "arduino"]
                if isinstance(frameworks, str):
                    frameworks = [frameworks]
                plain = normalize(info.get("pio_name") or name.split(" [", 1)[0])
                identity = normalize(info.get("board") or "")
                doc = normalize(" ".join(str(info.get(key, "")) for key in
                    ("board", "platform", "mcu", "pio_vendor", "framework", "frameworks")) + " " + name)
                self.rows.append((name, plain, identity, doc, {
                    "name": name, "platform": str(info.get("platform") or ""),
                    "mcu": str(info.get("mcu") or ""),
                    "frameworks": sorted(str(value) for value in frameworks),
                }))
            self.revision = revision
        words = query_words(query)
        matches = []
        for name, plain, identity, doc, row in self.rows:
            score = relevance(words, plain, identity, doc)
            if score is not None:
                matches.append((-score, len(plain), name.casefold(), row))
        fuzzy = False
        if not matches and words and len(words) <= 5:
            phrase = " ".join(words)
            close = set(difflib.get_close_matches(phrase, [row[1] for row in self.rows], n=limit, cutoff=.72))
            for name, plain, identity, doc, row in self.rows:
                if plain in close:
                    score = difflib.SequenceMatcher(None, phrase, plain).ratio()
                    matches.append((-score, len(plain), name.casefold(), row))
            fuzzy = bool(matches)
        matches.sort(key=lambda item: item[:3])
        return {"query": query, "items": [item[3] for item in matches[:limit]], "total": len(matches), "fuzzy": fuzzy}


def rank_libraries(items, query):
    words = query_words(query)
    seen, scored = set(), []
    for item in items:
        if item["spec"] in seen:
            continue
        seen.add(item["spec"])
        name = normalize(item["name"])
        doc = normalize(" ".join(str(item.get(key, "")) for key in ("name", "owner", "description")))
        score = relevance(words, name, name, doc)
        scored.append((-(score if score is not None else -1), len(name), name, item))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:60]]
