"""The call guard (app/guard.py) on its own: the calls that went wrong in real use must be held back,
good calls must pass, and the ways a request can write a number must not cause a false block."""
import pytest

from app.guard import EVIDENCE, SCHEMA, STATE, Guard, Request, Rules, fold, param_family

BODY = "units: mm; sketch: none; bodies: Body1; last_feature: Extrude1"
SKETCH = "units: mm; sketch: Sketch1 on xy; bodies: none; last_feature: none"
EMPTY = "units: mm; sketch: none; bodies: none; last_feature: none"


def call(tool, /, **arguments):
    return {"name": tool, "arguments": arguments}


@pytest.fixture(scope="module")
def judge(rules, catalogue):
    guard = Guard(rules)

    def check(query, calls, state=BODY, offered=None, **options):
        return guard.check(query, state, calls, catalogue, offered, **options)

    return check


def codes(verdict):
    return sorted((issue["code"], issue.get("param", "")) for issue in verdict["issues"])


# ---- what went wrong in real use: every one of these is held back -----------------

def test_invented_values_are_held_back(judge):
    verdict = judge("cut in half, horizontally",
                    [call("create_hole", diameter=0.5, depth=0.5, face="bottom"),
                     call("extrude", operation="cut", extent="through_all")])
    assert verdict["decision"] == "blocked"
    found = codes(verdict)
    assert ("not_in_request", "diameter") in found and ("not_in_request", "depth") in found   # "half" is not 0.5
    assert ("not_named", "face") in found and ("not_named", "extent") in found
    assert ("not_named", "operation") not in found           # "cut" is written
    assert not verdict["overridable"]                        # ... and extrude has no open sketch to work on
    assert ("state_need", "") in found


def test_a_count_and_a_margin_are_not_a_diameter_and_a_depth(judge):
    verdict = judge("4 holes on the corner with 5 mm margin", [call("create_hole", diameter=4, depth=5)])
    assert codes(verdict) == [("wrong_label", "depth"), ("wrong_label", "diameter")]
    assert verdict["decision"] == "blocked" and verdict["overridable"]
    messages = " ".join(issue["message"] for issue in verdict["issues"])
    assert "“4 holes”" in messages and "“5 mm margin”" in messages and "count" in messages


def test_a_distance_from_the_corner_is_not_a_diameter(judge):
    verdict = judge("hole on the leftside 10 mm from each corner", [call("create_hole", diameter=10, face="left")])
    assert ("wrong_label", "diameter") in codes(verdict) and verdict["decision"] == "blocked"
    assert "“10 mm from”" in " ".join(issue["message"] for issue in verdict["issues"])


def test_one_number_is_not_two_arguments(judge):
    verdict = judge("10 mm hole", [call("create_hole", diameter=10, depth=10)])
    assert codes(verdict) == [("number_reused", "diameter")] or codes(verdict) == [("number_reused", "depth")]
    assert verdict["overridable"] and "once" in verdict["issues"][0]["message"]
    assert judge("10 mm hole, 10 deep", [call("create_hole", diameter=10, depth=10)])["decision"] == "pass"


def test_calls_that_do_not_fit_the_state_are_held_back(judge):
    verdict = judge("extrude 10 mm", [call("extrude", distance=10)], state=BODY)
    assert codes(verdict) == [("state_need", "")] and not verdict["overridable"]
    assert verdict["issues"][0]["kind"] == STATE and "open sketch" in verdict["issues"][0]["message"]
    assert judge("extrude 10 mm", [call("extrude", distance=10)], state=SKETCH)["decision"] == "pass"
    assert codes(judge("fillet all edges R1", [call("fillet", radius=1, edges="all")], state=EMPTY)) == [("state_need", "")]
    assert codes(judge("polar pattern ×6", [call("circular_pattern", count=6)], state=EMPTY)) == [("state_need", "")]
    assert codes(judge("join the bodies", [call("combine", operation="join")], state=BODY)) == [("state_need", "")]
    # a state line that does not hold the fact is not judged
    assert judge("extrude 10 mm", [call("extrude", distance=10)], state="units: mm")["decision"] == "pass"


def test_a_compound_answer_may_create_what_its_later_calls_need(judge):
    plate = [call("create_sketch"), call("draw_rectangle", width=40, height=30), call("extrude", distance=10)]
    assert judge("plate 40x30x10", plate, state=EMPTY)["decision"] == "pass"
    drilled = plate + [call("create_hole", diameter=6)]
    assert judge("plate 40x30x10 with a Ø6 hole", drilled, state=EMPTY)["decision"] == "pass"
    # ... but not the other way round
    assert codes(judge("plate 40x30x10", [plate[1], plate[0], plate[2]], state=EMPTY)) == [("state_need", "")]


def test_schema_violations_are_never_runnable(judge):
    cases = [
        ("fillet -2 mm", [call("fillet", radius=-2)], "out_of_range"),
        ("fillet", [call("fillet")], "missing_required"),
        ("fillet 2 mm", [call("fillet", radius="2")], "wrong_type"),
        ("fillet 2 mm on the side edges", [call("fillet", radius=2, edges="side")], "bad_enum"),
        ("fillet 2 mm", [call("fillet", radius=2, size=2)], "unknown_param"),
        ("teleport it 2 mm", [call("teleport", distance=2)], "unknown_tool"),
        ("polar pattern ×1", [call("circular_pattern", count=1)], "out_of_range"),
        ("polar pattern ×2.5", [call("circular_pattern", count=2.5)], "wrong_type"),
        ("revolve 400 degrees around the x axis", [call("revolve", axis="x", angle=400)], "out_of_range"),
        ("fillet 2 mm", [{"name": "fillet", "arguments": [2]}], "bad_arguments"),
    ]
    for query, calls, code in cases:
        verdict = judge(query, calls)
        assert verdict["decision"] == "blocked" and not verdict["overridable"], query
        assert code in [issue["code"] for issue in verdict["issues"] if issue["kind"] == SCHEMA], query


def test_a_tool_that_was_not_offered_is_held_back(judge):
    verdict = judge("fillet 2 mm", [call("fillet", radius=2)], offered=["create_hole", "undo"])
    assert codes(verdict) == [("not_offered", "")] and not verdict["overridable"]


def test_the_renderers_own_refusal_counts_as_a_schema_finding(judge):
    verdict = judge("fillet 2 mm", [call("fillet", radius=2)], renderer_check=lambda name, arguments: "no template")
    assert verdict["issues"][0]["code"] == "renderer" and not verdict["overridable"]


# ---- good calls pass ---------------------------------------------------------------

GOOD = [
    ("6 mm hole at (10, 0), 5 deep", BODY, [call("create_hole", diameter=6, x=10, y=0, depth=5)]),
    ("fillet the top edges 2 mm", BODY, [call("fillet", radius=2, edges="top")]),
    ("extrude twenty millimetres", SKETCH, [call("extrude", distance=20)]),
    ("shell 2mm", BODY, [call("shell", thickness=2)]),
    ("pattern it 4 times along x, 15 mm apart", BODY, [call("rectangular_pattern", x_count=4, x_spacing=15)]),
    ("set material to aluminium", BODY, [call("set_material", material="aluminum")]),
    ("set material to aluminum", BODY, [call("set_material", material="aluminum")]),
    ("undo", BODY, [call("undo")]),
    ("plate 40x30x10", EMPTY, [call("create_sketch"), call("draw_rectangle", width=40, height=30),
                               call("extrude", distance=10)]),
    ("Ø6 hole at (10, 0)", BODY, [call("create_hole", diameter=6, x=10, y=0)]),
    ("polar pattern ×6", BODY, [call("circular_pattern", count=6)]),
    ("fillet the top edges R2", BODY, [call("fillet", radius=2, edges="top")]),
    ("cut through all", "units: mm; sketch: Sketch2 on top face; bodies: Body1; last_feature: Extrude1",
     [call("extrude", operation="cut", extent="through_all")]),
    ("new sketch on the top face", BODY, [call("create_sketch", face="top")]),
    ("hexagon 17 across flats", SKETCH, [call("draw_polygon", shape="hexagon", size=17, measure="flats")]),
    ("mirror across the yz plane", BODY, [call("mirror", plane="yz")]),
    ("revolve 180 degrees about the y axis", SKETCH, [call("revolve", axis="y", angle=180)]),
    ("set parameter wall to 3 mm", BODY, [call("set_parameter", name="wall", value=3, unit="mm")]),
    ("export an stl named bracket", BODY, [call("export_model", format="stl", name="bracket")]),
    ("screenshot from the front", BODY, [call("capture_view", direction="front")]),
]


@pytest.mark.parametrize("query, state, calls", GOOD, ids=[entry[0] for entry in GOOD])
def test_good_calls_pass(judge, query, state, calls):
    verdict = judge(query, calls, state=state)
    assert verdict["decision"] == "pass", verdict["issues"]


# ---- false-block regressions: how people write numbers ------------------------------

NUMBERS = [
    # number words
    ("extrude ten millimetres", SKETCH, [call("extrude", distance=10)]),
    ("extrude twenty five mm", SKETCH, [call("extrude", distance=25)]),
    ("extrude one hundred and twenty mm", SKETCH, [call("extrude", distance=120)]),
    ("drill six holes around the z axis", BODY, [call("circular_pattern", count=6, axis="z")]),
    ("pattern it four times along x, fifteen mm apart", BODY, [call("rectangular_pattern", x_count=4, x_spacing=15)]),
    # decimals, with a dot, a comma, a leading dot, a trailing zero
    ("chamfer all edges 0.5", BODY, [call("chamfer", distance=0.5, edges="all")]),
    ("chamfer all edges 0,5 mm", BODY, [call("chamfer", distance=0.5, edges="all")]),
    ("chamfer all edges .5", BODY, [call("chamfer", distance=0.5, edges="all")]),
    ("circle Ø12.50", SKETCH, [call("draw_circle", diameter=12.5)]),
    ("extrude 6.0", SKETCH, [call("extrude", distance=6)]),
    ("extrude 1,200 mm", SKETCH, [call("extrude", distance=1200)]),
    # units and symbols glued to the number
    ("extrude 20mm", SKETCH, [call("extrude", distance=20)]),
    ("Ø6 hole", BODY, [call("create_hole", diameter=6)]),
    ("6mm hole", BODY, [call("create_hole", diameter=6)]),
    ("set parameter wall to 3mm", BODY, [call("set_parameter", name="wall", value=3, unit="mm")]),
    ("rotate the view... revolve 90° around the x axis", SKETCH, [call("revolve", axis="x", angle=90)]),
    # negative numbers
    ("move it -5 in x and 2.5 in z", BODY, [call("move_body", x=-5, z=2.5)]),
    ("move the body minus 20 in y", BODY, [call("move_body", y=-20)]),
    ("hole Ø4 at (-19, -9)", BODY, [call("create_hole", diameter=4, x=-19, y=-9)]),
    ("line from (-15,20) to (0,0)", SKETCH, [call("draw_line", start_x=-15, start_y=20, end_x=0, end_y=0)]),
    # numbers that legitimately repeat, or stand for two sides
    ("10 by 10 square", SKETCH, [call("draw_rectangle", width=10, height=10)]),
    ("20 mm square", SKETCH, [call("draw_rectangle", width=20, height=20)]),
    ("rectangle 10x10 at (10, 10)", SKETCH, [call("draw_rectangle", width=10, height=10, center_x=10, center_y=10)]),
    ("pattern in a 3x3 grid, 35 pitch", BODY, [call("rectangular_pattern", x_count=3, y_count=3, x_spacing=35,
                                                    y_spacing=35)]),
    ("drill Ø4 holes at (-5, 0) and (5, 0)", BODY, [call("create_hole", diameter=4, x=-5, y=0),
                                                    call("create_hole", diameter=4, x=5, y=0)]),
    ("10 mm hole at (10, 0), 10 deep", BODY, [call("create_hole", diameter=10, x=10, y=0, depth=10)]),
    # a label that belongs to the next number, and words that only look like a label
    ("put a 8mm diameter hole at 0, -5 depth 6", BODY, [call("create_hole", diameter=8, x=0, y=-5, depth=6)]),
    ("arc R 15 from 15° to 180°", SKETCH, [call("draw_arc", radius=15, start_angle=15, end_angle=180)]),
    ("drill 6 dia 1.5 deep at (-25,0)", BODY, [call("create_hole", diameter=6, depth=1.5, x=-25, y=0)]),
    ("4 mm holes at (5, 5)", BODY, [call("create_hole", diameter=4, x=5, y=5)]),
    ("rectangle 40 × 30", SKETCH, [call("draw_rectangle", width=40, height=30)]),
    ("a hole 6 wide and 5 deep", BODY, [call("create_hole", diameter=6, depth=5)]),
    # an argument left at its schema default is the same call as without it
    ("Ø6 hole", BODY, [call("create_hole", diameter=6, x=0, y=0, face="top")]),
    ("extrude 10", SKETCH, [call("extrude", distance=10, operation="new_body", extent="distance")]),
]


@pytest.mark.parametrize("query, state, calls", NUMBERS, ids=[entry[0] for entry in NUMBERS])
def test_ways_of_writing_numbers_do_not_block(judge, query, state, calls):
    verdict = judge(query, calls, state=state)
    assert verdict["decision"] == "pass", verdict["issues"]


BAD_NUMBERS = [
    ("extrude 72 mm", SKETCH, [call("extrude", distance=2)], "not_in_request"),          # a token is a whole run
    ("extrude 10 mm", SKETCH, [call("extrude", distance=1)], "not_in_request"),          # nothing is converted
    ("extrude 2 cm", SKETCH, [call("extrude", distance=20)], "not_in_request"),
    ("make it half as thick", SKETCH, [call("extrude", distance=0.5)], "not_in_request"),
    ("extrude a few mm", SKETCH, [call("extrude", distance=3)], "not_in_request"),
    ("move it 5 in x", BODY, [call("move_body", x=-5)], "not_in_request"),                 # a minus needs a minus
    ("shell it", BODY, [call("shell", thickness=2)], "not_in_request"),
    ("fillet the edges 2 mm", BODY, [call("fillet", radius=2, edges="vertical")], "not_named"),
    ("make it metal", BODY, [call("set_material", material="steel")], "not_named"),
    ("open the bracket file", BODY, [call("open_document", name="Bracket")], "name_not_verbatim"),
    ("R5 circle", SKETCH, [call("draw_circle", diameter=5)], "wrong_label"),               # a radius is no diameter
    ("pattern 5 mm apart", BODY, [call("rectangular_pattern", x_count=5, x_spacing=5)], "wrong_label"),
    ("hole 5 deep", BODY, [call("create_hole", diameter=5, depth=5)], "wrong_label"),
]


@pytest.mark.parametrize("query, state, calls, code", BAD_NUMBERS, ids=[entry[0] for entry in BAD_NUMBERS])
def test_values_the_request_does_not_back_are_held_back(judge, query, state, calls, code):
    verdict = judge(query, calls, state=state)
    assert verdict["decision"] == "blocked" and verdict["overridable"], verdict
    assert code in [issue["code"] for issue in verdict["issues"]]
    assert all(issue["kind"] == EVIDENCE for issue in verdict["issues"])


def test_calls_written_by_hand_are_judged_for_schema_and_state_only(judge):
    assert judge("Ø6 hole", [call("create_hole", diameter=7)], evidence=False)["decision"] == "pass"
    assert judge("Ø6 hole", [call("create_hole", diameter=-7)], evidence=False)["decision"] == "blocked"
    assert judge("Ø6 hole", [call("create_hole", diameter=7)], state=EMPTY, evidence=False)["decision"] == "blocked"


# ---- pieces --------------------------------------------------------------------------

def test_fold_and_families():
    assert fold("Aluminium centre, 20mm x 5 millimetres") == "Aluminum center, 20 mm x 5 millimeters"
    assert fold("export as 3mf") == "export as 3mf"          # not a number with a unit
    assert [param_family(name) for name in ("diameter", "x_count", "center_x", "start_angle", "value", "depth")] == [
        "diameter", "count", "position", "angle", None, "size"]
    request = Request("4 holes, 5 mm margin, R2, ×6, 90 degrees, thirty one deep", {"holes"})
    said = {str(sorted(place.values)[0]): place.families for place in request.occurrences}
    assert said["4"] == {"count"} and said["5"] == {"position"} and said["2"] == {"radius"}
    assert said["6"] == {"count"} and said["9E+1"] == {"angle"}
    assert any(place.values == {30, 1, 31} and place.families == {"size"} for place in request.occurrences)


def test_tools_and_files_the_guard_has_never_seen():
    """New tools, enum values and missing rule files must not break it: what cannot be judged is skipped."""
    tool = {"name": "make_gear", "description": "Make a spur gear.", "parameters": {
        "type": "object", "required": ["teeth"], "properties": {
            "teeth": {"type": "integer", "minimum": 6}, "module": {"type": "number", "exclusiveMinimum": 0},
            "style": {"type": "string", "enum": ["spur", "helical"]}}}}
    bare = Guard(Rules())                                    # no synonyms.yaml, no toolsets.yaml
    good = [call("make_gear", teeth=20, module=2, style="helical")]
    assert bare.check("gear with 20 teeth, module 2", EMPTY, good, [tool])["decision"] == "pass"
    assert bare.check("gear with 20 teeth", EMPTY, [call("make_gear", teeth=20, module=2)], [tool])["issues"][0][
        "code"] == "not_in_request"
    assert bare.check("gear with 4 teeth", EMPTY, [call("make_gear", teeth=4)], [tool])["issues"][0][
        "code"] == "out_of_range"
    listed = Guard(Rules(synonyms={"make_gear": {"style": {"helical": ["helical", "angled teeth"]}}},
                         toolsets={"routing": {"needs": {"make_gear": "body", "other": "no-such-need"}},
                                   "groups": {"feature": ["make_gear"]}}))
    verdict = listed.check("spur gear with 20 teeth", EMPTY, [call("make_gear", teeth=20, style="spur")], [tool])
    assert [issue["code"] for issue in verdict["issues"]] == ["state_need"]      # "spur" is named in the request
    verdict = listed.check("gear with 20 teeth", BODY, [call("make_gear", teeth=20, style="helical")], [tool])
    assert [issue["code"] for issue in verdict["issues"]] == ["not_named"]
    assert Rules.load("/no/such/project").problems           # a project folder that is not there: said, not raised
    for junk in (None, "x", [None, 3, {"name": 7}], [{"arguments": {}}]):
        assert bare.check("anything", EMPTY, junk, [tool])["decision"] in ("pass", "blocked")
