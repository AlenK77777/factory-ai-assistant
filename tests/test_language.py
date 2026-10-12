"""Unit tests for language detection and script checks. No database or Ollama needed."""

import pytest

from rag.language import NOT_FOUND, answer_in_language, detect_language


@pytest.mark.parametrize(
    "text, language",
    [
        ("What should I do if delamination is found?", "en"),
        ("ESD wrist strap", "en"),
        ("Что делать, если обнаружено расслоение платы?", "ru"),
        ("Какая столица Франции?", "ru"),
        ("מה עושים אם מתגלה התפרקות שכבות בלוח?", "he"),
        ("מהי בירת צרפת?", "he"),
        ("123 !!!", "en"),
        ("", "en"),
    ],
)
def test_detect_language(text, language):
    assert detect_language(text) == language


def test_russian_beats_latin_acronyms_in_the_question():
    assert detect_language("Что делать при ESD в зоне защиты?") == "ru"


def test_hebrew_beats_latin_acronyms_in_the_question():
    assert detect_language("מה עושים עם תווית HOLD אדומה?") == "he"


def test_english_answer_is_accepted_for_english():
    assert answer_in_language("Stop the lot and attach a red HOLD tag.", "en")


def test_russian_answer_is_accepted_for_russian():
    assert answer_in_language("Остановите партию и повесьте красную бирку HOLD.", "ru")


def test_hebrew_answer_is_accepted_for_hebrew():
    assert answer_in_language("עצרו את המנה והצמידו תווית HOLD אדומה.", "he")


def test_english_answer_is_rejected_for_russian():
    assert not answer_in_language("Stop the lot and attach a red HOLD tag.", "ru")


def test_english_answer_is_rejected_for_hebrew():
    assert not answer_in_language("Stop the lot and attach a red HOLD tag.", "he")


def test_not_found_texts_are_in_their_own_language():
    assert answer_in_language(NOT_FOUND["en"], "en")
    assert answer_in_language(NOT_FOUND["ru"], "ru")
    assert answer_in_language(NOT_FOUND["he"], "he")


def test_cyrillic_inside_a_hebrew_word_is_rejected():
    # The mixed letter is Cyrillic о (U+043E), not Hebrew.
    assert not answer_in_language("האיזоляציה Q2", "he")


def test_hebrew_inside_a_russian_word_is_rejected():
    assert not answer_in_language("изоляцיה HOLD", "ru")


def test_cyrillic_in_an_english_answer_is_rejected():
    assert not answer_in_language("Move the lot to изоляция rack Q2.", "en")


def test_arabic_letters_in_a_hebrew_answer_are_rejected():
    assert not answer_in_language("יש לבדוש على both הרגליים HOLD", "he")
