"""Optional dictionary providers; grading never depends on dictionary availability."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol


OEWN_SPEC = "oewn:2025"


@dataclass(frozen=True)
class DictionaryEntry:
    term: str
    part_of_speech: str
    definition: str


class DictionaryProvider(Protocol):
    def lookup(self, term: str) -> DictionaryEntry | None: ...


class OewnDictionaryProvider:
    """Read Open English WordNet 2025 through the lightweight ``wn`` package."""

    _download_started = False
    _download_lock = threading.Lock()

    def __init__(self) -> None:
        self._wordnet = None
        try:
            import wn
        except Exception:
            self._wn = None
            return
        self._wn = wn
        try:
            self._wordnet = wn.Wordnet(OEWN_SPEC)
        except Exception:
            self._wordnet = None

    @classmethod
    def _start_background_install(cls, wn_module: object) -> None:
        with cls._download_lock:
            if cls._download_started:
                return
            cls._download_started = True

        def install() -> None:
            try:
                wn_module.download(OEWN_SPEC, progress_handler=None)
            except Exception:
                pass

        threading.Thread(target=install, name="oewn-2025-install", daemon=True).start()

    def lookup(self, term: str) -> DictionaryEntry | None:
        clean = " ".join(term.strip().split())
        if not clean:
            return None
        if self._wordnet is None:
            if self._wn is not None:
                self._start_background_install(self._wn)
            return None
        try:
            synsets = self._wordnet.synsets(clean)
            if not synsets and " " in clean:
                synsets = self._wordnet.synsets(clean.replace(" ", "_"))
            if not synsets:
                return None
            synset = synsets[0]
            definition = synset.definition()
            pos = {
                "n": "noun", "v": "verb", "a": "adjective",
                "s": "adjective", "r": "adverb",
            }.get(str(synset.pos), str(synset.pos))
            return DictionaryEntry(clean, pos, str(definition or "").strip())
        except Exception:
            return None


@lru_cache(maxsize=1)
def get_default_dictionary_provider() -> DictionaryProvider:
    return OewnDictionaryProvider()
