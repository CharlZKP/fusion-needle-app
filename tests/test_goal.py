from app.goal import split_goal


def test_contract_example():
    goal = "plate 40x30x10 → Ø6 hole at (10, 0) → polar pattern ×6 → fillet the top edges R2"
    assert split_goal(goal) == ["plate 40x30x10", "Ø6 hole at (10, 0)", "polar pattern ×6",
                                "fillet the top edges R2"]


def test_every_separator():
    assert split_goal("a -> b then c\nd\r\ne → f THEN g Then h") == list("abcdefgh")


def test_then_is_a_whole_word():
    assert split_goal("strengthen the rib, lengthen it") == ["strengthen the rib, lengthen it"]
    assert split_goal("authentic then; athens") == ["authentic", "; athens"]


def test_trim_and_drop_empty():
    assert split_goal("  a  →  → \n\n ->b->  ") == ["a", "b"]
    assert split_goal("") == []
    assert split_goal(" \n then → ") == []


def test_text_is_not_reworded():
    feature = "Plate 1.5 in x 2,5mm  ×3 (Keep  spacing)"
    assert split_goal(feature) == [feature]
    assert split_goal("a - > b") == ["a - > b"]          # only the exact arrows separate
