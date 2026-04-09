#!/usr/bin/env python3
"""Convert miMind mind map XML to Freeplane .mm format."""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TextRun:
    """A span of text with uniform formatting."""
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font: str = ""
    size_pt: int = 0
    color: str = ""  # "#RRGGBB"
    hyperlink: str = ""


@dataclass
class MiMindNode:
    node_id: str
    parent_id: str
    index: int = 0
    text: str = ""
    text_runs: list[TextRun] = field(default_factory=list)
    background_color: str = ""
    frame_color: str = ""
    text_color: str = ""
    font_name: str = ""
    font_size: int = 0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    folded: bool = False
    notes: str = ""
    shape_type: str = ""
    layout_direction: int = 0
    color_schema_level: int = -1
    color_groups: list[dict] = field(default_factory=list)
    children: list["MiMindNode"] = field(default_factory=list)
    # Positioning
    relative_x: float = 0.0
    relative_y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    # Edge info (from parent-child connection pointing to this node)
    edge_color: str = ""
    edge_width: int = 0
    # Cross-links originating from this node
    cross_links: list[dict] = field(default_factory=list)


@dataclass
class MiMindConnection:
    origin_id: str
    target_id: str
    is_cross_link: bool
    color: str = ""
    line_width: int = 0
    label: str = ""


# ---------------------------------------------------------------------------
# XML Preprocessor
# ---------------------------------------------------------------------------

def preprocess_xml(raw: str) -> str:
    """Fix miMind's non-standard XML so ElementTree can parse it."""
    # 1. Prefix numeric attribute names on mp* elements with 'p'
    #    e.g. <mpBold 0="1" 17="0"> → <mpBold p0="1" p17="0">
    raw = re.sub(
        r'(<mp\w+)\s',
        lambda m: m.group(0),  # keep the tag start
        raw,
    )
    # More targeted: replace ' 123="' patterns inside mp* tags
    # We process each mp* element individually
    def fix_mp_tag(match):
        tag_content = match.group(0)
        # Replace numeric attribute names: space + digits + ="
        tag_content = re.sub(r'\s(\d+)="', r' p\1="', tag_content)
        return tag_content

    raw = re.sub(r'<mp\w+[^>]*>', fix_mp_tag, raw)

    # 2. Fix bracket attributes: vecUserAdjustedMidPts[0]="val"
    raw = re.sub(r'\[(\d+)\]="', r'_\1="', raw)

    return raw


# ---------------------------------------------------------------------------
# Color conversion
# ---------------------------------------------------------------------------

def rgba_to_hex(color_str: str) -> str:
    """Convert miMind 'R,G,B,A,flag' to '#RRGGBB'. Returns '' for invalid/transparent."""
    if not color_str:
        return ""
    parts = color_str.split(",")
    if len(parts) < 3:
        return ""
    try:
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return ""
    return f"#{r:02x}{g:02x}{b:02x}"


def is_transparent(color_str: str) -> bool:
    """Check if a miMind color is fully transparent."""
    if not color_str:
        return True
    parts = color_str.split(",")
    if len(parts) >= 4:
        try:
            alpha = int(parts[3])
            return alpha == 0
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Font size conversion
# ---------------------------------------------------------------------------

def mimind_height_to_pt(height: float) -> int:
    """Convert miMind text height (pixels) to font point size."""
    if height <= 0:
        return 12  # default
    # miMind heights are roughly 1.33x point sizes (96dpi vs 72dpi)
    pt = round(height * 0.75)
    return max(6, min(pt, 144))  # clamp to reasonable range


# ---------------------------------------------------------------------------
# Text format parsing
# ---------------------------------------------------------------------------

def parse_format_map(element: ET.Element, prefix: str = "p") -> dict[int, str]:
    """Parse a mp* element's positional attributes into {position: value}.

    After preprocessing, attributes like p0="1" p17="0" become a dict
    {0: "1", 17: "0"}.
    """
    result = {}
    if element is None:
        return result
    for attr_name, attr_val in element.attrib.items():
        if attr_name.startswith(prefix):
            try:
                pos = int(attr_name[len(prefix):])
                result[pos] = attr_val
            except ValueError:
                pass
    return result


def build_text_runs(text: str, text_format: ET.Element) -> list[TextRun]:
    """Build a list of TextRun objects from text and its TextFormat element."""
    if not text or text_format is None:
        if text:
            return [TextRun(text=text)]
        return []

    # Parse each formatting channel
    bold_map = parse_format_map(text_format.find("mpBold"))
    italic_map = parse_format_map(text_format.find("mpItalic"))
    underline_map = parse_format_map(text_format.find("mpUnderline"))
    height_map = parse_format_map(text_format.find("mpTextHeight"))
    color_map = parse_format_map(text_format.find("mpTextColor"))
    font_map = parse_format_map(text_format.find("mpTextFont"))
    hyperlink_map = parse_format_map(text_format.find("mpHyperlink"))

    # Collect all change positions
    positions = set()
    for m in [bold_map, italic_map, underline_map, height_map, color_map, font_map, hyperlink_map]:
        positions.update(m.keys())
    positions.add(0)
    positions = sorted(positions)

    # Build runs
    runs = []
    current_bold = False
    current_italic = False
    current_underline = False
    current_height = 0.0
    current_color = ""
    current_font = ""
    current_hyperlink = ""

    for i, pos in enumerate(positions):
        # Update state at this position
        if pos in bold_map:
            current_bold = bold_map[pos] == "1"
        if pos in italic_map:
            current_italic = italic_map[pos] == "1"
        if pos in underline_map:
            current_underline = underline_map[pos] == "1"
        if pos in height_map:
            try:
                current_height = float(height_map[pos])
            except ValueError:
                pass
        if pos in color_map:
            current_color = rgba_to_hex(color_map[pos])
        if pos in font_map:
            current_font = font_map[pos]
        if pos in hyperlink_map:
            current_hyperlink = hyperlink_map[pos]

        # Determine text slice
        start = pos
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        if start >= len(text):
            break
        end = min(end, len(text))
        slice_text = text[start:end]
        if not slice_text:
            continue

        runs.append(TextRun(
            text=slice_text,
            bold=current_bold,
            italic=current_italic,
            underline=current_underline,
            font=current_font,
            size_pt=mimind_height_to_pt(current_height),
            color=current_color,
            hyperlink=current_hyperlink,
        ))

    if not runs and text:
        runs = [TextRun(text=text)]

    return runs


def has_mixed_formatting(runs: list[TextRun]) -> bool:
    """Check if runs have varying formatting (need richcontent)."""
    if len(runs) <= 1:
        return False
    first = runs[0]
    for r in runs[1:]:
        if (r.bold != first.bold or r.italic != first.italic or
                r.underline != first.underline or r.font != first.font or
                r.size_pt != first.size_pt or r.color != first.color or
                r.hyperlink != first.hyperlink):
            return True
    return False


# ---------------------------------------------------------------------------
# Parse miMind Content.xml
# ---------------------------------------------------------------------------

def parse_mimind(content_path: str) -> tuple[dict[str, MiMindNode], list[MiMindConnection], str]:
    """Parse Content.xml and return (nodes_dict, connections_list, bg_color)."""
    with open(content_path, "r", encoding="utf-8") as f:
        raw = f.read()

    raw = preprocess_xml(raw)
    root = ET.fromstring(raw)

    bg_color = rgba_to_hex(root.get("cBackgroundColor", ""))

    nodes: dict[str, MiMindNode] = {}
    connections: list[MiMindConnection] = []

    for mm_node in root.findall("MindMapNode"):
        node = _parse_node(mm_node)
        nodes[node.node_id] = node

        # Parse connections inside this node
        for mm_conn in mm_node.findall("MindMapConnection"):
            conn = _parse_connection(mm_conn)
            connections.append(conn)

    return nodes, connections, bg_color


def _parse_node(mm_node: ET.Element) -> MiMindNode:
    """Parse a single MindMapNode element."""
    node_id = mm_node.get("iNodeID", "")
    parent_id = mm_node.get("iParentID", "0")

    try:
        index = int(mm_node.get("iIndex", "0"))
    except ValueError:
        index = 0

    folded = mm_node.get("bSubNodesExpanded", "1") == "0"

    try:
        layout_dir = int(mm_node.get("eSubNodeLayoutDirection", "0"))
    except ValueError:
        layout_dir = 0

    # Extract positioning
    try:
        rel_x = float(mm_node.get("iRelativeX", "0"))
    except ValueError:
        rel_x = 0.0
    try:
        rel_y = float(mm_node.get("iRelativeY", "0"))
    except ValueError:
        rel_y = 0.0
    try:
        width = float(mm_node.get("iWidth", "0"))
    except ValueError:
        width = 0.0
    try:
        height = float(mm_node.get("iHeight", "0"))
    except ValueError:
        height = 0.0

    # Extract text content
    text = ""
    text_runs = []
    child_el = mm_node.find("MindMapNodeChild")
    if child_el is not None:
        edit_text = child_el.find("EditText")
        if edit_text is not None:
            text_el = edit_text.find("Text")
            if text_el is not None and text_el.text:
                text = text_el.text
            # Parse formatting from TextFormat (not RSpriteTextFormat)
            tf = edit_text.find("TextFormat")
            if tf is not None:
                text_runs = build_text_runs(text, tf)

    # Parse NodeShape
    bg_color = ""
    frame_color = ""
    shape_type = ""
    node_shape = mm_node.find("NodeShape")
    if node_shape is not None:
        bg_color = rgba_to_hex(node_shape.get("cBackColor", ""))
        frame_color = rgba_to_hex(node_shape.get("cFrameColor", ""))
        shape_type = node_shape.get("type", "")

    # Parse text color from node-level TextFormat (not inside EditText)
    text_color = ""
    node_tf = None
    for tf in mm_node.findall("TextFormat"):
        # The node-level TextFormat is directly under MindMapNode
        # (not inside MindMapNodeChild/EditText)
        if tf.find("mpTextColor") is not None:
            node_tf = tf
    if node_tf is not None:
        tc_map = parse_format_map(node_tf.find("mpTextColor"))
        if 0 in tc_map:
            text_color = rgba_to_hex(tc_map[0])

    # Primary text formatting (from the EditText's TextFormat)
    font_name = ""
    font_size = 0
    bold = False
    italic = False
    underline = False
    if text_runs:
        first_run = text_runs[0]
        font_name = first_run.font
        font_size = first_run.size_pt
        bold = first_run.bold
        italic = first_run.italic
        underline = first_run.underline
        if not text_color and first_run.color:
            text_color = first_run.color

    # Parse notes
    notes = ""
    notes_el = mm_node.find("sNotes")
    if notes_el is not None and notes_el.text:
        notes = notes_el.text.strip()

    # Parse color schema
    color_groups = []
    color_schema_level = -1
    cs = mm_node.find("colorSchema")
    if cs is not None:
        try:
            color_schema_level = int(cs.get("level", "-1"))
        except ValueError:
            color_schema_level = -1
        for cg in cs.findall("ColorGroup"):
            color_groups.append({
                "background": rgba_to_hex(cg.get("cColorBackground", "")),
                "frame": rgba_to_hex(cg.get("cColorFrame", "")),
                "text": rgba_to_hex(cg.get("cColorText", "")),
                "title": rgba_to_hex(cg.get("cColorTitle", "")),
            })

    return MiMindNode(
        node_id=node_id,
        parent_id=parent_id,
        index=index,
        text=text,
        text_runs=text_runs,
        background_color=bg_color,
        frame_color=frame_color,
        text_color=text_color,
        font_name=font_name,
        font_size=font_size,
        bold=bold,
        italic=italic,
        underline=underline,
        folded=folded,
        notes=notes,
        shape_type=shape_type,
        layout_direction=layout_dir,
        relative_x=rel_x,
        relative_y=rel_y,
        width=width,
        height=height,
        color_schema_level=color_schema_level,
        color_groups=color_groups,
    )


def _parse_connection(mm_conn: ET.Element) -> MiMindConnection:
    """Parse a single MindMapConnection element."""
    origin_id = mm_conn.get("iOriginNodeID", "")
    target_id = mm_conn.get("iTargetNodeID", "")
    is_cross = mm_conn.get("bIsCrossLink", "0") == "1"
    color = rgba_to_hex(mm_conn.get("cColor", ""))

    try:
        line_width = int(float(mm_conn.get("fLineWidth", "0")))
    except ValueError:
        line_width = 0

    # Parse label from EditText with significance="3"
    label = ""
    for edit_text in mm_conn.findall("EditText"):
        tf = edit_text.find("TextFormat")
        if tf is not None and tf.get("significance") == "3":
            text_el = edit_text.find("Text")
            if text_el is not None and text_el.text:
                label = text_el.text.strip()
            break

    return MiMindConnection(
        origin_id=origin_id,
        target_id=target_id,
        is_cross_link=is_cross,
        color=color,
        line_width=line_width,
        label=label,
    )


# ---------------------------------------------------------------------------
# Build tree
# ---------------------------------------------------------------------------

def build_tree(
    nodes: dict[str, MiMindNode],
    connections: list[MiMindConnection],
    map_name: str,
) -> MiMindNode:
    """Build a nested tree from flat nodes. Returns synthetic root."""
    # Assign edge info from parent-child connections
    for conn in connections:
        if conn.is_cross_link:
            # Store cross-link on origin node
            if conn.origin_id in nodes:
                nodes[conn.origin_id].cross_links.append({
                    "target_id": conn.target_id,
                    "color": conn.color,
                    "label": conn.label,
                })
        else:
            # Parent-child connection → edge styling on child
            if conn.target_id in nodes:
                target = nodes[conn.target_id]
                if not target.edge_color and conn.color:
                    target.edge_color = conn.color
                if not target.edge_width and conn.line_width:
                    target.edge_width = conn.line_width

    # Build parent → children mapping
    children_by_parent: dict[str, list[MiMindNode]] = {}
    root_nodes: list[MiMindNode] = []

    for node in nodes.values():
        if node.parent_id == "0":
            root_nodes.append(node)
        else:
            children_by_parent.setdefault(node.parent_id, []).append(node)

    # Attach children recursively
    for node in nodes.values():
        node.children = sorted(
            children_by_parent.get(node.node_id, []),
            key=lambda n: n.index,
        )

    # Sort root nodes by index
    root_nodes.sort(key=lambda n: n.index)

    # Create synthetic root
    synthetic = MiMindNode(
        node_id="synthetic_root",
        parent_id="",
        text=map_name,
        children=root_nodes,
    )

    return synthetic


# ---------------------------------------------------------------------------
# Freeplane XML generation
# ---------------------------------------------------------------------------

def generate_freeplane(root: MiMindNode, bg_color: str) -> str:
    """Generate a Freeplane .mm XML string from the node tree."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<map version="freeplane 1.12.1">')

    _emit_node(root, lines, depth=0, is_root=True)

    lines.append("</map>")
    return "\n".join(lines)


def _indent(depth: int) -> str:
    return "  " * (depth + 1)


def _emit_node(node: MiMindNode, lines: list[str], depth: int, is_root: bool = False, parent: MiMindNode | None = None):
    """Recursively emit a <node> element."""
    ind = _indent(depth)
    attrs = []

    # ID
    if node.node_id == "synthetic_root":
        attrs.append('ID="ID_root"')
    else:
        attrs.append(f'ID="ID_{node.node_id}"')

    # Determine if we need richcontent for the node text
    use_richcontent = has_mixed_formatting(node.text_runs) or _text_has_newlines(node.text)

    if not use_richcontent:
        # Simple TEXT attribute
        text_escaped = escape(node.text).replace('"', '&quot;').replace("\n", " ")
        attrs.append(f'TEXT="{text_escaped}"')

    # Colors
    if node.background_color:
        attrs.append(f'BACKGROUND_COLOR="{node.background_color}"')
    if node.text_color:
        attrs.append(f'COLOR="{node.text_color}"')

    # Fold state
    if node.folded:
        attrs.append('FOLDED="true"')

    # Compute delta from parent's absolute canvas position
    # miMind iRelativeX/Y are ABSOLUTE canvas coords, not relative to parent
    if node.node_id != "synthetic_root" and parent is not None:
        delta_x = node.relative_x - parent.relative_x
        delta_y = node.relative_y - parent.relative_y

        # Position (left/right side of parent)
        # miMind's X axis is inverted relative to Freeplane's branch direction
        if delta_x < 0:
            attrs.append('POSITION="right"')
        else:
            attrs.append('POSITION="left"')

        # HGAP = horizontal distance from parent, VSHIFT = vertical offset
        hgap = round(abs(delta_x))
        vshift = round(delta_y)
        if hgap != 0 or vshift != 0:
            attrs.append(f'HGAP_QUANTITY="{hgap} px"')
            attrs.append(f'VSHIFT_QUANTITY="{vshift} px"')

    # Node dimensions
    if node.width > 0:
        attrs.append(f'MIN_WIDTH="{round(node.width)}"')
        attrs.append(f'MAX_WIDTH="{round(node.width)}"')

    attr_str = " ".join(attrs)
    lines.append(f"{ind}<node {attr_str}>")

    # Font element
    if node.font_name or node.font_size or node.bold or node.italic:
        font_attrs = []
        if node.font_name:
            font_attrs.append(f'NAME="{escape(node.font_name)}"')
        if node.font_size:
            font_attrs.append(f'SIZE="{node.font_size}"')
        if node.bold:
            font_attrs.append('BOLD="true"')
        if node.italic:
            font_attrs.append('ITALIC="true"')
        lines.append(f"{ind}  <font {' '.join(font_attrs)}/>")

    # Edge element
    if node.edge_color or node.edge_width:
        edge_attrs = []
        if node.edge_color:
            edge_attrs.append(f'COLOR="{node.edge_color}"')
        if node.edge_width:
            edge_attrs.append(f'WIDTH="{node.edge_width}"')
        lines.append(f"{ind}  <edge {' '.join(edge_attrs)}/>")

    # Rich content for node text (when mixed formatting or multiline)
    if use_richcontent:
        _emit_richcontent(node, lines, ind + "  ")

    # Notes
    if node.notes:
        _emit_notes(node.notes, lines, ind + "  ")

    # Cross-links (arrowlinks)
    for cl in node.cross_links:
        al_attrs = [f'DESTINATION="ID_{cl["target_id"]}"']
        if cl.get("color"):
            al_attrs.append(f'COLOR="{cl["color"]}"')
        if cl.get("label"):
            al_attrs.append(f'MIDDLE_LABEL="{escape(cl["label"]).replace(chr(10), " ")}"')
        al_attrs.append('STARTARROW="NONE"')
        al_attrs.append('ENDARROW="DEFAULT"')
        lines.append(f'{ind}  <arrowlink {" ".join(al_attrs)}/>')

    # Children
    for child in node.children:
        _emit_node(child, lines, depth + 1, parent=node)

    lines.append(f"{ind}</node>")


def _text_has_newlines(text: str) -> bool:
    """Check if text contains newlines that should be preserved."""
    return "\n" in text if text else False


def _emit_richcontent(node: MiMindNode, lines: list[str], ind: str):
    """Emit <richcontent TYPE='NODE'> with formatted HTML."""
    lines.append(f'{ind}<richcontent TYPE="NODE"><html><head/><body>')

    if node.text_runs and has_mixed_formatting(node.text_runs):
        # Split runs by newlines and emit paragraphs
        _emit_formatted_paragraphs(node.text_runs, lines, ind + "  ")
    else:
        # Simple multiline text → paragraphs
        for paragraph in node.text.split("\n"):
            p_text = escape(paragraph) if paragraph.strip() else "&#160;"
            lines.append(f"{ind}  <p>{p_text}</p>")

    lines.append(f"{ind}</body></html></richcontent>")


def _emit_formatted_paragraphs(runs: list[TextRun], lines: list[str], ind: str):
    """Emit text runs as HTML paragraphs, splitting on newlines."""
    # Split runs into lines (at newline boundaries)
    current_line_runs: list[TextRun] = []

    for run in runs:
        parts = run.text.split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                # Emit current line as a paragraph, then start new line
                _emit_paragraph(current_line_runs, lines, ind)
                current_line_runs = []
            if part:  # skip empty parts that just represent the newline itself
                current_line_runs.append(TextRun(
                    text=part,
                    bold=run.bold,
                    italic=run.italic,
                    underline=run.underline,
                    font=run.font,
                    size_pt=run.size_pt,
                    color=run.color,
                    hyperlink=run.hyperlink,
                ))

    # Emit remaining line
    if current_line_runs:
        _emit_paragraph(current_line_runs, lines, ind)
    elif runs and runs[-1].text.endswith("\n"):
        # Trailing newline → empty paragraph
        lines.append(f"{ind}<p>&#160;</p>")


def _emit_paragraph(runs: list[TextRun], lines: list[str], ind: str):
    """Emit a single <p> with styled spans."""
    if not runs:
        lines.append(f"{ind}<p>&#160;</p>")
        return

    parts = []
    for run in runs:
        text = escape(run.text)
        if not text:
            continue

        styles = []
        if run.font:
            styles.append(f"font-family: {run.font}")
        if run.size_pt:
            styles.append(f"font-size: {run.size_pt}pt")
        if run.color:
            styles.append(f"color: {run.color}")
        if run.bold:
            styles.append("font-weight: bold")
        if run.italic:
            styles.append("font-style: italic")

        # Build the element
        content = text
        if run.underline:
            content = f"<u>{content}</u>"
        if run.hyperlink:
            content = f'<a href="{escape(run.hyperlink)}">{content}</a>'

        if styles:
            style_str = "; ".join(styles)
            content = f'<span style="{style_str}">{content}</span>'

        parts.append(content)

    if parts:
        lines.append(f'{ind}<p>{"".join(parts)}</p>')
    else:
        lines.append(f"{ind}<p>&#160;</p>")


def _emit_notes(notes: str, lines: list[str], ind: str):
    """Emit <richcontent TYPE='NOTE'> for node notes."""
    lines.append(f'{ind}<richcontent TYPE="NOTE"><html><head/><body>')
    for paragraph in notes.split("\n"):
        p_text = escape(paragraph) if paragraph.strip() else "&#160;"
        lines.append(f"{ind}  <p>{p_text}</p>")
    lines.append(f"{ind}</body></html></richcontent>")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def convert(input_path: str, output_path: str | None = None):
    """Run the full conversion pipeline."""
    input_p = Path(input_path)

    # Determine input files
    if input_p.is_dir():
        content_path = input_p / "Content.xml"
        map_name = input_p.name
    elif input_p.name == "Content.xml":
        content_path = input_p
        map_name = input_p.parent.name
    else:
        content_path = input_p
        map_name = input_p.stem

    if not content_path.exists():
        print(f"Error: {content_path} not found", file=sys.stderr)
        sys.exit(1)

    # Determine output path
    if output_path is None:
        output_path = str(content_path.parent / f"{map_name}.mm")

    print(f"Parsing {content_path}...")
    nodes, connections, bg_color = parse_mimind(str(content_path))
    print(f"  Found {len(nodes)} nodes, {len(connections)} connections")

    cross_links = [c for c in connections if c.is_cross_link]
    parent_child = [c for c in connections if not c.is_cross_link]
    print(f"  Parent-child: {len(parent_child)}, Cross-links: {len(cross_links)}")

    print("Building tree...")
    root = build_tree(nodes, connections, map_name)

    def count_nodes(n: MiMindNode) -> int:
        return 1 + sum(count_nodes(c) for c in n.children)

    total = count_nodes(root)
    print(f"  Tree has {total} nodes (including synthetic root)")

    print("Generating Freeplane XML...")
    xml_out = generate_freeplane(root, bg_color)

    # Validate output is well-formed XML
    try:
        ET.fromstring(xml_out)
    except ET.ParseError as e:
        print(f"Warning: Output XML validation failed: {e}", file=sys.stderr)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_out)

    print(f"Written to {output_path}")
    print(f"  Output size: {len(xml_out):,} bytes")


def main():
    parser = argparse.ArgumentParser(description="Convert miMind XML to Freeplane .mm format")
    parser.add_argument("input", help="Path to Content.xml or directory containing it")
    parser.add_argument("-o", "--output", help="Output .mm file path (default: auto)")
    args = parser.parse_args()
    convert(args.input, args.output)


if __name__ == "__main__":
    main()
