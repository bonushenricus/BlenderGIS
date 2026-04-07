# -*- coding:utf-8 -*-
import os
import bpy
import bmesh
import mathutils

import logging
log = logging.getLogger(__name__)

from ..core.lib.shapefile import Writer as shpWriter
from ..core.lib.shapefile import POINTZ, POLYLINEZ, POLYGONZ, MULTIPOINTZ

from bpy_extras.io_utils import ExportHelper
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator

from ..geoscene import GeoScene
from ..core.proj import SRS

class EXPORTGIS_OT_shapefile(Operator, ExportHelper):
    """Export to ESRI shapefile (GN Attributes - Scalars Only)"""
    bl_idname = "exportgis.shapefile"
    bl_description = 'Export to ESRI shapefile. Ignores vector/color attributes.'
    bl_label = "Export SHP"
    bl_options = {"UNDO"}

    filename_ext = ".shp"
    filter_glob: StringProperty(
            default = "*.shp",
            options = {'HIDDEN'},
            )

    exportType: EnumProperty(
            name = "Feature type",
            description = "Select feature type",
            items = [
                ('POINTZ', 'Point', ""),
                ('POLYLINEZ', 'Line', ""),
                ('POLYGONZ', 'Polygon', "")
            ])

    objectsSource: EnumProperty(
            name = "Objects",
            description = "Objects to export",
            items = [
                ('COLLEC', 'Collection', "Export a collection of objects"),
                ('SELECTED', 'Selected objects', "Export the current selection")
            ],
            default = 'SELECTED'
            )

    def listCollections(self, context):
        return [(c.name, c.name, "Collection") for c in bpy.data.collections]

    selectedColl: EnumProperty(
        name = "Collection",
        description = "Select the collection to export",
        items = listCollections)

    mode: EnumProperty(
            name = "Mode",
            description = "Use MESH2FEAT to export attributes per-face/edge.",
            items = [
                ('OBJ2FEAT', 'Objects to features', "One feature per object"),
                ('MESH2FEAT', 'Mesh to features', "One feature per face/edge/point")
            ],
            default = 'OBJ2FEAT'
            )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'objectsSource')
        if self.objectsSource == 'COLLEC':
            layout.prop(self, 'selectedColl')
        layout.prop(self, 'mode')
        layout.prop(self, 'exportType')
        layout.label(text="Vectors/Colors skipped. Use MESH2FEAT for varying data.", icon='INFO')

    def execute(self, context):
        filePath = self.filepath
        folder = os.path.dirname(filePath)
        scn = context.scene
        geoscn = GeoScene(scn)

        # --- Georeferencing ---
        if geoscn.isGeoref:
            dx, dy = geoscn.getOriginPrj()
            crs = SRS(geoscn.crs)
            try:
                wkt = crs.getWKT()
            except Exception as e:
                log.warning('Cannot convert crs to wkt', exc_info=True)
                wkt = None
        elif geoscn.isBroken:
            self.report({'ERROR'}, "Scene georef is broken")
            return {'CANCELLED'}
        else:
            dx, dy = (0, 0)
            wkt = None

        # --- Selection ---
        if self.objectsSource == 'SELECTED':
            objects = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
        elif self.objectsSource == 'COLLEC':
            if self.selectedColl in bpy.data.collections:
                objects = bpy.data.collections[self.selectedColl].all_objects
                objects = [obj for obj in objects if obj.type == 'MESH']
            else:
                objects = []

        if not objects:
            self.report({'ERROR'}, "No mesh objects found")
            return {'CANCELLED'}

        # --- 1. ANALYZE ATTRIBUTES (SCALARS ONLY) ---
        mesh_attrs_to_export = {}
        cLen, nLen, dLen, maxLen = 255, 20, 5, 8
        
        # Tipi di dati sicuri da esportare
        SAFE_TYPES = ['FLOAT', 'INT', 'BOOLEAN', 'STRING']
        
        depsgraph = context.evaluated_depsgraph_get()
        sample_obj = objects[0].evaluated_get(depsgraph)
        sample_mesh = sample_obj.to_mesh()
        
        if sample_mesh:
            for attr in sample_mesh.attributes:
                # Salta se dominio non valido o nome interno
                if attr.domain not in ['EDGE', 'FACE', 'POINT'] or attr.name.startswith('_'):
                    continue
                
                # SALTA TUTTO QUELLO CHE NON È SCALARE (Vector, Color, ecc.)
                if attr.data_type not in SAFE_TYPES:
                    log.info(f"Skipping attribute '{attr.name}' (Type: {attr.data_type}) - Not a scalar.")
                    continue

                f_type = 'N' if attr.data_type in ['FLOAT', 'INT', 'BOOLEAN'] else 'C'
                
                mesh_attrs_to_export[attr.name] = {
                    'type': f_type,
                    'domain': attr.domain
                }
            sample_obj.to_mesh_clear()

        # --- 2. CREATE FIELDS ---
        outShp = shpWriter(filePath)
        if self.exportType == 'POLYGONZ': outShp.shapeType = POLYGONZ
        elif self.exportType == 'POLYLINEZ': outShp.shapeType = POLYLINEZ
        elif self.exportType == 'POINTZ' and self.mode == 'MESH2FEAT': outShp.shapeType = POINTZ
        elif self.exportType == 'POINTZ' and self.mode == 'OBJ2FEAT': outShp.shapeType = MULTIPOINTZ

        outShp.field('objId', 'N', nLen)

        for name, info in mesh_attrs_to_export.items():
            k = name[:maxLen]
            if k not in [f[0] for f in outShp.fields]:
                if info['type'] == 'C':
                    outShp.field(k, 'C', cLen)
                else:
                    outShp.field(k, 'N', nLen, dLen)

        for obj in objects:
            for k, v in obj.items():
                k_short = k[:maxLen]
                if k_short not in [f[0] for f in outShp.fields]:
                    if isinstance(v, (int, float)):
                        outShp.field(k_short, 'N', nLen, dLen)
                    elif isinstance(v, str):
                        outShp.field(k_short, 'C', cLen)

        # --- 3. EXPORT LOOP ---
        for i, obj in enumerate(objects):
            bm = bmesh.new()
            eval_obj = obj.evaluated_get(depsgraph)
            bm.from_object(eval_obj, depsgraph)
            bm.transform(obj.matrix_world)

            nFeat = 1
            geom_written = False

            # Geometry
            if self.exportType == 'POINTZ':
                if not bm.verts: bm.free(); continue
                pts = [[v.co.x+dx, v.co.y+dy, v.co.z] for v in bm.verts]
                if self.mode == 'MESH2FEAT':
                    for pt in pts: outShp.pointz(*pt)
                    nFeat = len(pts)
                else:
                    outShp.multipointz(pts)
                geom_written = True

            elif self.exportType == 'POLYLINEZ':
                if not bm.edges: bm.free(); continue
                lines = [[(v.co.x+dx, v.co.y+dy, v.co.z) for v in e.verts] for e in bm.edges]
                if self.mode == 'MESH2FEAT':
                    for line in lines: outShp.linez([line])
                    nFeat = len(lines)
                else:
                    outShp.linez(lines)
                geom_written = True

            elif self.exportType == 'POLYGONZ':
                if not bm.faces: bm.free(); continue
                polys = []
                for f in bm.faces:
                    p = [(v.co.x+dx, v.co.y+dy, v.co.z) for v in f.verts]
                    p.append(p[0])
                    p.reverse()
                    polys.append(p)
                if self.mode == 'MESH2FEAT':
                    for poly in polys: outShp.polyz([poly])
                    nFeat = len(polys)
                else:
                    outShp.polyz(polys)
                geom_written = True

            if not geom_written:
                bm.free()
                continue

            # Data Writing
            mesh_data = eval_obj.to_mesh()
            
            for n in range(nFeat):
                attributes = {'objId': i}
                
                if mesh_data:
                    for name, info in mesh_attrs_to_export.items():
                        if name in mesh_data.attributes:
                            layer = mesh_data.attributes[name]
                            read_index = n if self.mode == 'MESH2FEAT' else 0
                            
                            if read_index < len(layer.data):
                                # Accesso sicuro: sappiamo che sono scalari
                                val = layer.data[read_index].value
                                k_short = name[:maxLen]
                                
                                if info['type'] == 'N':
                                    try: attributes[k_short] = float(val)
                                    except: attributes[k_short] = None
                                else:
                                    attributes[k_short] = str(val)
                
                # Custom Props classiche
                for k, v in obj.items():
                    k_short = k[:maxLen]
                    if k_short in [f[0] for f in outShp.fields]:
                        attributes[k_short] = v

                # Fill missing
                for f in outShp.fields:
                    if f[0] not in attributes:
                        attributes[f[0]] = None

                outShp.record(**attributes)

            if mesh_data: eval_obj.to_mesh_clear()
            bm.free()

        outShp.close()

        if wkt:
            prjPath = os.path.splitext(filePath)[0] + '.prj'
            try:
                with open(prjPath, "w") as prj: prj.write(wkt)
            except: pass

        self.report({'INFO'}, "Export complete")
        return {'FINISHED'}

def register():
    try:
        bpy.utils.register_class(EXPORTGIS_OT_shapefile)
    except ValueError:
        unregister()
        bpy.utils.register_class(EXPORTGIS_OT_shapefile)

def unregister():
    bpy.utils.unregister_class(EXPORTGIS_OT_shapefile)
