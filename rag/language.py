"""Detect the question language from its alphabet (English, Russian or Hebrew).

Used so the local model is constrained to answer in the same language, and so
the "not found" text is a fixed translation rather than free generation.
"""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["en", "ru", "he"]

LANGUAGES: tuple[Language, ...] = ("en", "ru", "he")

LANGUAGE_NAMES: dict[Language, str] = {
    "en": "English",
    "ru": "Russian",
    "he": "Hebrew",
}

# When found is false, the answer is replaced by this text so it cannot invent content.
NOT_FOUND: dict[Language, str] = {
    "en": "No relevant work instruction was found for this question.",
    "ru": "По этому вопросу не найдена соответствующая рабочая инструкция.",
    "he": "לא נמצאה הוראת עבודה רלוונטית לשאלה זו.",
}

FAILED: dict[Language, str] = {
    "en": "The local model did not return a valid answer. Please try again or ask a QA engineer.",
    "ru": "Локальная модель не вернула корректный ответ. Повторите попытку или обратитесь к инженеру по качеству.",
    "he": "המודל המקומי לא החזיר תשובה תקינה. נסו שוב או פנו למהנדס איכות.",
}

# Plant codes and acronyms (ESD, EPA, MES, HOLD, WI-PLN-007) are Latin even in RU/HE answers.
_CODE_TOKEN = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)*\b")


def count_letters(text: str) -> dict[Language, int]:
    """Count alphabetic characters per script. Other letters are ignored."""
    counts: dict[Language, int] = {"en": 0, "ru": 0, "he": 0}
    for character in text:
        if not character.isalpha():
            continue
        code = ord(character)
        if 0x0590 <= code <= 0x05FF:
            counts["he"] += 1
        elif 0x0400 <= code <= 0x04FF:
            counts["ru"] += 1
        elif character.isascii():
            counts["en"] += 1
    return counts


def detect_language(text: str) -> Language:
    """Return the script with the most letters in the question; English if none."""
    counts = count_letters(text)
    winner: Language = max(LANGUAGES, key=lambda language: counts[language])
    return winner if counts[winner] else "en"


def answer_in_language(text: str, language: Language, min_share: float = 0.5) -> bool:
    """True if at least min_share of the letters (ignoring plant codes) are in the expected script."""
    counts = count_letters(_CODE_TOKEN.sub(" ", text))
    letters = sum(counts.values())
    if letters == 0:
        return False
    return counts[language] / letters >= min_share
