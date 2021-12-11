
# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# Hell is other people's code

import bmesh
import bpy
import bpy.props
from mathutils import Vector, Euler

import dataclasses
import json
from math import radians
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, cast

from .lookup_funcs import wmo_read_color, wmo_read_group_flags
from .node_groups import do_wmo_combiner, get_utility_group
from .preferences import WoWbject_ObjectProperties, get_prefs
from .wbjtypes import JsonWmoGroup, JsonWmoMetadata, JsonWmoRenderBatch, Vec2, Vec3, Vec4, Tri, iColor3, iColor4, fColor3, fColor4

from .vendor import mashumaro
from .utilities import recursive_remove_doubles, tris_to_quads

# FIXME: Which of these should be general, and which wmo-specific?
@dataclasses.dataclass
class ObjGroup:
    """
    An individual object group from within an obj. There is one of these
    per obj object group.
    """
    mtlname: Optional[str] = None
    name: Optional[str] = None
    # FIXME: should this be 1-based like other obj things?
    verts: Set[int] = dataclasses.field(default_factory=set)
    """set of vertex indexes (0-based!) used in group"""
    faces: List[Tri] = dataclasses.field(default_factory=list)
    """list of faces in group, using vertex indexes (1-based)"""
    # uv: List[Vec2] = dataclasses.field(default_factory=list)  # not used?


@dataclasses.dataclass
class ObjData:
    """
    Raw data as read from an obj file, as exported from wow.export. Includes
    vertexes, faces (as indexes into vertex list), normals, UV maps, etc for
    the entire WMO, root + groups
    """
    usemtl: str = ""
    mtlfile: str = ""
    name: Optional[str] = None
    verts: List[Vec3] = dataclasses.field(default_factory=list)
    """vertexes (x,y,z) - 1-indexed when referenced by 'faces' field"""
    # Pretty sure 'faces' isn't needed, because all the faces are defined in
    # the individual groups?
    # faces: List[Tri] = dataclasses.field(default_factory=list)
    # """faces (v1, v2, v3) - right-handed; indexes into 'verts' (1-based)"""
    normals: List[Vec3] = dataclasses.field(default_factory=list)
    """vertex normals (x, y, z) - might not be unit vectors"""
    uv: List[Vec2] = dataclasses.field(default_factory=list)
    uv2: List[Vec2] = dataclasses.field(default_factory=list)
    uv3: List[Vec2] = dataclasses.field(default_factory=list)
    groups: List[ObjGroup] = dataclasses.field(default_factory=list)



@dataclasses.dataclass(init=False)
class WmoGroup:
    """
    An individual group from within a WMO. There is one of these per WMO
    group.
    """
    obj_data: ObjData
    """the full obj data for the entire import"""

    json_group: JsonWmoGroup
    """the metadata, from json, for this specific WMO group"""

    group_offset: int = -1

    batch_count: int = -1
    mesh_batches: List[ObjGroup] = dataclasses.field(default_factory=list)
    json_batches: List[JsonWmoRenderBatch] = dataclasses.field(default_factory=list)
    colors: List[List[int]] = dataclasses.field(default_factory=list)


# originally "initialize_mesh"
# format description: https://en.wikipedia.org/wiki/Wavefront_.obj_file
# TODO: Replace with a more robust port of the ImportObj add-on's process
def read_obj(file: Path) -> ObjData:
    print(f"Importing obj data from {file}")
    # START def initialize_mesh(mesh_path: str):
    obj_data = ObjData()
    groupIndex: int = -1

    # FIXME: what -is- the right encoding for obj files? Can we make this
    # read as utf-8 or ascii instead?
    with file.open('rb') as f:
        for line in f:
            line_split = line.split()
            if not line_split:
                continue
            line_start = line_split[0]

            # actual obj can have multiple mtllib files, but wow.export only
            # exports one. We probably don't need this though.
            if line_start == b'mtllib':
                obj_data.mtlfile = str(line_split[1])

            # Vertexes
            elif line_start == b'v':
                obj_data.verts.append(
                    cast(Vec3, tuple([float(v) for v in line_split[1:4]])))

            # Vertex Normals
            elif line_start == b'vn':
                obj_data.normals.append(
                    cast(Vec3, tuple([float(v) for v in line_split[1:4]])))

            # UV/texture coordinates (3rd set)
            elif line_start == b'vt3':
                obj_data.uv3.append(
                    cast(Vec2, tuple([float(v) for v in line_split[1:3]])))

            # UV/texture coordinates (2nd set)
            elif line_start == b'vt2':
                # FIXME: What is the 'undefined' bit about?
                if not line_split[1] == b'undefined':
                    obj_data.uv2.append(
                        cast(Vec2, tuple([float(v) for v in line_split[1:3]])))
                else:
                    obj_data.uv2.append((0.0, 0.0))

            # UV/texture coordinates (1st set)
            elif line_start == b'vt':
                obj_data.uv.append(
                    cast(Vec2, tuple([float(v) for v in line_split[1:3]])))

            # Faces -- 3 fields (for WoW), in the format of vertex/uv/normal
            elif line_start == b'f':
                if groupIndex < 0:
                    raise RuntimeError("obj file specifies faces before a face group")

                line_split = line_split[1:]
                # makes list from first element (vertex index) of each vertex
                fv = [int(v.split(b'/')[0]) for v in line_split]

                obj_data.groups[groupIndex].faces.append((fv[0], fv[1], fv[2]))
                obj_data.groups[groupIndex].verts.update([i - 1 for i in fv])

            # Object name
            elif line_start == b'o':
                obj_data.name = str(line_split[1])

            # Groups
            elif line_start == b'g':
                groupIndex += 1
                obj_data.groups.append(ObjGroup())
                obj_data.groups[groupIndex].name = line_split[1].decode('utf-8')

            # Smooth shading
            # Materials
            elif line_start == b'usemtl':
                obj_data.groups[groupIndex].mtlname = line_split[1].decode('utf-8')

    total_verts = sum([len(g.verts) for g in obj_data.groups])
    total_faces = sum([len(g.faces) for g in obj_data.groups])
    print(f"Read {groupIndex + 1} groups, {total_verts} total verts and {total_faces} total faces")

    return obj_data



# FIXME: return type
def import_wmo(context: bpy.types.Context, filepath: str, reuse_mats: bool, base_shader: str, op_args: Dict[str, Any]) -> None:
    debug = True

    file = Path(filepath)
    name_override = op_args.get("name_override")

    print("loading json metadata")
    # if do_search:
    if not file.with_suffix(".json").exists():
        # FIXME: user-facing error handling
        raise RuntimeError(
            f"failed to load metadata from '{file.with_suffix('.json')}, can't continue")

    with file.with_suffix(".json").open() as p:
        # FIXME: error handling here
        json_config = json.load(p)

    if not json_config:
        raise RuntimeError(f"failed to load metadata file {file.with_suffix('.json')}")

    if name_override:
        basename = name_override
    else:
        basename = file.stem

    print(f"will be importing using base name: {basename}")

    # START def do_setup(self, files, directory, op_args, **kwargs):
    if True:
        # START def setup_json_data(self, **kwargs):
        if ".wmo" not in json_config["fileName"]:
            raise ValueError("import_wmo called to import a non-WMO file")

        try:
            json_config = JsonWmoMetadata.from_dict(json_config)  # type: ignore
        # FIXME: Not sure why pylance can't figure out the types here
        except mashumaro.exceptions.MissingField as e:
            raise RuntimeError(str(e))
        except mashumaro.exceptions.InvalidFieldValue as e:
            raise RuntimeError("invalid field value (not repeating)")
        # END setup_json_data

        # START def setup_bl_object(self, progress):
        # END -- tail call to import_obj

        # START def import_obj(file, directory, reuse_mats, name_override, merge_verts, make_quads, use_collections, import_container, progress, **kwargs):
        if True:
            # FIXME: Is the poll needed? Is it even valid? select_all is a function?
            if bpy.ops.object.select_all.poll():  # type: ignore
                bpy.ops.object.select_all('INVOKE_DEFAULT', False, action='DESELECT')

            print("Reading OBJ File")

            # START def initialize_mesh(mesh_path: str):
            obj_data = read_obj(file)

            # for i, group in enumerate(obj_data.groups):
            #     print(f"obj group {i} has {len(group.verts)} verts")

            newMesh = bpy.data.meshes.new(basename)
            newMesh.use_auto_smooth = True
            newMesh.auto_smooth_angle = radians(60)

            newObj = bpy.data.objects.new(basename, newMesh)
            WBJ = cast(WoWbject_ObjectProperties, newObj.WBJ)  # type: ignore

            # FIXME: Not sure how to type annotate the props to make these not
            # complain? But should be possible?
            WBJ.source_file = str(file.name)
            WBJ.source_directory = str(file.parent)
            WBJ.initialized = True

            # START def repack_wmo(import_container, groups: dict, obj_data: meshObject, config: dict):
            wmo_groups: List[WmoGroup] = []

            # ? Why was this all the way up here? Seems only needed much further down?
            # offset = 0

            # not sure why we need the big flat vertex color list -- ask Asher,
            # if he reappears
            flat_colors: List[List[int]] = [[], [], []]

            # So, there's going to be more vertex colors than there are verts.
            # This is because the collision meshes (as definedd in MOBN and MOBR
            # chunks in the WMO) reference verts that are not actually included
            # in the OBJ export, but the vertex color data (from MOCV) includes
            # those verts, and is passed through unchanged.
            #
            # An extremely brief eyeball suggests that vertex colors for verts
            # which got elided have a vertex color of 4286545791 (0xff7f7f7f
            # (in abgr, I think)), which might be worth checking for as a
            # sanity check to make sure we're not off in our count somewhere.
            # This only applies to the first set of vertex colors.
            total_groups = -1
            total_batches = -1
            for i, group in enumerate(json_config.groups):
                total_groups += 1
                verts_in_json_group = 0
                for j, batch in enumerate(group.renderBatches):
                    total_batches += 1
                    verts_in_json_batch = batch.lastVertex - batch.firstVertex + 1
                    verts_in_json_group += verts_in_json_batch
                    obj_group_verts = len(obj_data.groups[total_batches].verts)

                    if debug:
                        print(
                            f"verts in json group {i:-2d} batch {j:-2d} (global {total_groups:-2d}, {total_batches:-3d}) = {verts_in_json_batch:-3d}     verts in obj group: {obj_group_verts}")

                    if verts_in_json_batch != obj_group_verts:
                        print(
                            f"WARNING: sanity check failed: unequal vert counts; verts in json group {i:-2d} batch {j:-2d} (global {total_batches:-3d}): {verts_in_json_batch:-3d}     verts in obj group: {obj_group_verts}")

                # if len(group.vertexColours) == 0:
                #     vcol_count = 0
                # else:
                #     vcol_count = len(group.vertexColours[0])

            # these are WMO groups here
            offset = 0
            for group in json_config.groups:
                wmo_group = WmoGroup()
                wmo_group.obj_data = obj_data
                wmo_group.batch_count = len(group.renderBatches)
                wmo_groups.append(wmo_group)

                if wmo_group.batch_count > 0:
                    wmo_group.json_group = group
                    wmo_group.json_batches = group.renderBatches

                    # the obj is all the batches, each as a group, so we need
                    # to know which of those represent the batches in this
                    # WMO group. Gah, we need better terminology, using 'group'
                    # waaaaay too many places here.
                    batch_range = slice(offset, offset + wmo_group.batch_count)
                    wmo_group.mesh_batches = obj_data.groups[batch_range]

                    # These are the vertex colors for the entire WMO group,
                    # which consists of multiple obj groups/wmo render batches.
                    vcolors = group.vertexColours

                    # if there isn't a lastVertex, it will be -1, thus making
                    # this 0. Otherwise, gives us the number of vertexes (thus
                    # vertex colors) in the entire WMO group.
                    last_vertex = group.renderBatches[-1].lastVertex + 1

                    group_vertex_count = 0

                    # FIXME: Can/does this differ from the vertex count in
                    # the json metadata?
                    for batch in wmo_group.mesh_batches:
                        group_vertex_count += len(batch.verts)

                    if group_vertex_count != last_vertex:
                        print(
                            f"WARNING: vertex count mismatch: {group_vertex_count} != {last_vertex}")

                    # Not sure why we're doing this, rather than just updating the
                    # group.vertexColours struct in place, or copying/updating
                    # to wmo_group.something
                    if len(vcolors) == 2:
                        flat_colors[0] += vcolors[0][0:last_vertex]
                        flat_colors[1] += vcolors[1][0:last_vertex]
                    elif len(vcolors) == 1:
                        flat_colors[0] += vcolors[0][0:last_vertex]
                        flat_colors[1] += [0 for _ in range(last_vertex)]
                    elif len(vcolors) == 0:
                        if group_vertex_count > 0:
                            flat_colors[0] += [0 for _ in range(last_vertex)]
                            flat_colors[1] += [0 for _ in range(last_vertex)]

                    l1 = len(flat_colors[0])
                    l2 = len(flat_colors[1])

                    if debug:
                        print(
                            f"color1 count: {l1}   color2 count: {l2}   group vertex count: {group_vertex_count}")

                    if l1 != group_vertex_count:
                        print(
                            f"WARNING: vertex color count mismatch: {group_vertex_count} != {l1}")

                    wmo_group.colors = flat_colors

                    offset += wmo_group.batch_count
                    wmo_group.group_offset = offset

                # wmo_group.batch_count == 0
                else:
                    print(f"{group.groupName} {group.groupDescription} Batchless")
                    print(f"numPortals: {group.numPortals}")
                    print(f"numBatchesA: {group.numBatchesA}")
                    print(f"numBatchesB: {group.numBatchesB}")
                    print(f"numBatchesC: {group.numBatchesC}")

            # END repack_wmo

            # FIXME: Is mat_dict a dict or a list?
            mat_dict: Dict[int, bpy.types.Material] = {}

            for i, mat in enumerate(json_config.materials):
                mat = bpy.data.materials.new(name=basename + "_mat_" + str(i))
                mat.use_nodes = True
                mat_dict[i] = mat

            objects: List[bpy.types.Object] = []

            steps = len(wmo_groups)
            print(f"working on {steps} groups")

            print("Generating meshes")
            for i, group in enumerate(wmo_groups):
                # sub = group.json_group.groupName or ""
                #### print(f"Constructing object {i + 1}/{steps} | {group.json_group.groupName}")

                # FIXME: revisit -- should inline this probably, too
                bl_obj = wmo_setup_blender_object(
                    # FIXME: make group num into metadata
                    base_name=f"{i:03d}_{basename}",
                    group=group,
                    mat_dict=mat_dict,
                    # merge_verts=merge_verts,
                    # make_quads=make_quads,
                    # use_collections=use_collections
                )

                if bl_obj:
                    objects.append(bl_obj)

            # END initialize_mesh (maybe?)
        # END import_obj

        print("leaving substeps")

        print("generating materials")

        # START def setup_materials(self):
        # START def do_wmo_mats(**kwargs):

        configured_mats: Set[bpy.types.Material] = set()

        # for obj in container.bl_obj:
        for obj in objects:
            # FIXME: give MaterialSlot an __iter__  method instead of the typecast?
            for slot in obj.material_slots:
                mat_number = slot.material.name.split('_')[-1]
                if '.' in mat_number:
                    mat_number = mat_number.split('.')[0]
                mat = json_config.materials[int(mat_number)]

                # next bits simplified from node_groups.py:get_tex
                # tex1 = get_tex(container, str(mat.get("texture1")))
                tex1: Optional[bpy.types.Image] = None
                texnum = mat.texture1
                texfilename = f"{texnum}.png"
                texfile = file.parent / texfilename

                if texnum > 0 and texfile.exists():
                    if texfilename in bpy.data.images:
                        tex1 = bpy.data.images[texfilename]
                    else:
                        tex1 = bpy.data.images.load(str(texfile.resolve()))
                        tex1.alpha_mode = 'CHANNEL_PACKED'

                # tex2 = get_tex(container, str(mat.get("texture2")))
                tex2: Optional[bpy.types.Image] = None
                texnum = mat.texture2
                texfilename = f"{texnum}.png"
                texfile = file.parent / texfilename

                if texnum > 0 and texfile.exists():
                    if texfilename in bpy.data.images:
                        tex2 = bpy.data.images[texfilename]
                    else:
                        tex2 = bpy.data.images.load(str(texfile.resolve()))
                        tex2.alpha_mode = 'CHANNEL_PACKED'

                # tex3 = get_tex(container, str(mat.get("texture3")))
                tex3: Optional[bpy.types.Image] = None
                texnum = mat.texture3
                texfilename = f"{texnum}.png"
                texfile = file.parent / texfilename
                if texnum > 0 and texfile.exists():
                    if texfilename in bpy.data.images:
                        tex3 = bpy.data.images[texfilename]
                    else:
                        tex3 = bpy.data.images.load(str(texfile.resolve()))
                        tex3.alpha_mode = 'CHANNEL_PACKED'

                tex_list = [tex1, tex2, tex3]

                bl_mat = slot.material
                tree = bl_mat.node_tree
                nodes = tree.nodes

                if bl_mat in configured_mats:
                    continue

                shader: Optional[bpy.types.Node] = None
                out_node = None
                # FIXME: give Nodes an __iter__ instead of using this typecast?
                for node in nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        shader = cast(bpy.types.ShaderNodeBsdfPrincipled, node)
                        shader.inputs["Roughness"].default_value = 1.0

                    if node.type == 'OUTPUT_MATERIAL':
                        out_node = node

                    if shader and out_node:
                        break

                if not out_node:
                    out_node = nodes.new('ShaderNodeOutputMaterial')

                if not shader:
                    # FIXME
                    print("DO LATER")

                # FIXME: What's this for?
                if shader:
                    nodes.remove(shader)

                # FIXME: wtf is this base shader thing actually doing?
                prefs = get_prefs()
                base = prefs.get_base_shader(base_shader)

                if base == 'Experimental':
                    shader = nodes.new('ShaderNodeGroup')
                    shader.node_tree = get_utility_group(name="TheStumpFargothHidTheRingIn")
                elif (base != ''):
                    shader = cast(bpy.types.ShaderNode, nodes.new(base))
                else:
                    shader = nodes.new("ShaderNodeEmission")

                tree.links.new(shader.outputs[0], out_node.inputs[0])

                baseColor = nodes.new('ShaderNodeRGB')
                baseColor.location += Vector((-1200.0, 400.0))
                baseColor.outputs["Color"].default_value = wmo_read_color(
                    mat.color2, 'CArgb')
                baseColor.label = 'BASE COLOR'

                tex_nodes: List[bpy.types.ShaderNodeTexImage] = []

                for i, tex in enumerate(tex_list):
                    if tex:
                        tex_node = nodes.new('ShaderNodeTexImage')
                        tex_node.image = tex
                        tex_node.location += Vector((-1200.0, (200 - i * 300.0)))
                        tex_node.label = ("TEXTURE_%s" % str(i + 1))
                        tex_nodes.append(tex_node)

                ambColor = wmo_read_color(json_config.ambientColor, 'CImVector')

                do_wmo_combiner(
                    tex_nodes=tex_nodes,
                    bl_mat=bl_mat,
                    shader_out=shader,
                    mat_info=mat,
                    ambient=ambColor,
                )

                configured_mats.add(bl_mat)
            # END do_wmo_mats
            # END setup_materials


# FIXME: Legit needs fewer arguments
def wmo_setup_blender_object(base_name: str, group: WmoGroup,
                             mat_dict: Dict[int, bpy.types.Material],
                             merge_verts: bool = False, make_quads: bool = False,
                             use_collections: bool = True) -> Optional[bpy.types.Object]:
    if group.batch_count < 1:
        return None

    obj_data = group.obj_data
    json_group = group.json_group

    full_name = base_name + "_" + (json_group.groupName or "section")

    mesh = bpy.data.meshes.new(base_name)
    mesh.use_auto_smooth = True
    mesh.auto_smooth_angle = radians(60)

    newObj = bpy.data.objects.new(full_name, mesh)
    WBJ = cast(WoWbject_ObjectProperties, newObj.WBJ)  # type: ignore
    WBJ.wow_model_type = 'WMO'
    WBJ.initialized = True

    flags = wmo_read_group_flags(json_group.flags)  # FIXME: do we need a 0 default?
    if 'INTERIOR' in flags and 'EXTERIOR' in flags:
        WBJ.wmo_lighting_type = 'TRANSITION'
    elif 'INTERIOR' in flags:
        WBJ.wmo_lighting_type = 'INTERIOR'
    elif 'EXTERIOR' in flags:
        WBJ.wmo_lighting_type = 'EXTERIOR'
    else:
        WBJ.wmo_lighting_type = 'UNLIT'


    bm = bmesh.new()
    # vcols = bm.loops.layers.color.new("vcols")
    uv_layer = bm.loops.layers.uv.new()

    batches = group.mesh_batches
    json_batches = group.json_batches
    color_list = [clist for clist in group.colors if len(clist) > 0]
    do_colors = True if len(color_list) > 0 else False

    # from pprint import pformat
    # before = str(group.colors)
    # after = str(color_list)
    # Path(f"before_{group.json_group.groupID}.txt").write_text(pformat(before))
    # Path(f"after_{group.json_group.groupID}.txt").write_text(pformat(after))

    # if before != after:
    #     print("color comparison differs")

    colors = {}
    v_dict: Dict[int, bmesh.types.BMVert] = {}
    # uv_dict = {}

    for i, batch in enumerate(batches):
        exampleFace: Optional[bmesh.types.BMFace] = None
        # cull_list = []
        for face in batch.faces:
            for v in face:
                # if type(v_dict.get(v)) == bmesh.types.BMVert:
                #     vert = v_dict[v]
                # else:
                #     vert = bm.verts.new(obj_data.verts[v - 1])
                #     v_dict[v] = vert
                # FIXME: what's up with the switcheroo types here?
                if v in v_dict:
                    vert = v_dict[v]
                else:
                    vert = bm.verts.new(obj_data.verts[v - 1])
                    v_dict[v] = vert

                # The data layer for vertex color is actually in the face loop.
                # We can't set the color until after all of the faces are made,
                # So we throw it all into a dictionary and brute-force it later.
                # Note: There can theoretically be three sets of vertex colors.
                if do_colors:
                    if type(color_list[0]) == list:
                        v_color: List[fColor4] = []
                        for sublist in color_list:
                            v_color.append(wmo_read_color(sublist[v - 1], 'CImVector'))
                    else:
                        # FIXME: What even -is- this?
                        x = color_list
                        v_color = wmo_read_color(color_list[v - 1], 'CImVector')  # ????

                    colors[vert] = v_color

            try:
                if not exampleFace:
                    bface = bm.faces.new((
                        v_dict[face[0]],
                        v_dict[face[1]],
                        v_dict[face[2]]
                    ))
                    exampleFace = bface

                    if json_batches[i].flags == 2:
                        # FIXME: what is this?
                        mat_ID = json_batches[i].possibleBox2[2]
                    else:
                        mat_ID = json_batches[i].materialID

                    # FIXME: is there a way to not have to always repeat this cast?
                    local_index = cast(bpy.types.Mesh, newObj.data).materials.find(
                        mat_dict[int(mat_ID)].name)

                    if local_index == -1:
                        # FIXME: typing
                        cast(bpy.types.Mesh, newObj.data).materials.append(
                            mat_dict[int(mat_ID)])
                        bface.material_index = cast(bpy.types.Mesh, newObj.data).materials.find(
                            mat_dict[int(mat_ID)].name)
                    else:
                        bface.material_index = local_index

                else:
                    bface = bm.faces.new((
                        v_dict[face[0]],
                        v_dict[face[1]],
                        v_dict[face[2]]
                    ), exampleFace)

            except ValueError as e:
                v1 = bm.verts.new(obj_data.verts[face[0] - 1])
                v2 = bm.verts.new(obj_data.verts[face[1] - 1])
                v3 = bm.verts.new(obj_data.verts[face[2] - 1])

                # FIXME: types?
                colors[v1] = colors[v_dict[face[0]]]
                colors[v2] = colors[v_dict[face[1]]]
                colors[v3] = colors[v_dict[face[2]]]

                if not exampleFace:
                    bface = bm.faces.new((v1, v2, v3))
                    exampleFace = bface

                    if json_batches[i].flags == 2:
                        mat_ID = int(json_batches[i].possibleBox2[2])  # WTF is this?
                    else:
                        mat_ID = json_batches[i].materialID

                    local_index = cast(bpy.types.Mesh, newObj.data).materials.find(
                        mat_dict[mat_ID].name)

                    if local_index == -1:
                        cast(bpy.types.Mesh, newObj.data).materials.append(mat_dict[mat_ID])
                        bface.material_index = cast(bpy.types.Mesh, newObj.data).materials.find(
                            mat_dict[mat_ID].name)
                    else:
                        bface.material_index = local_index
                else:
                    bface = bm.faces.new((v1, v2, v3), exampleFace)

                err_detail = (
                    f"Duplicate Face: {face[0]}/{face[0]}/{face[0]} {face[1]}/{face[1]}/{face[1]} {face[2]}/{face[2]}/{face[2]}")
                # print(err_detail)


    if len(obj_data.uv2) > 0:
        uv2_layer = bm.loops.layers.uv.new('UV2Map')

    if len(obj_data.uv3) > 0:
        uv3_layer = bm.loops.layers.uv.new('UV3Map')

    bm.faces.ensure_lookup_table()

    face_list = [batch.faces for batch in batches]
    face_list = [face for sublist in face_list for face in sublist]

    for i, face in enumerate(face_list):
        for j, loop in enumerate(bm.faces[i].loops):  # <--- fix types
            loop[uv_layer].uv = obj_data.uv[face[j] - 1]

            if len(obj_data.uv2) > 0:
                loop[uv2_layer].uv = obj_data.uv2[face[j] - 1]

            if len(obj_data.uv3) > 0:
                loop[uv3_layer].uv = obj_data.uv3[face[j] - 1]

    # bm.verts.ensure_lookup_table()

    if len(color_list) > 0:
        vcols = []
        for i, clist in enumerate(color_list):
            vcols.append(bm.loops.layers.color.new(f"vcols_{i}"))

        for i, vert in enumerate(bm.verts):
            for loop in vert.link_loops:
                for i, vcol_list in enumerate(vcols):
                    loop[vcol_list] = colors[vert][i]

    if merge_verts:
        st = recursive_remove_doubles(bm, verts=bm.verts, dist=0.00001)
        print(
            f"{newObj.name}:"
            f" {st['removed_verts']} of {st['start_verts']} verts removed"
            f" in {st['merge_passes']} passes"
            f" in {st['total_time']:1.6f}s"
            f" ({st['end_verts']} verts remain)"
        )

    bm.to_mesh(mesh)
    bm.free()

    newObj.rotation_euler = Euler((0, 0, 0))
    newObj.rotation_euler.x = radians(90)

    collection_name = json_group.groupDescription
    if use_collections and collection_name:
        if collection_name in bpy.data.collections:
            collection = bpy.data.collections[collection_name]
        else:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)

        collection.objects.link(newObj)
    else:
        bpy.context.view_layer.active_layer_collection.collection.objects.link(
            newObj)


    if make_quads:
        st = tris_to_quads(newObj, 5.0)
        print(
            f"{newObj.name}:"
            f" {st['removed_faces']} of {st['start_faces']} faces removed"
            f" in {st['total_time']:1.6f}s"
            f" ({st['end_faces']} faces remain)"
        )

    # Give us a reasonable origin on everything
    bpy.ops.object.select_all('INVOKE_DEFAULT', False, action='DESELECT')
    newObj.select_set(True)
    bpy.ops.object.origin_set('INVOKE_DEFAULT', False, type='ORIGIN_GEOMETRY', center='MEDIAN')
    bpy.ops.object.shade_smooth('INVOKE_DEFAULT', False)

    return newObj
