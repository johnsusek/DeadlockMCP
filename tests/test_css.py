from deadlock_mcp.css import css_defines, css_diff_rules, css_to_rules, parse_selector


def test_parse_selector():
    assert parse_selector("Label.Name.Big") == [{"comb": " ", "type": "Label", "classes": ["Name", "Big"]}]
    assert parse_selector("#Hud > Panel .x") == [
        {"comb": " ", "id": "Hud"},
        {"comb": ">", "type": "Panel"},
        {"comb": " ", "classes": ["x"]},
    ]
    assert parse_selector("Button:hover") is None
    assert parse_selector("") is None


def test_css_to_rules():
    css = """
    /* comment */
    @define red: #f00;
    @keyframes 'pulse' { 0% { opacity: 0; } 100% { opacity: 1; } }
    #HudRoot .Health, Label.Ammo { color: #fff; font-size: 24px; background-color: gradient( linear, 0% 0%, 0% 100%, from( #000 ), to( #111 ) ); }
    Button:hover { wash-color: red; }
    """
    rules, warnings = css_to_rules(css)
    assert len(rules) == 2
    assert rules[0]["decl"]["font-size"] == "24px"
    assert rules[0]["decl"]["background-color"].startswith("gradient(")
    assert any("@keyframes" in w for w in warnings)
    assert any("Button:hover" in w for w in warnings)


def test_css_defines_and_diff():
    orig = """@define accent: #ff0000;
@define accent2: accent;
.GameTime { color: white; font-size: 20px; }
.A, .B { opacity: 1; }
Button:hover { color: red; }
"""
    edited = orig.replace("font-size: 20px", "font-size: 40px") + "\n.New { wash-color: accent2; }\n"
    rules, warnings = css_diff_rules(orig, edited)
    by = {r["sel"][0].get("classes", [""])[0]: r["decl"] for r in rules}
    assert by == {"GameTime": {"font-size": "40px"}, "New": {"wash-color": "#ff0000"}}
    rules2, w2 = css_to_rules("@define c: #123456;\n#X { color: c; border: 1px solid c; }")
    assert rules2[0]["decl"] == {"color": "#123456", "border": "1px solid #123456"}
    assert not any("@define" in w for w in w2)


def _decl(css):
    rules, warnings = css_to_rules(css)
    return {".".join(r["sel"][-1].get("classes", [])): r["decl"] for r in rules}, warnings


def test_strings_and_urls_are_opaque():
    by, w = _decl('.A { background-image: url("file://{images}/hud/a.png"); color: red; }\n.B { color: blue; }')
    assert by == {"A": {"background-image": 'url("file://{images}/hud/a.png")', "color": "red"}, "B": {"color": "blue"}}
    assert not w
    by, w = _decl('.A { background-image: url("s2r://panorama/images/foo@2x_png.vtex"); color: red; }')
    assert by["A"] == {"background-image": 'url("s2r://panorama/images/foo@2x_png.vtex")', "color": "red"}
    assert not w
    by, _ = _decl('.A { font-family: "a;b"; color: red; }')
    assert by["A"] == {"font-family": '"a;b"', "color": "red"}
    by, w = _decl(".A { background-image: url(file://{resources}/x.png); }")  # unquoted url
    assert by["A"] == {"background-image": "url(file://{resources}/x.png)"} and not w
    by, _ = _decl(".A { content: '/* not a comment */'; } /* {comment} with } braces { */ .B { x: 1; }")
    assert by == {"A": {"content": "'/* not a comment */'"}, "B": {"x": "1"}}


def test_values_and_at_rules():
    grad = "gradient( linear, 0% 0%, 0% 100%, from( #000 ), to( #111 ) )"
    by, w = _decl(f"@keyframes 'pulse' {{ 0% {{ opacity: 0; }} 100% {{ opacity: 1; }} }}\n.A, .B {{ background-color: {grad}; }}")
    assert by == {"A": {"background-color": grad}, "B": {"background-color": grad}}
    assert w == ["skipped at-rule (not supported live): @keyframes 'pulse'"]
    _, w = _decl("@media (max-width: 10px) { .A { x: 1; } }\n@import url('a.css');\n.C { y: 2; }")
    assert any("@media" in x for x in w) and any("@import" in x for x in w)
    for empty in ("", "  \n\t", "/* only */"):
        assert css_to_rules(empty) == ([], [])


def test_defines():
    assert css_defines('@define a: "x;y"; @define b: url("s2r://p/a@2x.vtex");') == {"a": '"x;y"', "b": 'url("s2r://p/a@2x.vtex")'}
    css = """@define base: #102030;
@define accent: base;
@define img: hud;
.A { color: accent; border: 1px solid accent; font-family: "accent img"; background-image: url("file://{images}/img.png");
     wash-color: rgba(accent, 0.5); width: imgs; }"""
    by, _ = _decl(css)
    assert by["A"] == {"color": "#102030", "border": "1px solid #102030", "font-family": '"accent img"',
                       "background-image": 'url("file://{images}/img.png")', "wash-color": "rgba(#102030, 0.5)",
                       "width": "imgs"}
    by, _ = _decl("@define a: b; @define b: a; .A { color: a; }")  # cyclic defines terminate
    assert "color" in by["A"]


def test_unterminated_input_warns():
    rules, w = css_to_rules(".A { color: red; }\n.B { color: blue;")
    assert len(rules) == 1 and any("unterminated block" in x for x in w)
    rules, w = css_to_rules('.A { font-family: "abc;\n color: red; }\n.B { color: blue; }')  # strings end at newline
    assert any("unterminated string" in x for x in w)
    assert rules[-1]["decl"] == {"color": "blue"}
    rules, w = css_to_rules(".A { color: red; } /* open")
    assert len(rules) == 1 and any("comment" in x for x in w)
    for bad in (".A", "@define", ".A { color: rgba(1, 2; }", "}}} .A {", '"', "@x {", ".A { : ; x }"):
        css_to_rules(bad)
        css_diff_rules("", bad)


def test_diff_uses_effective_declarations():
    orig = ".A { color: red; } .A { color: blue; }"
    # editing an overridden block changes nothing in the real cascade
    rules, w = css_diff_rules(orig, ".A { color: green; } .A { color: blue; }")
    assert rules == []
    # inserting a block before the others must not re-apply untouched declarations
    rules, w = css_diff_rules(orig, ".A { width: 1px; } " + orig)
    assert [r["decl"] for r in rules] == [{"width": "1px"}]
    assert not w
    rules, _ = css_diff_rules(orig, ".A { color: red; } .A { color: pink; }")
    assert [r["decl"] for r in rules] == [{"color": "pink"}]
    # selector spacing is normalised; selector lists compare per selector
    rules, w = css_diff_rules(".A>.B, .C { x: 1; }", ".A > .B { x: 1; }\n.C { x: 2; y: 3; }")
    assert [(r["sel"][-1]["classes"], r["decl"]) for r in rules] == [(["C"], {"x": "2", "y": "3"})]
    assert not w
    # removals warn; a changed @define re-applies the declarations using it
    rules, w = css_diff_rules("@define c: red;\n.A { color: c; width: 1px; }", "@define c: blue;\n.A { color: c; }")
    assert [r["decl"] for r in rules] == [{"color": "blue"}]
    assert any("removed" in x for x in w)
    # edited order of first appearance
    rules, _ = css_diff_rules("", ".B { b: 1; a: 2; } .A { z: 1; } .B { c: 3; }")
    assert [(r["sel"][0]["classes"][0], list(r["decl"])) for r in rules] == [("B", ["b", "a", "c"]), ("A", ["z"])]
