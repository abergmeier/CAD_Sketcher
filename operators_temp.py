"""
Operators
"""
import logging
import math
from collections import deque, namedtuple
from typing import Deque, Generator

import bpy
from bl_operators.presets import AddPresetBase
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Context, Event, Operator, Scene
from mathutils import Vector
from mathutils.geometry import intersect_line_plane


from . import class_defines, functions, global_data

from .declarations import Operators, VisibilityTypes
from .class_defines import (
    SlvsConstraints,
    SlvsGenericEntity,
    SlvsSketch,
)
from .solver import solve_system
from .functions import show_ui_message_popup
from .operators.utilities import activate_sketch
from .utilities.highlighting import HighlightElement
from .stateful_operator.integration import StatefulOperator
from .stateful_operator.state import state_from_args
from .operators.base_stateful import GenericEntityOp
from .operators.utilities import deselect_all, ignore_hover

logger = logging.getLogger(__name__)


def add_point(context, pos, name=""):
    data = bpy.data
    ob = data.objects.new(name, None)
    ob.location = pos
    context.collection.objects.link(ob)
    return ob

from .operators.base_2d import Operator2d
from .operators.constants import types_point_3d, types_point_2d





class Intersection:
    """Either a intersection between the segment to be trimmed and specified entity or a segment endpoint"""

    def __init__(self, element, co):
        # Either a intersecting entity, a segment endpoint or a coincident/midpoint constraint
        self.element = element
        self.co = co
        self.index = -1
        self._is_endpoint = False
        self._point = None

    def is_entity(self):
        return issubclass(type(self.element), class_defines.SlvsGenericEntity)

    def is_constraint(self):
        return issubclass(type(self.element), class_defines.GenericConstraint)

    def is_endpoint(self):
        return self._is_endpoint

    def get_point(self, context: Context):
        if self.is_entity() and self.element.is_point():
            return self.element
        if self.is_constraint():
            return self.element.entities()[0]
        if self._point == None:
            sketch = context.scene.sketcher.active_sketch
            # Implicitly create point at co
            self._point = context.scene.sketcher.entities.add_point_2d(self.co, sketch)

            # Add coincident constraint
            if self.is_entity():  # and self.element.is_segment()
                c = context.scene.sketcher.constraints.add_coincident(
                    self._point, self.element, sketch=sketch
                )

        return self._point

    def __str__(self):
        return "Intersection {}, {}, {}".format(self.index, self.co, self.element)


class TrimSegment:
    """Holds data of a segment to be trimmed"""

    def __init__(self, segment, pos):
        self.segment = segment
        self.pos = pos
        self._intersections = []
        self._is_closed = segment.is_closed()
        self.connection_points = segment.connection_points().copy()
        self.obsolete_intersections = []
        self.reuse_segment = False

        # Add connection points as intersections
        if not self._is_closed:
            for p in self.connection_points:
                intr = self.add(p, p.co)
                intr._is_endpoint = True

    def add(self, element, co):
        intr = Intersection(element, co)
        self._intersections.append(intr)
        return intr

    def check(self):
        relevant = self.relevant_intersections()
        return len(relevant) in (2, 4)

    def _sorted(self):
        # Return intersections sorted by distance from mousepos
        return sorted(
            self._intersections,
            key=lambda intr: self.segment.distance_along_segment(self.pos, intr.co),
        )

    def get_intersections(self):
        # Return intersections in order starting from startpoint
        sorted_intersections = self._sorted()
        for i, intr in enumerate(sorted_intersections):
            intr.index = i
        return sorted_intersections

    def relevant_intersections(self):
        # Get indices of two neighbouring points
        ordered = self.get_intersections()
        closest = ordered[0].index, ordered[-1].index

        # Form a list of relevant intersections, e.g. endpoints and closest points
        relevant = []
        for intr in ordered:
            if intr.is_endpoint():
                # Add endpoints
                if intr.index in closest:
                    # Not if next to trim segment
                    if intr not in self.obsolete_intersections:
                        self.obsolete_intersections.append(intr)
                    continue
                relevant.append(intr)

            if intr.index in closest:
                if intr.is_constraint():
                    if intr not in self.obsolete_intersections:
                        self.obsolete_intersections.append(intr)
                relevant.append(intr)

        def _get_log_msg():
            msg = "Trimming:"
            for intr in ordered:
                is_relevant = intr in relevant
                msg += "\n - " + ("RELEVANT " if is_relevant else "IGNORE ") + str(intr)
            return msg

        logger.debug(_get_log_msg())
        return relevant

    def ensure_points(self, context):
        for intr in self.relevant_intersections():
            intr.get_point(context)

    def replace(self, context):
        relevant = self.relevant_intersections()

        # Get constraints
        constrs = {}
        for c in context.scene.sketcher.constraints.all:
            entities = c.entities()
            if not self.segment in entities:
                continue
            constrs[c] = entities

        # Note: this seems to be needed, explicitly add all points and update viewlayer before starting to replace segments
        self.ensure_points(context)

        # NOTE: This is needed for some reason, otherwise there's a bug where
        # a point is suddenly interpreted as a line
        context.view_layer.update()

        # Create new segments
        segment_count = len(relevant) // 2
        for index, intrs in enumerate(
            [relevant[i * 2 : i * 2 + 2] for i in range(segment_count)]
        ):
            reuse_segment = index == 0 and not isinstance(
                self.segment, class_defines.SlvsCircle
            )
            intr_1, intr_2 = intrs
            if not intr_1:
                continue

            new_segment = self.segment.replace(
                context,
                intr_1.get_point(context),
                intr_2.get_point(context),
                use_self=reuse_segment,
            )

            if reuse_segment:
                self.reuse_segment = True
                continue

            # Copy constraints to new segment
            for c, ents in constrs.items():
                i = ents.index(self.segment)
                if index != 0:
                    if c.type in ("RATIO", "COINCIDENT", "MIDPOINT", "TANGENT"):
                        continue
                    ents[i] = new_segment
                    new_constr = c.copy(context, ents)
                else:
                    # if the original segment doesn't get reused the original constraints
                    # have to be remapped to the new segment
                    setattr(c, "entity{}_i".format(i + 1), new_segment.slvs_index)

        def _get_msg_obsolete():
            msg = "Remove obsolete intersections:"
            for intr in self.obsolete_intersections:
                msg += "\n - {}".format(intr)
            return msg

        logger.debug(_get_msg_obsolete())

        # Remove unused endpoints
        delete_constraints = []
        for intr in self.obsolete_intersections:
            if intr.is_constraint():
                c = intr.element
                i = context.scene.sketcher.constraints.get_index(c)
                # TODO: Make this a class reference
                bpy.ops.view3d.slvs_delete_constraint(type=c.type, index=i)
            if intr.is_entity():
                # Use operator which checks if other entities depend on this and auto deletes constraints
                # TODO: Make this a class reference
                bpy.ops.view3d.slvs_delete_entity(index=intr.element.slvs_index)

        # Remove original segment if not used
        if not self.reuse_segment:
            context.scene.sketcher.entities.remove(self.segment.slvs_index)


class View3D_OT_slvs_trim(Operator, Operator2d):
    """Trim segment to it's closest intersections"""

    bl_idname = Operators.Trim
    bl_label = "Trim Segment"
    bl_options = {"REGISTER", "UNDO"}

    trim_state1_doc = ("Segment", "Segment to trim.")

    radius: FloatProperty(
        name="Radius", precision=5,
    )

    states = (
        state_from_args(
            trim_state1_doc[0],
            description=trim_state1_doc[1],
            pointer="segment",
            types=class_defines.segment,
            pick_element="pick_element_coords",
            use_create=False,
            # interactive=True
        ),
    )

    # TODO: Disable execution based on selection
    # NOTE: That does not work if run with select -> action
    def pick_element_coords(self, context, coords):
        data = self.state_data
        data["mouse_pos"] = self.state_func(context, coords)
        return super().pick_element(context, coords)

    def main(self, context: Context):
        return True

    def fini(self, context: Context, succeede):
        if not succeede:
            return False

        sketch = context.scene.sketcher.active_sketch
        segment = self.segment

        mouse_pos = self._state_data[0].get("mouse_pos")
        if mouse_pos == None:
            return False

        trim = TrimSegment(segment, mouse_pos)

        # Find intersections
        for e in sketch.sketch_entities(context):
            if not type(e) in class_defines.segment:
                continue
            if e == segment:
                continue

            for co in segment.intersect(e):
                # print("intersect", co)
                trim.add(e, co)

        # Find points that are connected to the segment through a conincident constraint
        for c in (
            *context.scene.sketcher.constraints.coincident,
            *context.scene.sketcher.constraints.midpoint,
        ):
            ents = c.entities()
            if segment not in ents:
                continue
            p = ents[0]
            trim.add(c, p.co)

        # TODO: Get rid of the coincident constraint as it will be a shared connection point

        if not trim.check():
            return

        trim.replace(context)
        functions.refresh(context)


class View3D_OT_slvs_test(Operator, GenericEntityOp):
    bl_idname = Operators.Test
    bl_label = "Test StateOps"
    bl_options = {"REGISTER", "UNDO"}

    states = (
        state_from_args("ob", pointer="object", types=(bpy.types.Object,),),
        state_from_args(
            "Pick Element",
            description="Pick an element to print",
            pointer="element",
            types=(
                *class_defines.point,
                *class_defines.line,
                *class_defines.curve,
                bpy.types.MeshVertex,
                bpy.types.MeshEdge,
                bpy.types.MeshPolygon,
            ),
        ),
    )

    def main(self, context: Context):
        element = self.element
        if element:
            self.report({"INFO"}, "Picked element " + str(element))
            return True
        return False


class View3D_OT_slvs_set_active_sketch(Operator):
    """Set the active sketch"""

    bl_idname = Operators.SetActiveSketch
    bl_label = "Set active Sketch"
    bl_options = {"UNDO"}

    index: IntProperty(default=-1)

    def execute(self, context: Context):
        return activate_sketch(context, self.index, self)


def get_flat_deps(entity):
    """Return flattened list of entities given entity depends on"""
    list = []

    def walker(entity, is_root=False):
        if entity in list:
            return
        if not is_root:
            list.append(entity)
        if not hasattr(entity, "dependencies"):
            return
        for e in entity.dependencies():
            if e in list:
                continue
            walker(e)

    walker(entity, is_root=True)
    return list


def get_scene_constraints(scene: Scene):
    return scene.sketcher.constraints.all


def get_scene_entities(scene: Scene):
    return scene.sketcher.entities.all


def get_entity_deps(
    entity: SlvsGenericEntity, context: Context
) -> Generator[SlvsGenericEntity, None, None]:
    for scene_entity in get_scene_entities(context.scene):
        deps = set(get_flat_deps(scene_entity))
        if entity in deps:
            yield scene_entity


def is_entity_referenced(entity: SlvsGenericEntity, context: Context) -> bool:
    """Check if entity is a dependency of another entity"""
    deps = get_entity_deps(entity, context)
    try:
        next(deps)
    except StopIteration:
        return False
    return True


def get_sketch_deps_indicies(sketch: SlvsSketch, context: Context):
    deps = deque()
    for entity in get_scene_entities(context.scene):
        if not hasattr(entity, "sketch_i"):
            continue
        if sketch.slvs_index != entity.sketch.slvs_index:
            continue
        deps.append(entity.slvs_index)
    return deps


def get_constraint_local_indices(
    entity: SlvsGenericEntity, context: Context
) -> Deque[int]:
    constraints = context.scene.sketcher.constraints
    ret_list = deque()

    for data_coll in constraints.get_lists():
        indices = deque()
        for c in data_coll:
            if entity in c.dependencies():
                indices.append(constraints.get_index(c))
        ret_list.append((data_coll, indices))
    return ret_list


class View3D_OT_slvs_delete_entity(Operator, HighlightElement):
    """Delete Entity by index or based on the selection if index isn't provided"""

    bl_idname = Operators.DeleteEntity
    bl_label = "Delete Solvespace Entity"
    bl_options = {"UNDO"}
    bl_description = (
        "Delete Entity by index or based on the selection if index isn't provided"
    )

    index: IntProperty(default=-1)

    @staticmethod
    def main(context: Context, index: int, operator: Operator):
        entities = context.scene.sketcher.entities
        entity = entities.get(index)

        if not entity:
            return {"CANCELLED"}

        if isinstance(entity, class_defines.SlvsSketch):
            if context.scene.sketcher.active_sketch_i != -1:
                activate_sketch(context, -1, operator)
            entity.remove_objects()

            deps = get_sketch_deps_indicies(entity, context)

            for i in reversed(deps):
                operator.delete(entities.get(i), context)

        elif is_entity_referenced(entity, context):
            deps = list(get_entity_deps(entity, context))

            message = f"Unable to delete {entity.name}, other entities depend on it:\n"+ "\n".join(
                [f" - {d}" for d in deps]
            )
            show_ui_message_popup(message=message, icon="ERROR")

            operator.report(
                {"WARNING"},
                "Cannot delete {}, other entities depend on it.".format(
                    entity.name
                ),
            )
            return {"CANCELLED"}

        operator.delete(entity, context)

    @staticmethod
    def delete(entity, context: Context):
        entity.selected = False

        # Delete constraints that depend on entity
        constraints = context.scene.sketcher.constraints

        for data_coll, indices in reversed(get_constraint_local_indices(entity, context)):
            if not indices:
                continue
            for i in indices:
                logger.debug("Delete: {}".format(data_coll[i]))
                data_coll.remove(i)

        logger.debug("Delete: {}".format(entity))
        entities = context.scene.sketcher.entities
        entities.remove(entity.slvs_index)

    def execute(self, context: Context):
        index = self.index
        selected = context.scene.sketcher.entities.selected_entities

        if index != -1:
            # Entity is specified via property
            self.main(context, index, self)
        elif len(selected) == 1:
            # Treat single selection same as specified entity
            self.main(context, selected[0].slvs_index, self)
        else:
            # Batch deletion
            indices = []
            for e in selected:
                indices.append(e.slvs_index)

            indices.sort(reverse=True)
            for i in indices:
                e = context.scene.sketcher.entities.get(i)

                # NOTE: this might be slow when a lot of entities are selected, improve!
                if is_entity_referenced(e, context):
                    continue
                self.delete(e, context)

        functions.refresh(context)
        return {"FINISHED"}


state_docstr = "Pick entity to constrain."


class GenericConstraintOp(GenericEntityOp):
    initialized: BoolProperty(options={"SKIP_SAVE", "HIDDEN"})
    _entity_prop_names = ("entity1", "entity2", "entity3", "entity4")

    def _available_entities(self):
        # Gets entities that are already set
        cls = SlvsConstraints.cls_from_type(self.type)
        entities = [None] * len(cls.signature)
        for i, name in enumerate(self._entity_prop_names):
            if hasattr(self, name):
                e = getattr(self, name)
                if not e:
                    continue
                entities[i] = e
        return entities

    @classmethod
    def states(cls, operator=None):
        states = []

        cls_constraint = SlvsConstraints.cls_from_type(cls.type)

        for i, _ in enumerate(cls_constraint.signature):
            name_index = i + 1
            if hasattr(cls_constraint, "get_types") and operator:
                types = cls_constraint.get_types(i, operator._available_entities())
            else:
                types = cls_constraint.signature[i]

            if not types:
                break

            states.append(
                state_from_args(
                    "Entity " + str(name_index),
                    description=state_docstr,
                    pointer="entity" + str(name_index),
                    property=None,
                    types=types,
                )
            )
        return states

    def initialize_constraint(self):
        c = self.target
        if not self.initialized and hasattr(c, "init_props"):
            kwargs = {}
            if hasattr(self, "value") and self.properties.is_property_set("value"):
                kwargs["value"] = self.value
            if hasattr(self, "setting") and self.properties.is_property_set("setting"):
                kwargs["setting"] = self.setting

            value, setting = c.init_props(**kwargs)
            if value is not None:
                self.value = value
            if setting is not None:
                self.setting = setting
        self.initialized = True

    def fill_entities(self):
        c = self.target
        args = []
        # fill in entities!
        for prop in self._entity_prop_names:
            if hasattr(c, prop):
                value = getattr(self, prop)
                setattr(c, prop, value)
                args.append(value)
        return args

    def main(self, context):
        c = self.target = context.scene.sketcher.constraints.new_from_type(self.type)
        self.sketch = context.scene.sketcher.active_sketch
        entities = self.fill_entities()
        c.sketch = self.sketch

        self.initialize_constraint()

        if hasattr(c, "value"):
            c["value"] = self.value
        if hasattr(c, "setting"):
            c["setting"] = self.setting

        deselect_all(context)
        solve_system(context, sketch=self.sketch)
        functions.refresh(context)
        return True

    def fini(self, context, succeede):
        if hasattr(self, "target"):
            logger.debug("Add: {}".format(self.target))

    def draw(self, context):
        layout = self.layout

        c = self.target
        if not c:
            return

        if hasattr(c, "value"):
            layout.prop(self, "value")
        if hasattr(c, "setting"):
            layout.prop(self, "setting")

        if hasattr(self, "draw_settings"):
            self.draw_settings(context)


# Dimensional constraints
class VIEW3D_OT_slvs_add_distance(Operator, GenericConstraintOp):
    """Add a distance constraint"""

    bl_idname = Operators.AddDistance
    bl_label = "Distance"
    bl_options = {"UNDO", "REGISTER"}

    value: FloatProperty(
        name="Distance",
        subtype="DISTANCE",
        unit="LENGTH",
        min=0.0,
        precision=5,
        options={"SKIP_SAVE"},
    )
    align: EnumProperty(name="Alignment", items=class_defines.align_items)
    type = "DISTANCE"

    def fini(self, context, succeede):
        super().fini(context, succeede)
        if hasattr(self, "target"):
            self.target.align = self.align
            self.target.draw_offset = 0.05 * context.region_data.view_distance

    def draw_settings(self, context):
        if not hasattr(self, "target"):
            return

        layout = self.layout

        row = layout.row()
        row.active = self.target.use_align()
        row.prop(self, "align")


def invert_angle_getter(self):
    return self.get("setting", self.bl_rna.properties["setting"].default)


def invert_angle_setter(self, setting):
    self["value"] = math.pi - self.value
    self["setting"] = setting


class VIEW3D_OT_slvs_add_angle(Operator, GenericConstraintOp):
    """Add an angle constraint"""

    bl_idname = Operators.AddAngle
    bl_label = "Angle"
    bl_options = {"UNDO", "REGISTER"}

    value: FloatProperty(
        name="Angle",
        subtype="ANGLE",
        unit="ROTATION",
        options={"SKIP_SAVE"},
        precision=5,
    )
    setting: BoolProperty(name="Measure supplementary angle", default = False, get=invert_angle_getter, set=invert_angle_setter)
    type = "ANGLE"

    def fini(self, context, succeede):
        super().fini(context, succeede)
        if hasattr(self, "target"):
            self.target.draw_offset = 0.1 * context.region_data.view_distance


class VIEW3D_OT_slvs_add_diameter(Operator, GenericConstraintOp):
    """Add a diameter constraint"""

    bl_idname = Operators.AddDiameter
    bl_label = "Diameter"
    bl_options = {"UNDO", "REGISTER"}

    # Either Radius or Diameter
    value: FloatProperty(
        name="Size",
        subtype="DISTANCE",
        unit="LENGTH",
        options={"SKIP_SAVE"},
        precision=5,
    )

    setting: BoolProperty(name="Use Radius")
    type = "DIAMETER"


# Geomteric constraints
class VIEW3D_OT_slvs_add_coincident(Operator, GenericConstraintOp):
    """Add a coincident constraint"""

    bl_idname = Operators.AddCoincident
    bl_label = "Coincident"
    bl_options = {"UNDO", "REGISTER"}

    type = "COINCIDENT"

    def main(self, context: Context):
        p1, p2 = self.entity1, self.entity2
        if all([e.is_point() for e in (p1, p2)]):
            # Implicitly merge points
            class_defines.update_pointers(context.scene, p1.slvs_index, p2.slvs_index)
            context.scene.sketcher.entities.remove(p1.slvs_index)
            solve_system(context, context.scene.sketcher.active_sketch)
            return True
        return super().main(context)


class VIEW3D_OT_slvs_add_equal(Operator, GenericConstraintOp):
    """Add an equal constraint"""

    bl_idname = Operators.AddEqual
    bl_label = "Equal"
    bl_options = {"UNDO", "REGISTER"}

    type = "EQUAL"


class VIEW3D_OT_slvs_add_vertical(Operator, GenericConstraintOp):
    """Add a vertical constraint"""

    bl_idname = Operators.AddVertical
    bl_label = "Vertical"
    bl_options = {"UNDO", "REGISTER"}

    type = "VERTICAL"


class VIEW3D_OT_slvs_add_horizontal(Operator, GenericConstraintOp):
    """Add a horizontal constraint"""

    bl_idname = Operators.AddHorizontal
    bl_label = "Horizontal"
    bl_options = {"UNDO", "REGISTER"}

    type = "HORIZONTAL"


class VIEW3D_OT_slvs_add_parallel(Operator, GenericConstraintOp):
    """Add a parallel constraint"""

    bl_idname = Operators.AddParallel
    bl_label = "Parallel"
    bl_options = {"UNDO", "REGISTER"}

    type = "PARALLEL"


class VIEW3D_OT_slvs_add_perpendicular(Operator, GenericConstraintOp):
    """Add a perpendicular constraint"""

    bl_idname = Operators.AddPerpendicular
    bl_label = "Perpendicular"
    bl_options = {"UNDO", "REGISTER"}

    type = "PERPENDICULAR"


class VIEW3D_OT_slvs_add_tangent(Operator, GenericConstraintOp, GenericEntityOp):
    """Add a tagent constraint"""

    bl_idname = Operators.AddTangent
    bl_label = "Tangent"
    bl_options = {"UNDO", "REGISTER"}

    type = "TANGENT"


class VIEW3D_OT_slvs_add_midpoint(Operator, GenericConstraintOp, GenericEntityOp):
    """Add a midpoint constraint"""

    bl_idname = Operators.AddMidPoint
    bl_label = "Midpoint"
    bl_options = {"UNDO", "REGISTER"}

    type = "MIDPOINT"


class VIEW3D_OT_slvs_add_ratio(Operator, GenericConstraintOp, GenericEntityOp):
    """Add a ratio constraint"""

    value: FloatProperty(
        name="Ratio", subtype="UNSIGNED", options={"SKIP_SAVE"}, min=0.0, precision=5,
    )
    bl_idname = Operators.AddRatio
    bl_label = "Ratio"
    bl_options = {"UNDO", "REGISTER"}

    type = "RATIO"


class View3D_OT_slvs_set_all_constraints_visibility(Operator, HighlightElement):
    """Set all constraints' visibility
    """
    _visibility_items = [
        (VisibilityTypes.Hide, "Hide all", "Hide all constraints"),
        (VisibilityTypes.Show, "Show all", "Show all constraints"),
    ]

    bl_idname = Operators.SetAllConstraintsVisibility
    bl_label = "Set all constraints' visibility"
    bl_options = {"UNDO"}
    bl_description = "Set all constraints' visibility"

    visibility: EnumProperty(
        name="Visibility",
        description="Visiblity",
        items=_visibility_items)

    @classmethod
    def poll(cls, context):
        return True

    @classmethod
    def description(cls, context, properties):
        for vi in cls._visibility_items:
            if vi[0] == properties.visibility:
                return vi[2]
        return None

    def execute(self, context):
        constraint_lists = context.scene.sketcher.constraints.get_lists()
        for constraint_list in constraint_lists:
            for constraint in constraint_list:
                if not hasattr(constraint, "visible"):
                    continue
                constraint.visible = self.visibility == "SHOW"
        return {"FINISHED"}


class View3D_OT_slvs_delete_constraint(Operator, HighlightElement):
    """Delete constraint by type and index
    """

    bl_idname = Operators.DeleteConstraint
    bl_label = "Delete Constraint"
    bl_options = {"UNDO"}
    bl_description = "Delete Constraint"

    type: StringProperty(name="Type")
    index: IntProperty(default=-1)

    @classmethod
    def description(cls, context, properties):
        cls.handle_highlight_hover(context, properties)
        if properties.type:
            return "Delete: " + properties.type.capitalize()
        return ""

    def execute(self, context: Context):
        constraints = context.scene.sketcher.constraints

        # NOTE: It's not really necessary to first get the
        # constraint from it's index before deleting

        constr = constraints.get_from_type_index(self.type, self.index)
        logger.debug("Delete: {}".format(constr))

        constraints.remove(constr)

        sketch = context.scene.sketcher.active_sketch
        solve_system(context, sketch=sketch)
        functions.refresh(context)
        return {"FINISHED"}


class View3D_OT_slvs_tweak_constraint_value_pos(Operator):
    bl_idname = Operators.TweakConstraintValuePos
    bl_label = "Tweak Constraint"
    bl_options = {"UNDO"}
    bl_description = "Tweak constraint's value or display position"

    type: StringProperty(name="Type")
    index: IntProperty(default=-1)

    def invoke(self, context: Context, event: Event):
        self.tweak = False
        self.init_mouse_pos = Vector((event.mouse_region_x, event.mouse_region_y))
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: Context, event: Event):
        delta = (
            self.init_mouse_pos - Vector((event.mouse_region_x, event.mouse_region_y))
        ).length
        if not self.tweak and delta > 6:
            self.tweak = True

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if not self.tweak:
                self.execute(context)
            return {"FINISHED"}

        if not self.tweak:
            return {"RUNNING_MODAL"}

        coords = event.mouse_region_x, event.mouse_region_y

        constraints = context.scene.sketcher.constraints
        constr = constraints.get_from_type_index(self.type, self.index)

        origin, end_point = functions.get_picking_origin_end(context, coords)
        pos = intersect_line_plane(origin, end_point, *constr.draw_plane())

        mat = constr.matrix_basis()
        pos = mat.inverted() @ pos

        constr.update_draw_offset(pos, context.preferences.system.ui_scale)
        context.space_data.show_gizmo = True
        return {"RUNNING_MODAL"}

    def execute(self, context: Context):
        bpy.ops.view3d.slvs_context_menu(type=self.type, index=self.index)
        return {"FINISHED"}


class SKETCHER_OT_add_preset_theme(AddPresetBase, Operator):
    """Add an Theme Preset"""

    bl_idname = Operators.AddPresetTheme
    bl_label = "Add Theme Preset"
    preset_menu = "SKETCHER_MT_theme_presets"

    preset_defines = [
        'prefs = bpy.context.preferences.addons["CAD_Sketcher"].preferences',
        "theme = prefs.theme_settings",
        "entity = theme.entity",
        "constraint = theme.constraint",
    ]

    preset_values = [
        "entity.default",
        "entity.highlight",
        "entity.selected",
        "entity.selected_highlight",
        "entity.inactive",
        "entity.inactive_selected",
        "constraint.default",
        "constraint.highlight",
        "constraint.failed",
        "constraint.failed_highlight",
        "constraint.text",
    ]

    preset_subdir = "bgs/theme"




constraint_operators = (
    VIEW3D_OT_slvs_add_distance,
    VIEW3D_OT_slvs_add_diameter,
    VIEW3D_OT_slvs_add_angle,
    VIEW3D_OT_slvs_add_coincident,
    VIEW3D_OT_slvs_add_equal,
    VIEW3D_OT_slvs_add_vertical,
    VIEW3D_OT_slvs_add_horizontal,
    VIEW3D_OT_slvs_add_parallel,
    VIEW3D_OT_slvs_add_perpendicular,
    VIEW3D_OT_slvs_add_tangent,
    VIEW3D_OT_slvs_add_midpoint,
    VIEW3D_OT_slvs_add_ratio,
)

from .stateful_operator.invoke_op import View3D_OT_invoke_tool

classes = (
    View3D_OT_slvs_trim,
    View3D_OT_slvs_test,
    View3D_OT_invoke_tool,
    View3D_OT_slvs_set_active_sketch,
    View3D_OT_slvs_set_all_constraints_visibility,
    View3D_OT_slvs_delete_entity,
    *constraint_operators,
    View3D_OT_slvs_delete_constraint,
    View3D_OT_slvs_tweak_constraint_value_pos,
    SKETCHER_OT_add_preset_theme,
)


def register():
    for cls in classes:
        if issubclass(cls, StatefulOperator):
            cls.register_properties()

        bpy.utils.register_class(cls)


def unregister():
    if global_data.offscreen:
        global_data.offscreen.free()
        global_data.offscreen = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
