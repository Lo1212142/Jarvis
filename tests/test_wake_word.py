from openjarvis.voice.wake_word import WakeWordConfig, detect_wake_word, normalize_text


def test_wake_word_supports_arabic_and_english() -> None:
    config = WakeWordConfig(words=("Jarvis", "يا جارفيس"), language="multi")
    assert detect_wake_word("يا جارفيس افتح المشروع", config) == "يا جارفيس"
    assert detect_wake_word("Jarvis, show me the logs", config) == "Jarvis"


def test_arabic_diacritics_are_normalized() -> None:
    assert normalize_text("يَا جَارْفِيس") == "يا جارڤيس" or normalize_text("يَا جَارْفِيس") == "يا جارفيس"


def test_disabled_wake_word_does_not_match() -> None:
    config = WakeWordConfig(enabled=False)
    assert detect_wake_word("Jarvis", config) is None
