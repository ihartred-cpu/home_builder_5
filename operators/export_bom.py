"""Bill of Materials exporter.

Walks the scene(s) built by Home Builder's product libraries (frameless,
face frame, closets) and writes three plain CSV files: a cut list of
panel parts, a hardware/component list, and a one-line-per-cabinet
assembly summary.

This intentionally reads the same markers the solvers already write
instead of re-deriving geometry:

- ``hb_part_role`` (``PART_ROLE_KEY`` in every product library's solver)
  is stamped on every physical cut part -- carcass sides, bottoms, backs,
  shelves, stretchers, etc. -- across frameless, face frame and closets.
  A part's actual size lives on its own GeoNodeCutpart modifier as the
  ``Length`` / ``Width`` / ``Thickness`` inputs (see e.g.
  ``product_libraries/frameless/solver_frameless.py:set_part``).
- A family of ``IS_*_CAGE`` / ``IS_GEONODE_CAGE`` flags mark containers
  (bays, openings, interiors, splitters, cabinet cages themselves) that
  also carry a part role in places (e.g. the frameless Bay) but are not
  physical parts and must be excluded from the cut list.
- A small set of flags mark placed hardware objects directly:
  ``IS_CABINET_PULL``, ``IS_LEG_LEVELER``, ``IS_DRAWER_BOX``.
- Cabinet/product roots are marked with one of a handful of
  ``IS_*_CABINET_CAGE`` / ``IS_*_PRODUCT_CAGE`` / ``IS_APPLIANCE`` /
  ``IS_CLOSET_STARTER_CAGE`` / ``IS_FRAMELESS_MISC_PART`` flags across the
  three libraries.

Extending coverage for a new hardware type or a new product library only
needs an addition to the constant sets below -- the walking/grouping/CSV
logic does not change.
"""

import bpy
import csv
import os
from bpy_extras.io_utils import ExportHelper
from .. import hb_utils
from .. import hb_types
from .. import units


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

PART_ROLE_KEY = 'hb_part_role'

# Containers that can carry a part role but are not themselves a physical
# part (a Bay, an Opening, an Interior, a Splitter, a cabinet cage, ...).
CAGE_FLAGS = {
    'IS_CAGE',
    'IS_CAGE_GROUP',
    'IS_GEONODE_CAGE',
    'IS_CLOSET_BAY_CAGE',
    'IS_CLOSET_OPENING_CAGE',
    'IS_CLOSET_STARTER_CAGE',
    'IS_FACE_FRAME_BAY_CAGE',
    'IS_FACE_FRAME_CABINET_CAGE',
    'IS_FACE_FRAME_OPENING_CAGE',
    'IS_FACE_FRAME_PRODUCT_CAGE',
    'IS_FRAMELESS_BAY_CAGE',
    'IS_FRAMELESS_CABINET_CAGE',
    'IS_FRAMELESS_DOORS_CAGE',
    'IS_FRAMELESS_INTERIOR_CAGE',
    'IS_FRAMELESS_LADDER_CAGE',
    'IS_FRAMELESS_OPENING_CAGE',
    'IS_FRAMELESS_PRODUCT_CAGE',
    'IS_FRAMELESS_SPLITTER_HORIZONTAL_CAGE',
    'IS_FRAMELESS_SPLITTER_VERTICAL_CAGE',
}

# obj[flag] -> hardware category label. Extend this as new hardware types
# get their own marker (hinges and shelf pins have none yet, so they are
# not counted -- see module docstring).
HARDWARE_FLAGS = {
    'IS_CABINET_PULL': 'Pull',
    'IS_LEG_LEVELER': 'Leg Leveler',
    'IS_DRAWER_BOX': 'Drawer Box',
}

# Root object flags for a placed cabinet/product/appliance, across every
# product library, mapped to a human label for the "Kind" column.
ASSEMBLY_ROOT_FLAGS = {
    'IS_FRAMELESS_CABINET_CAGE': 'Cabinet',
    'IS_FRAMELESS_PRODUCT_CAGE': 'Product',
    'IS_FRAMELESS_MISC_PART': 'Product',
    'IS_FACE_FRAME_CABINET_CAGE': 'Cabinet',
    'IS_FACE_FRAME_PRODUCT_CAGE': 'Product',
    'IS_CLOSET_STARTER_CAGE': 'Closet',
    'IS_APPLIANCE': 'Appliance',
}


def is_cage(obj):
    return any(obj.get(flag) for flag in CAGE_FLAGS)


def get_assembly_kind(obj):
    for flag, label in ASSEMBLY_ROOT_FLAGS.items():
        if obj.get(flag):
            return label
    return None


def find_assembly_root(obj):
    """Walk up the parent chain to the nearest cabinet/product/appliance
    root, across every product library."""
    current = obj
    while current is not None:
        if get_assembly_kind(current) is not None:
            return current
        current = current.parent
    return None


def get_part_dims(obj):
    """Return (length, width, thickness) in meters from a GeoNodeCutpart's
    modifier inputs, or (None, None, None) for anything without them."""
    part = hb_types.GeoNodeObject(obj)
    dims = []
    for name in ('Length', 'Width', 'Thickness'):
        try:
            dims.append(part.get_input(name))
        except (ValueError, KeyError):
            dims.append(None)
    return tuple(dims)


def get_cage_dims(obj):
    """Return (dim_x, dim_y, dim_z) in meters from a GeoNodeCage's Dim X/Y/Z
    modifier inputs, or (None, None, None) if unavailable."""
    cage = hb_types.GeoNodeObject(obj)
    dims = []
    for name in ('Dim X', 'Dim Y', 'Dim Z'):
        try:
            dims.append(cage.get_input(name))
        except (ValueError, KeyError):
            dims.append(None)
    return tuple(dims)


def get_material_name(obj):
    mat = getattr(obj, 'active_material', None)
    return mat.name if mat else ''


def get_material_thickness(obj):
    """Cabinets store carcass thickness as a plain ID property on the
    cabinet root (added via add_property('Material Thickness', ...)),
    not a modifier input."""
    root = find_assembly_root(obj)
    if root is not None and 'Material Thickness' in root:
        return root['Material Thickness']
    return None


def format_len(scene, value):
    if value is None:
        return ''
    return units.unit_to_string(scene.unit_settings, value)


def clean_name(name):
    """Strip Blender's '.001' duplicate suffix for display.

    Only safe for names that are re-grouped by something else that already
    disambiguates them (a part's role + dims within one assembly). Never
    use this on an assembly root's own name -- Blender hands out '.001',
    '.002', ... precisely because multiple cabinets are literally named
    "Cabinet", and stripping that suffix makes distinct cabinets collide
    under one identical label.
    """
    base, dot, suffix = name.rpartition('.')
    if dot and suffix.isdigit():
        return base
    return name


def get_bom_scenes(context):
    """Same room/main-scene selection prepare_for_export uses, so the BOM
    covers the whole project rather than just whichever scene is active."""
    scenes = [s for s in bpy.data.scenes if s.get('IS_ROOM_SCENE') or s.get('IS_MAIN_SCENE')]
    return scenes or [context.scene]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_bom(context):
    """Walk every relevant scene once and return (assemblies, panel_parts,
    hardware) -- three lists of plain dicts, one row per grouped item."""

    assemblies = {}   # obj name -> row dict (mutable, aggregated into)
    part_groups = {}  # group key -> row dict
    hw_groups = {}    # group key -> row dict

    seen_objects = set()

    for scene in get_bom_scenes(context):
        for obj in scene.objects:
            if obj.name in seen_objects:
                continue
            seen_objects.add(obj.name)

            kind = get_assembly_kind(obj)
            if kind is not None:
                assemblies[obj.name] = {
                    'Room': scene.name,
                    'Assembly': obj.name,
                    'Kind': kind,
                    'Width': '', 'Height': '', 'Depth': '',
                    'Material': '',
                    'Panel Part Qty': 0,
                    'Hardware Qty': 0,
                    '_obj': obj,
                    '_materials': {},
                }
                continue

            hw_flag = next((f for f in HARDWARE_FLAGS if obj.get(f)), None)
            if hw_flag is not None:
                root = find_assembly_root(obj)
                assembly_name = root.name if root else '(Unassigned)'
                room = scene.name
                category = HARDWARE_FLAGS[hw_flag]
                item_name = clean_name(obj.name)
                key = (room, assembly_name, category, item_name)
                row = hw_groups.setdefault(key, {
                    'Room': room, 'Assembly': assembly_name,
                    'Category': category, 'Item': item_name, 'Qty': 0,
                })
                row['Qty'] += 1
                if root is not None and root.name in assemblies:
                    assemblies[root.name]['Hardware Qty'] += 1
                continue

            # Non-mesh helper objects can carry a part role too -- e.g. the
            # frameless carcass's "Overlay Prompt Obj" is a plain EMPTY used
            # to anchor a UI prompt, not a physical part -- so exclude
            # anything that isn't real cut geometry regardless of role.
            if PART_ROLE_KEY in obj and not is_cage(obj) and obj.type != 'EMPTY':
                root = find_assembly_root(obj)
                assembly_name = root.name if root else '(Unassigned)'
                room = scene.name
                role = obj[PART_ROLE_KEY]
                part_name = clean_name(obj.name)
                length, width, thickness = get_part_dims(obj)
                material = get_material_name(obj)
                if not thickness:
                    thickness = get_material_thickness(obj)

                key = (room, assembly_name, role, length, width, thickness, material)
                row = part_groups.setdefault(key, {
                    'Room': room, 'Assembly': assembly_name,
                    'Part Name': part_name, 'Role': role,
                    'Qty': 0,
                    'Length': length, 'Width': width, 'Thickness': thickness,
                    'Material': material,
                    '_scene': scene,
                })
                row['Qty'] += 1
                if root is not None and root.name in assemblies:
                    assemblies[root.name]['Panel Part Qty'] += 1
                    if material:
                        materials = assemblies[root.name]['_materials']
                        materials[material] = materials.get(material, 0) + 1

    # Second pass over assemblies: fill in dims/material now that every
    # part has been seen.
    for row in assemblies.values():
        obj = row.pop('_obj')
        scene = None
        for s in get_bom_scenes(context):
            if obj.name in s.objects:
                scene = s
                break
        scene = scene or context.scene
        dim_x, dim_y, dim_z = get_cage_dims(obj)
        row['Width'] = format_len(scene, dim_x)
        row['Depth'] = format_len(scene, dim_y)
        row['Height'] = format_len(scene, dim_z)
        materials = row.pop('_materials')
        row['Material'] = max(materials, key=materials.get) if materials else ''

    # Format part dimensions last, once every group has its scene.
    panel_parts = []
    for row in part_groups.values():
        scene = row.pop('_scene')
        row['Length'] = format_len(scene, row['Length'])
        row['Width'] = format_len(scene, row['Width'])
        row['Thickness'] = format_len(scene, row['Thickness'])
        panel_parts.append(row)

    assembly_rows = list(assemblies.values())
    hardware_rows = list(hw_groups.values())

    # Stable, readable ordering.
    assembly_rows.sort(key=lambda r: (r['Room'], r['Assembly']))
    panel_parts.sort(key=lambda r: (r['Room'], r['Assembly'], r['Role'], r['Part Name']))
    hardware_rows.sort(key=lambda r: (r['Room'], r['Assembly'], r['Category'], r['Item']))

    return assembly_rows, panel_parts, hardware_rows


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

ASSEMBLY_FIELDS = ['Room', 'Assembly', 'Kind', 'Width', 'Height', 'Depth',
                    'Material', 'Panel Part Qty', 'Hardware Qty']
PART_FIELDS = ['Room', 'Assembly', 'Part Name', 'Role', 'Qty',
               'Length', 'Width', 'Thickness', 'Material']
HARDWARE_FIELDS = ['Room', 'Assembly', 'Category', 'Item', 'Qty']


def write_csv(path, fieldnames, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class HOME_BUILDER_OT_export_bom(bpy.types.Operator, ExportHelper):
    bl_idname = "home_builder.export_bom"
    bl_label = "Export BOM (CSV)"
    bl_description = (
        "Write a panel-parts cut list, a hardware list and a cabinet "
        "assembly summary as three CSV files"
    )
    bl_options = {'REGISTER'}

    filename_ext = ".csv"
    filter_glob: bpy.props.StringProperty(default="*.csv", options={'HIDDEN'})  # type: ignore

    filepath: bpy.props.StringProperty(
        name="File Path",
        default="Home_Builder_BOM.csv",
        subtype='FILE_PATH',
    )  # type: ignore

    def execute(self, context):
        assemblies, panel_parts, hardware = collect_bom(context)

        if not assemblies and not panel_parts and not hardware:
            self.report({'WARNING'}, "No Home Builder cabinets/products found to export.")
            return {'CANCELLED'}

        stem = os.path.splitext(self.filepath)[0]
        parts_path = stem + '_panel_parts.csv'
        hardware_path = stem + '_hardware.csv'
        assembly_path = stem + '_assembly_summary.csv'

        write_csv(assembly_path, ASSEMBLY_FIELDS, assemblies)
        write_csv(parts_path, PART_FIELDS, panel_parts)
        write_csv(hardware_path, HARDWARE_FIELDS, hardware)

        self.report({'INFO'},
            f"BOM exported: {len(assemblies)} assemblies, {len(panel_parts)} "
            f"panel part lines, {len(hardware)} hardware lines.")
        return {'FINISHED'}


classes = (
    HOME_BUILDER_OT_export_bom,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
