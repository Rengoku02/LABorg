"""apps.guard.anonymize: taking people out of free text that the whole lab will see."""

import pytest

from apps.guard.anonymize import NameScrubber

LINK = (
    "https://app.quartzy.com/groups/e44155cc-de69-56a0-a371-de796026e823/requests"
    "?id=ab171e34-ceff-54a3-9181-521b45640b56"
)
SCRUB = NameScrubber(["Priya", "Patel", "José", "García-López", "Siobhán O'Brien", "Li", "J."])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "GAPDH (14C10) Rabbit mAb - for Priya's westerns",
            "GAPDH (14C10) Rabbit mAb - for [name]'s westerns",
        ),
        ("PRIYA PATEL to order", "[name] [name] to order"),
        ("primers_for_priya", "primers_for_[name]"),  # an underscore separates words
        ("Priyanka's primers", "Priyanka's primers"),  # not a whole-word match
        ("Jose's plasmids", "[name]'s plasmids"),  # typed without the accent
        ("Garcia-Lopez lab, for Lopez", "[name] lab, for [name]"),  # whole or by its parts
        ("Siobhan's and O'Brien's cells; Brien", "[name]'s and [name]'s cells; [name]"),
        # A two-letter name counts, but only as a whole word; one letter never does.
        ("Li2CO3 and LiCl for Li", "Li2CO3 and LiCl for [name]"),
        ("J. Smith's kit", "J. Smith's kit"),
        ("ask priya.patel@example.edu", "ask [email]"),
        (f"see {LINK}", "see https://app.quartzy.com/groups/[id]/requests?id=[id]"),
        ("", ""),
        (None, None),
    ],
)
def test_names_emails_and_ids_are_replaced(text, expected):
    assert SCRUB(text) == expected


def test_without_names_only_emails_and_ids_go():
    scrub = NameScrubber(["priya.patel@example.edu", "", None])  # not names: nothing to find
    assert scrub("Priya's example kit, priya.patel@example.edu") == "Priya's example kit, [email]"


@pytest.mark.parametrize(
    ("names", "text", "expected"),
    [
        # The profile has no accent, the text has one: the text is folded too.
        (
            ["Jose", "Garcia"],
            "GAPDH mAb - for José García's westerns",
            "GAPDH mAb - for [name] [name]'s westerns",
        ),
        (["Zoe", "Muller"], "FBS for Zoë Müller, ZOË", "FBS for [name] [name], [name]"),
        (["Jose"], "Jose\u0301's and Jose\u0332's", "[name]'s and [name]'s"),  # marks typed apart
        # Letters NFD can't split, typed without their stroke, either way round.
        (["Łukasz", "Đức", "Søren"], "Lukasz, Duc and Soren", "[name], [name] and [name]"),
        (["Lukasz"], "for Łukasz", "for [name]"),
        (["Strauß"], "STRAUSS lab", "[name] lab"),
        (["O\u2019Brien"], "O'Brien's cells", "[name]'s cells"),
        (["Priya"], "Priyá's primers and Priyanká", "[name]'s primers and Priyanká"),
    ],
)
def test_accents_are_ignored_in_the_text_too(names, text, expected):
    assert NameScrubber(names)(text) == expected


# Quartzy shows "first last"; Chinese, Japanese and Korean put the family name first.
CJK = NameScrubber(
    ["伟", "王", "伟 王", "小明", "花子", "山田", "花子 山田", "민수", "김", "민수 김", "かずみ"]
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("王伟的抗体", "[name]的抗体"),  # a full name, inside other words
        ("for 伟王", "for [name]"),
        ("小明的样品", "[name]的样品"),  # a given name of two characters
        ("山田さんの抗体、花子用", "[name]さんの抗体、[name]用"),
        ("山田花子", "[name]"),
        ("김민수의 항체, 민수씨", "[name]의 항체, [name]씨"),
        # One character is too many other words to scrub: aqua regia, kimchi.
        ("王水 and 김치", "王水 and 김치"),
        ("민숙의 샘플", "민숙의 샘플"),  # another name that starts the same way
        # A voiced kana is another letter, not an accented one: Kazumi isn't kasumi (haze).
        ("かすみ草の種、かずみさん用", "かすみ草の種、[name]さん用"),
    ],
)
def test_names_in_scripts_without_spaces_are_found_inside_words(text, expected):
    assert CJK(text) == expected


def test_replace_names_hands_each_name_over_folded_and_leaves_the_rest():
    found = []
    scrub = NameScrubber(["Jose", "伟 王"])
    text = "José's gel, JOSE's blot, 王伟的抗体, 0f8fad5b-d9cb-469f-a165-70867728950e"
    replaced = scrub.replace_names(text, lambda name: found.append(name) or "N")
    assert replaced == "N's gel, N's blot, N的抗体, 0f8fad5b-d9cb-469f-a165-70867728950e"
    assert found == ["jose", "jose", "王伟"]
    assert NameScrubber([]).replace_names("José", str.upper) == "José"
