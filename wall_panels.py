"""
Freehand wall panels: a standalone framed or flat panel assembly that
does not need a wall, a cabinet run, or any other reference geometry --
"Add Wall Panel" drops one wherever the 3D cursor is.

A wall panel is a root cage (IS_WALL_PANEL_CAGE, the same GeoNodeCage
every other product root uses) holding one top-level opening. Split
Opening turns any leaf opening into a row or column of sub-openings
with a real stile/rail between them -- the same order of operations
Face_Frame_Split._create_bay_mid_rail / _create_bay_mid_stile already
uses for a cabinet's internal bays: frame members are real, fixed
width first, whatever width or height is left over is shared by the
children. That is the "treat each panel as an assembly, split it like
a standard opening, keep the gap logic" behaviour from the CV attempts,
just built on Home Builder's own solver instead of a UCS re-doing it by
hand.

Every leaf opening carries a FRONT, built by whatever function is
registered for its front_type. SLAB and SHAKER both route straight into
door_builder.build_door_mesh -- the same real 5-piece door engine wood
hood doors and (behind USE_PYTHON_DOORS) cabinet fronts already use --
so raised panels, mid rails/stiles, edge profiles and mitered frames
all come for free instead of being reimplemented here. Anything
door_builder cannot do (a slat wall, a sine-driven surface, whatever
comes next) is a new entry through register_front_builder(): the
opening/split/resize machinery below never needs to change for it.

Geometry convention, matching backsplash.py and the countertop
generator's wall-local frame:
    local X = panel width      (0 .. Dim X)
    local Z = panel height     (0 .. Dim Z, bottom to top)
    local Y = thickness        (0 at the back/wall face, +Y toward the room)
A leaf's front part is authored in door_builder's own cutpart-local
space -- per build_door_mesh's own convention, height runs +X from the
bottom edge and width runs -Y from the left edge (Mirror Y set) -- and
then carried onto that frame by the same rotation the wood hood's
static doors use (rotation (0, -90, 90), Mirror Y=True, Length=height,
Width=width), so the two systems agree on "which way is up" without a
second mapping to get wrong.

Frame members (stiles/rails, and the root's own split bookkeeping) sit
directly under the wall panel root rather than nested opening-inside-
opening -- every opening's Dim X/Z and location.x/z are already in the
root's local frame, which keeps resize() a straight walk instead of a
chain of nested local-space conversions.
"""

import math

import bpy

from . import hb_types

try:
    from .product_libraries.common import door_builder
except ImportError:                     # pragma: no cover - import-time safety
    door_builder = None

TAG = 'IS_WALL_PANEL_CAGE'
OPENING_TAG = 'IS_WALL_PANEL_OPENING'
FRAME_MEMBER_TAG = 'IS_WALL_PANEL_FRAME_MEMBER'
FRONT_PART_TAG = 'IS_WALL_PANEL_FRONT'

MENU_ID = 'HOME_BUILDER_MT_wall_panel_commands'
# Root, opening and front-part objects all share one right-click menu;
# HOME_BUILDER_MT_wall_panel_commands.draw() branches on what is
# actually selected (see operators/ops_wall_panels.py).
OPENING_MENU_ID = MENU_ID

INCH = 0.0254
TOL = 1e-5

DEFAULT_WIDTH = 24 * INCH
DEFAULT_HEIGHT = 30 * INCH
DEFAULT_THICKNESS = 0.75 * INCH
DEFAULT_SPLITTER_WIDTH = 2.5 * INCH
MIN_OPENING = 2 * INCH

DEFAULT_FRONT_TYPE = 'SLAB'


# ---------------------------------------------------------------------------
# Front registry -- the extension point
# ---------------------------------------------------------------------------

_FRONT_BUILDERS = {}
_FRONT_LABELS = {}


def register_front_builder(front_type, build_fn, label, description="",
                           icon='MESH_PLANE'):
    """Register a front type.

    build_fn(mesh, width, height, thickness, opts) must fill `mesh`
    (a fresh or reused bpy.types.Mesh) in door_builder's cutpart-local
    space -- see build_door_mesh's own docstring: height runs +X from
    the bottom edge, width runs -Y from the left edge, thickness
    through Z. `opts` is whatever dict was passed to assign_front (a
    door style's fields, a slat spec, ...) or None.

    Can be called from anywhere -- a future slat_wall.py -- at register
    time; front types do not need to live in this module.
    """
    _FRONT_BUILDERS[front_type] = build_fn
    _FRONT_LABELS[front_type] = (label, description, icon)


def unregister_front_builder(front_type):
    _FRONT_BUILDERS.pop(front_type, None)
    _FRONT_LABELS.pop(front_type, None)


def front_type_items(self=None, context=None):
    """EnumProperty items callback: every currently-registered front."""
    return [(key, lbl, desc, icon, i)
            for i, (key, (lbl, desc, icon)) in enumerate(_FRONT_LABELS.items())]


def _door_builder_front(door_type):
    """A front builder that hands off to door_builder.build_door_mesh.
    Covers SLAB (flat) and 5_PIECE (shaker / raised panel, with
    whatever mid rail / mid stile / panel fields `opts` supplies) --
    door_builder already knows both, so no new mesh code is needed."""

    def build(mesh, width, height, thickness, opts):
        info = door_builder.door_style_info(None)
        info['door_type'] = door_type
        if opts:
            info.update(opts)
        if door_type != 'SLAB':
            min_w, min_h = door_builder.layout_min_size(info)
            if width <= min_w or height <= min_h:
                # Too small for stiles/rails to fit -- fall back rather
                # than building overlapping/negative-width members.
                info = dict(info, door_type='SLAB')
        door_builder.build_door_mesh(mesh, info, width, height, thickness)

    return build


if door_builder is not None:
    register_front_builder(
        'SLAB', _door_builder_front('SLAB'), "Slab",
        "Flat panel, no frame", 'MESH_PLANE')
    register_front_builder(
        'SHAKER', _door_builder_front('5_PIECE'), "Shaker / Raised Panel",
        "Stiles, rails and an inset or raised center panel", 'MOD_BUILD')


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

def is_wall_panel(obj):
    return bool(obj is not None and obj.get(TAG))


def is_opening(obj):
    return bool(obj is not None and obj.get(OPENING_TAG))


def is_frame_member(obj):
    return bool(obj is not None and obj.get(FRAME_MEMBER_TAG))


def root_of(obj):
    """The wall panel root above obj (itself included), or None."""
    node = obj
    while node is not None:
        if node.get(TAG):
            return node
        node = node.parent
    return None


def top_opening_of(root):
    """The single opening parented straight under the root (wp_parent
    == ''), whether it is still a leaf or has since been split."""
    for child in root.children:
        if is_opening(child) and child.get('wp_parent', '') == '':
            return child
    return None


def _members(root, parent_name):
    """Every opening / frame member split off of the opening named
    parent_name, in creation order (root.children preserves it)."""
    return [o for o in root.children if o.get('wp_parent') == parent_name]


def leaves_of(root):
    return [o for o in root.children if is_opening(o) and o.get('wp_leaf', True)]


def _dim(obj, key):
    return hb_types.GeoNodeCage(obj).get_input(key)


def _set_dim(obj, key, value):
    hb_types.GeoNodeCage(obj).set_input(key, value)


def get_size(root):
    """(width, height, thickness) of a wall panel root."""
    return (_dim(root, 'Dim X'), _dim(root, 'Dim Z'), _dim(root, 'Dim Y'))


# ---------------------------------------------------------------------------
# Fronts
# ---------------------------------------------------------------------------

def _clear_front(opening):
    for child in list(opening.children):
        if child.get(FRONT_PART_TAG):
            bpy.data.objects.remove(child, do_unlink=True)


def assign_front(opening, front_type, opts=None):
    """Build (or rebuild) the front filling one leaf opening. Safe to
    call again after a resize -- it always clears its own old front
    first, and remembers front_type/opts on the opening so resize() can
    call it again without the caller having to track that."""
    _clear_front(opening)
    opening['wp_front_type'] = front_type
    if opts is not None:
        opening['wp_front_opts'] = opts
    opts = opts if opts is not None else opening.get('wp_front_opts')

    builder = _FRONT_BUILDERS.get(front_type)
    if builder is None:
        return None

    root = root_of(opening)
    width = _dim(opening, 'Dim X')
    height = _dim(opening, 'Dim Z')
    thickness = _dim(root, 'Dim Y') if root is not None else DEFAULT_THICKNESS

    part = hb_types.GeoNodeCutpart()
    part.create("Wall Panel Front")
    part.obj.parent = opening
    part.obj.matrix_parent_inverse.identity()
    part.obj[FRONT_PART_TAG] = True
    part.obj['hb_part_role'] = 'Wall Panel Front'
    part.obj['MENU_ID'] = MENU_ID

    # Same reorientation wood_hoods._static_hood_door uses to carry a
    # door_builder mesh (authored height+X, width-Y) onto a frame where
    # width runs +X, height +Z and thickness +Y: rotate (0, -90, 90),
    # Length=height, Width=width, Mirror Y=True.
    part.obj.rotation_euler = (0.0, math.radians(-90.0), math.radians(90.0))
    part.obj.location = (0.0, 0.0, 0.0)
    part.set_input('Length', height)
    part.set_input('Width', width)
    part.set_input('Thickness', thickness)
    part.set_input('Mirror Y', True)

    if part.obj.data.users > 1:
        part.obj.data = part.obj.data.copy()
    builder(part.obj.data, width, height, thickness, opts)
    part.obj.data.update()

    for mod in part.obj.modifiers:
        if mod.type == 'NODES':
            mod.show_viewport = False
            mod.show_render = False
    return part.obj


# ---------------------------------------------------------------------------
# Frame members (stiles / rails between split children)
# ---------------------------------------------------------------------------

def _add_frame_member(root, parent_name, x0, z0, orientation, span, thin,
                      role):
    """orientation 'HORIZONTAL' -> a rail: spans `span` along X, `thin`
    (its own height) along Z. 'VERTICAL' -> a stile: spans `span` along
    Z, `thin` (its own width) along X. Matches
    Face_Frame_Split._create_bay_mid_rail / _create_bay_mid_stile:
    horizontal = no rotation (Length+X, Width+Y, Thickness+Z);
    vertical = rotation.y=-90, Mirror Z=True (Length+Z, Width+Y,
    Thickness+X). Origin at the member's bottom-front-left corner."""
    thickness = _dim(root, 'Dim Y')
    part = hb_types.GeoNodeCutpart()
    part.create(role)
    part.obj.parent = root
    part.obj.matrix_parent_inverse.identity()
    part.obj[FRAME_MEMBER_TAG] = True
    part.obj['hb_part_role'] = role
    part.obj['wp_parent'] = parent_name
    part.obj['MENU_ID'] = ''
    part.obj.location = (x0, 0.0, z0)
    if orientation == 'HORIZONTAL':
        part.set_input('Length', span)
        part.set_input('Width', thickness)
        part.set_input('Thickness', thin)
    else:
        part.obj.rotation_euler.y = math.radians(-90)
        part.set_input('Mirror Z', True)
        part.set_input('Length', span)
        part.set_input('Width', thickness)
        part.set_input('Thickness', thin)
    return part.obj


# ---------------------------------------------------------------------------
# Openings
# ---------------------------------------------------------------------------

def _new_opening(root, parent_name, x0, z0, width, height):
    node = hb_types.GeoNodeCage()
    node.create("Wall Panel Opening")
    obj = node.obj
    obj[OPENING_TAG] = True
    obj['MENU_ID'] = OPENING_MENU_ID
    obj['wp_root'] = root.name
    obj['wp_parent'] = parent_name
    obj['wp_leaf'] = True
    obj.parent = root
    obj.matrix_parent_inverse.identity()
    obj.location = (x0, 0.0, z0)
    node.set_input('Dim X', width)
    node.set_input('Dim Y', _dim(root, 'Dim Y'))
    node.set_input('Dim Z', height)
    return obj


def split_opening(opening, orientation, count, splitter_width=None,
                  front_type=None):
    """Replace a leaf opening with `count` children (>=2) along X
    ('VERTICAL' -- stiles between them) or Z ('HORIZONTAL' -- rails
    between them). Children start out equal; drag/resize afterwards
    redistributes by whatever ratio they currently hold, so an uneven
    split survives a later panel resize the same way it would in a
    face-frame bay.

    Splitting an already-split opening is not supported (call it on a
    fresh leaf, or remove its children first); the opening stays
    around as a hidden bookkeeping node recording the split so resize()
    has somewhere to hang the orientation/splitter width.
    """
    if splitter_width is None:
        splitter_width = DEFAULT_SPLITTER_WIDTH
    if front_type is None:
        front_type = opening.get('wp_front_type', DEFAULT_FRONT_TYPE)

    root = root_of(opening)
    count = max(int(count), 2)

    _clear_front(opening)
    for child in list(_members(root, opening.name)):
        bpy.data.objects.remove(child, do_unlink=True)

    x0, z0 = opening.location.x, opening.location.z
    width = _dim(opening, 'Dim X')
    height = _dim(opening, 'Dim Z')
    n_split = count - 1

    opening['wp_leaf'] = False
    opening['wp_split_orientation'] = orientation
    opening['wp_split_count'] = count
    opening['wp_splitter_width'] = splitter_width
    opening.hide_viewport = True
    opening.hide_render = True
    opening.hide_select = True

    role = 'Wall Panel Stile' if orientation == 'VERTICAL' else 'Wall Panel Rail'
    children = []

    if orientation == 'VERTICAL':
        available = max(width - n_split * splitter_width, MIN_OPENING * count)
        each = available / count
        cursor = x0
        for i in range(count):
            child = _new_opening(root, opening.name, cursor, z0, each, height)
            children.append(child)
            assign_front(child, front_type)
            cursor += each
            if i < n_split:
                _add_frame_member(root, opening.name, cursor, z0,
                                  'VERTICAL', height, splitter_width, role)
                cursor += splitter_width
    else:
        available = max(height - n_split * splitter_width, MIN_OPENING * count)
        each = available / count
        cursor = z0
        for i in range(count):
            child = _new_opening(root, opening.name, x0, cursor, width, each)
            children.append(child)
            assign_front(child, front_type)
            cursor += each
            if i < n_split:
                _add_frame_member(root, opening.name, x0, cursor,
                                  'HORIZONTAL', width, splitter_width, role)
                cursor += splitter_width

    return children


# ---------------------------------------------------------------------------
# Resize -- opening cells share new space, splitter widths stay fixed
# ---------------------------------------------------------------------------

def resize(root, width, height, thickness=None):
    """Grow or shrink the whole panel. Frame member (stile/rail) widths
    never change -- a 2.5in stile stays 2.5in -- so the extra or
    missing space is shared among sibling openings in proportion to
    the sizes they already have. Recurses through nested splits; each
    leaf's front is rebuilt at its new size via assign_front, which
    also picks up a changed thickness since it reads Dim Y off the
    root fresh every time."""
    _set_dim(root, 'Dim X', width)
    _set_dim(root, 'Dim Z', height)
    if thickness is not None:
        _set_dim(root, 'Dim Y', thickness)
        for member in root.children:
            if is_frame_member(member):
                hb_types.GeoNodeCutpart(member).set_input('Width', thickness)
    top = top_opening_of(root)
    if top is not None:
        _resize_node(root, top, 0.0, 0.0, width, height)


def _resize_node(root, opening, x0, z0, width, height):
    old_w = _dim(opening, 'Dim X')
    old_h = _dim(opening, 'Dim Z')
    opening.location.x = x0
    opening.location.z = z0
    _set_dim(opening, 'Dim X', width)
    _set_dim(opening, 'Dim Z', height)

    if opening.get('wp_leaf', True):
        front_type = opening.get('wp_front_type')
        if front_type:
            assign_front(opening, front_type)
        return

    orientation = opening.get('wp_split_orientation')
    splitter_width = opening.get('wp_splitter_width', DEFAULT_SPLITTER_WIDTH)
    members = _members(root, opening.name)
    axis_key = 'x' if orientation == 'VERTICAL' else 'z'
    children = sorted((m for m in members if is_opening(m)),
                      key=lambda o: getattr(o.location, axis_key))
    frame_members = sorted((m for m in members if is_frame_member(m)),
                           key=lambda o: getattr(o.location, axis_key))
    n = len(children)
    if n == 0:
        return
    n_split = n - 1

    if orientation == 'VERTICAL':
        old_available = max(old_w - n_split * splitter_width, TOL)
        new_available = max(width - n_split * splitter_width, MIN_OPENING * n)
        weights = [_dim(c, 'Dim X') / old_available for c in children]
    else:
        old_available = max(old_h - n_split * splitter_width, TOL)
        new_available = max(height - n_split * splitter_width, MIN_OPENING * n)
        weights = [_dim(c, 'Dim Z') / old_available for c in children]

    total = sum(weights) or float(n)
    sizes = [new_available * w / total for w in weights]

    if orientation == 'VERTICAL':
        cursor = x0
        for i, child in enumerate(children):
            _resize_node(root, child, cursor, z0, sizes[i], height)
            cursor += sizes[i]
            if i < len(frame_members):
                fm = frame_members[i]
                fm.location.x, fm.location.z = cursor, z0
                hb_types.GeoNodeCutpart(fm).set_input('Length', height)
                cursor += splitter_width
    else:
        cursor = z0
        for i, child in enumerate(children):
            _resize_node(root, child, x0, cursor, width, sizes[i])
            cursor += sizes[i]
            if i < len(frame_members):
                fm = frame_members[i]
                fm.location.x, fm.location.z = x0, cursor
                hb_types.GeoNodeCutpart(fm).set_input('Length', width)
                cursor += splitter_width


# ---------------------------------------------------------------------------
# Create / remove
# ---------------------------------------------------------------------------

def create(context, location, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
          thickness=DEFAULT_THICKNESS, front_type=DEFAULT_FRONT_TYPE):
    """One freehand wall panel at `location` (typically the 3D cursor):
    a root cage plus its single starting opening, front already built
    so Add shows something real instead of an empty box."""
    root_node = hb_types.GeoNodeCage()
    root_node.create("Wall Panel")
    root = root_node.obj
    root[TAG] = True
    root['MENU_ID'] = MENU_ID
    root.location = location
    root_node.set_input('Dim X', width)
    root_node.set_input('Dim Y', thickness)
    root_node.set_input('Dim Z', height)

    opening = _new_opening(root, '', 0.0, 0.0, width, height)
    assign_front(opening, front_type)
    return root


def remove(root):
    objs = [root] + list(_collect_descendants(root))
    for obj in objs:
        bpy.data.objects.remove(obj, do_unlink=True)


def _collect_descendants(obj):
    for child in list(obj.children):
        yield child
        yield from _collect_descendants(child)
