"""Build the standalone pure-geometry snapshot from the editor's algorithms.

No editor storage, server, GUI or graph-executor dependencies enter this module.
The exporter embeds this snapshot in every notebook. Run after geometry changes.
"""
import ast
from pathlib import Path

APP=Path(__file__).resolve().parent/'app'

def main():
    nodes=ast.parse((APP/'nodes.py').read_text(encoding='utf-8-sig'))
    repair=ast.parse((APP/'voxel_repair_nodes.py').read_text(encoding='utf-8-sig'))
    defs={n.name:n for n in nodes.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    wanted={'_build_normal_thickened_mesh','_choose_mesh_scalar','_as_bool','_as_float','_as_int'}
    while True:
        extra={n.id for key in wanted for n in ast.walk(defs[key]) if isinstance(n,ast.Name) and n.id in defs}
        if extra<=wanted:break
        wanted|=extra
    boundary=defs['ExtractVoxelBoundariesNode']
    methods=[n for n in boundary.body if isinstance(n,ast.FunctionDef) and n.name not in {'run','_boundary_colors','save_polydata_as_vtp_and_vtu'}]
    standalone=ast.ClassDef(name='ExtractVoxelBoundariesNode',bases=[],keywords=[],body=methods,decorator_list=[])
    repair_end=next(i for i,n in enumerate(repair.body) if isinstance(n,ast.FunctionDef) and n.name=='reorder_mesh_by_material')
    repair_section=repair.body[:repair_end+1]
    repair_helpers=[n for n in repair_section if isinstance(n,ast.FunctionDef)]
    repair_constants=[n for n in repair_section if isinstance(n,ast.Assign)]
    cls=next(n for n in repair.body if isinstance(n,ast.ClassDef) and n.name=='RepairReorderVoxelMeshNode')
    run=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='run')
    block=next(n for n in run.body if isinstance(n,ast.Try)).body
    cutoff=next(i for i,n in enumerate(block) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='file_name' for t in n.targets))
    wrapper=ast.parse('def editor_repair(mesh, params):\n    _ensure_repair_dependencies()\n').body[0]
    wrapper.body+=block[:cutoff]+[ast.Return(value=ast.Name(id='mesh_reordered',ctx=ast.Load()))]
    parts=['from __future__ import annotations','from typing import Any, Dict, List, Optional, Sequence, Iterable, Tuple',
           'from pathlib import Path','import numpy as np','NodeExecutionError = ValueError',
           '# Generated from nodes.py and voxel_repair_nodes.py; do not edit this snapshot manually.']
    parts += [ast.unparse(ast.fix_missing_locations(n)) for n in nodes.body if isinstance(n,ast.FunctionDef) and n.name in wanted]
    parts += [ast.unparse(ast.fix_missing_locations(n)) for n in repair_constants+repair_helpers+[wrapper,standalone]]
    source='\n\n'.join(parts)+'\n'
    compile(source,'notebook_geometry_helpers.py','exec')
    (APP/'notebook_geometry_helpers.py').write_text(source,encoding='utf-8')
    print('Generated standalone geometry:',len(source),'characters')

if __name__=='__main__':main()
