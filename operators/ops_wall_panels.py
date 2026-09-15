"""
Operators for freehand wall panels (see ../wall_panels.py for the data
model and geometry). Five commands and a right-click menu:

  home_builder.add_wall_panel             drop one at the 3D cursor
  home_builder.split_wall_panel_opening   divide the active opening
  home_builder.assign_wall_panel_front    change a leaf's front style
  home_builder.resize_wall_panel          change the whole panel's size
  home_builder.remove_wall_panel          delete it

Add and Split both use a props dialog rather than a plain redo panel so
the front-type / orientation choice is visible before anything is
built; every property is still adjustable afterwards from the F6 redo
panel like any other REGISTER+UNDO operator.
"""

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty

from .. import wall_panels as wp


def _wp_front_items(self, context):
    items = wp.front_type_items()
    return items if items else [('SLAB', "Slab", "Flat panel, no frame", 0)]


SPLIT_ORIENTATION_ITEMS = [
    ('VERTICAL', "Vertical (Stiles)", "Split side by side with a stile between", 0),
    ('HORIZONTAL', "Horizontal (Rails)", "Stack top to bottom with a rail between", 1),
]


def _active_opening(context):
    obj = context.active_object
    return obj if wp.is_opening(obj) else None


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------

class HOME_BUILDER_OT_add_wall_panel(bpy.types.Operator):
    bl_idname = "home_builder.add_wall_panel"
    bl_label = "Add Wall Panel"
    bl_description = ("Add a freehand wall panel at the 3D cursor -- not "
                      "tied to a wall or cabinet run")
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(name="Width", default=wp.DEFAULT_WIDTH,
                         min=wp.MIN_OPENING, max=10.0, unit='LENGTH',
                         precision=4)
    height: FloatProperty(name="Height", default=wp.DEFAULT_HEIGHT,
                          min=wp.MIN_OPENING, max=4.0, unit='LENGTH',
                          precision=4)
    thickness: FloatProperty(name="Thickness", default=wp.DEFAULT_THICKNESS,
                             min=0.005, max=0.15, unit='LENGTH', precision=4)
    front_type: EnumProperty(name="Front", items=_wp_front_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        col = self.layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, "width")
        col.prop(self, "height")
        col.prop(self, "thickness")
        col.prop(self, "front_type")

    def execute(self, context):
        location = context.scene.cursor.location.copy()
        root = wp.create(context, location, self.width, self.height,
                         self.thickness, self.front_type)
        bpy.ops.object.select_all(action='DESELECT')
        root.select_set(True)
        context.view_layer.objects.active = root
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

class HOME_BUILDER_OT_split_wall_panel_opening(bpy.types.Operator):
    bl_idname = "home_builder.split_wall_panel_opening"
    bl_label = "Split Wall Panel Opening"
    bl_description = ("Divide the selected wall panel opening into a row "
                      "or column of openings with a stile/rail between "
                      "them")
    bl_options = {'REGISTER', 'UNDO'}

    orientation: EnumProperty(name="Split", items=SPLIT_ORIENTATION_ITEMS)
    count: IntProperty(name="Count", default=2, min=2, max=12)
    splitter_width: FloatProperty(name="Stile/Rail Width",
                                  default=wp.DEFAULT_SPLITTER_WIDTH,
                                  min=0.006, max=0.3, unit='LENGTH',
                                  precision=4)
    front_type: EnumProperty(name="Front", items=_wp_front_items)

    @classmethod
    def poll(cls, context):
        opening = _active_opening(context)
        return bool(opening is not None and opening.get('wp_leaf', True))

    def invoke(self, context, event):
        opening = _active_opening(context)
        current = opening.get('wp_front_type') if opening else None
        if current:
            self.front_type = current
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        col = self.layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, "orientation")
        col.prop(self, "count")
        col.prop(self, "splitter_width")
        col.prop(self, "front_type")

    def execute(self, context):
        opening = _active_opening(context)
        if opening is None:
            self.report({'WARNING'}, "Select a wall panel opening first")
            return {'CANCELLED'}
        children = wp.split_opening(opening, self.orientation, self.count,
                                    self.splitter_width, self.front_type)
        if children:
            bpy.ops.object.select_all(action='DESELECT')
            children[0].select_set(True)
            context.view_layer.objects.active = children[0]
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Assign front
# ---------------------------------------------------------------------------

class HOME_BUILDER_OT_assign_wall_panel_front(bpy.types.Operator):
    bl_idname = "home_builder.assign_wall_panel_front"
    bl_label = "Assign Wall Panel Front"
    bl_description = "Change the front style built into this opening"
    bl_options = {'REGISTER', 'UNDO'}

    front_type: EnumProperty(name="Front", items=_wp_front_items)

    @classmethod
    def poll(cls, context):
        opening = _active_opening(context)
        return bool(opening is not None and opening.get('wp_leaf', True))

    def invoke(self, context, event):
        opening = _active_opening(context)
        current = opening.get('wp_front_type') if opening else None
        if current:
            self.front_type = current
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        self.layout.prop(self, "front_type")

    def execute(self, context):
        opening = _active_opening(context)
        if opening is None:
            self.report({'WARNING'}, "Select a wall panel opening first")
            return {'CANCELLED'}
        wp.assign_front(opening, self.front_type)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Resize
# ---------------------------------------------------------------------------

class HOME_BUILDER_OT_resize_wall_panel(bpy.types.Operator):
    bl_idname = "home_builder.resize_wall_panel"
    bl_label = "Resize Wall Panel"
    bl_description = ("Change the panel's overall size. Stile/rail widths "
                      "stay fixed -- openings share the new space")
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(name="Width", min=wp.MIN_OPENING, max=10.0,
                         unit='LENGTH', precision=4)
    height: FloatProperty(name="Height", min=wp.MIN_OPENING, max=4.0,
                          unit='LENGTH', precision=4)
    thickness: FloatProperty(name="Thickness", min=0.005, max=0.15,
                             unit='LENGTH', precision=4)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return wp.root_of(obj) is not None if obj else False

    def invoke(self, context, event):
        root = wp.root_of(context.active_object)
        self.width, self.height, self.thickness = wp.get_size(root)
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        col = self.layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(self, "width")
        col.prop(self, "height")
        col.prop(self, "thickness")

    def execute(self, context):
        root = wp.root_of(context.active_object)
        if root is None:
            self.report({'WARNING'}, "Select a wall panel first")
            return {'CANCELLED'}
        wp.resize(root, self.width, self.height, self.thickness)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

class HOME_BUILDER_OT_remove_wall_panel(bpy.types.Operator):
    bl_idname = "home_builder.remove_wall_panel"
    bl_label = "Delete Wall Panel"
    bl_description = "Delete this wall panel and everything in it"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return wp.root_of(obj) is not None if obj else False

    def execute(self, context):
        root = wp.root_of(context.active_object)
        if root is None:
            self.report({'WARNING'}, "Select a wall panel first")
            return {'CANCELLED'}
        wp.remove(root)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Right-click menu (picked up automatically via the object's MENU_ID,
# see ui/menu_apend.py)
# ---------------------------------------------------------------------------

class HOME_BUILDER_MT_wall_panel_commands(bpy.types.Menu):
    bl_label = "Wall Panel Commands"

    def draw(self, context):
        layout = self.layout
        opening = _active_opening(context)
        if opening is not None and opening.get('wp_leaf', True):
            layout.operator("home_builder.split_wall_panel_opening",
                            icon='MOD_EDGESPLIT')
            layout.operator("home_builder.assign_wall_panel_front",
                            icon='MATERIAL')
            layout.separator()
        layout.operator("home_builder.resize_wall_panel",
                        icon='DRIVER_DISTANCE')
        layout.separator()
        layout.operator("home_builder.remove_wall_panel",
                        text="Delete Wall Panel", icon='X')


classes = [
    HOME_BUILDER_OT_add_wall_panel,
    HOME_BUILDER_OT_split_wall_panel_opening,
    HOME_BUILDER_OT_assign_wall_panel_front,
    HOME_BUILDER_OT_resize_wall_panel,
    HOME_BUILDER_OT_remove_wall_panel,
    HOME_BUILDER_MT_wall_panel_commands,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
