import pytest

from deadlock_mcp.errors import LayoutError
from deadlock_mcp.layout import add_stylesheet, ensure_root, inject_bridge, replace_stylesheet, stylesheet_includes


def test_xml_helpers():
    assert ensure_root("<root><Panel/></root>") == "<root><Panel/></root>"
    wrapped = ensure_root('<Label text="hi"/>')
    assert wrapped.startswith("<root>")
    styled = add_stylesheet(wrapped, "file://{resources}/styles/x.css")
    assert styled.index("<styles>") < styled.index("x.css") < styled.index("<Panel ")
    styled2 = add_stylesheet('<root>\n<styles>\n<include src="a.css" />\n</styles><Panel/></root>', "b.css")
    assert styled2.count("<styles>") == 1
    # added last, so it wins cascade ties against the layout's own stylesheets
    assert styled2.index("a.css") < styled2.index("b.css") < styled2.index("</styles>")
    one_line = add_stylesheet('<root><styles><include src="a.css" /></styles><Panel/></root>', "b.css")
    assert stylesheet_includes(one_line.replace("a.css", "styles/a.css").replace("b.css", "styles/b.css")) == [
        "styles/a.css", "styles/b.css"]


def test_inject_bridge():
    host = '<!-- banner -->\n<root>\n\t<styles>\n\t\t<include src="s2r://x.vcss" />\n\t</styles>\n\t<Panel class="X" />\n</root>\n'
    out = inject_bridge(host, "h")
    assert out.startswith("<root>")
    assert out.index("</styles>") < out.index("<scripts>") < out.index("mcp_bridge_h.js") < out.index("<Panel")
    with pytest.raises(LayoutError):
        inject_bridge(out, "h")


def test_root_with_attributes_and_comments():
    doc = '<root >\n\t<!-- <styles><include src="old.css" /></styles> <scripts></scripts> -->\n\t<Panel />\n</root>'
    assert ensure_root(doc) == doc
    out = add_stylesheet(doc, "x.css")
    assert out.index("-->") < out.index('<styles>\n\t\t<include src="x.css" />\n\t</styles>') < out.index("<Panel")
    out = inject_bridge(doc, "h")
    assert out.index("-->") < out.index("<scripts>\n\t\t<include") < out.index("mcp_bridge_h.js") < out.index("<Panel")
    assert ensure_root("<!-- <root> -->\n<Label />").startswith("<root>\n")
    attr = '<root class="X">\n\t<styles />\n\t<scripts>\n\t\t<include src="a.js" />\n\t</scripts>\n</root>'
    assert '<styles>\n\t\t<include src="b.css" />\n\t</styles>' in add_stylesheet(attr, "b.css")
    out = inject_bridge(attr)
    assert '<scripts>\n\t\t<include src="file://{resources}/scripts/mcp_bridge_m.js" />\n\t\t<include src="a.js" />' in out
    # a commented-out bridge include doesn't count as installed
    assert "mcp_bridge_m.js\" />\n" in inject_bridge('<root>\n<!-- mcp_bridge -->\n</root>')
    with pytest.raises(LayoutError):
        add_stylesheet("<!-- <root> --><Panel />", "x.css")
    with pytest.raises(LayoutError):
        inject_bridge("<Panel />")


LAYOUT = """<root>
\t<!-- <styles><include src="s2r://panorama/styles/commented.vcss" /></styles> -->
\t<styles>
\t\t<include src="s2r://panorama/styles/citadel_base_styles.vcss" />
\t\t<include src="s2r://panorama/styles/HUD_Common.vcss_c" />
\t\t<!-- <include src="s2r://panorama/styles/off.vcss" /> -->
\t\t<include src="s2r://panorama/styles/ability_hud_elements/abilities_artist.vcss" />
\t\t<include src="file://{resources}/styles/mine.css" />
\t</styles>
\t<scripts>
\t\t<include src="s2r://panorama/scripts/x.vjs_c" />
\t</scripts>
\t<Panel />
</root>
"""


def test_stylesheet_includes():
    assert stylesheet_includes(LAYOUT) == ["styles/citadel_base_styles.css", "styles/hud_common.css",
                                           "styles/ability_hud_elements/abilities_artist.css", "styles/mine.css"]
    assert stylesheet_includes("<root><Panel /></root>") == []


def test_replace_stylesheet():
    new = "file://{resources}/styles/mcp_live/n.css"
    out = replace_stylesheet(LAYOUT, "styles/ability_hud_elements/abilities_artist.css", new)
    assert f'<include src="{new}" />' in out and "abilities_artist" not in out
    assert out.replace(new, "s2r://panorama/styles/ability_hud_elements/abilities_artist.vcss") == LAYOUT
    assert stylesheet_includes(replace_stylesheet(LAYOUT, "styles/hud_common.css", new))[1] == "styles/mcp_live/n.css"
    with pytest.raises(LayoutError, match="styles/citadel_base_styles.css"):
        replace_stylesheet(LAYOUT, "styles/off.css", new)  # only commented out
