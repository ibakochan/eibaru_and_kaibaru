def is_english(club):
    return getattr(club, "language", "ja") == "en"


def pick(club, japanese, english):
    return english if is_english(club) else japanese
