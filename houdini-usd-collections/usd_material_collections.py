"""
USD Material Collections Generator for Houdini Solaris
======================================================
Author: Teja Ravi Varma
GitHub: https://github.com/TEJARAVIVARMA/houdini-usd-collections
License: MIT

Description:
    Automatically generates USD Collections in Houdini Solaris (LOPs) based on
    assigned materials and mesh naming suffixes. Eliminates manual wildcard creation
    for complex assets with dozens of materials, guaranteeing collision-free matching
    and seamless binding in Material Library LOPs.
"""

import os
import re
from typing import Dict, List, Optional, Set, Tuple
import hou
from pxr import Usd, UsdGeom, UsdShade


def extract_material_names(
    lop_node: hou.LopNode,
    method: str = "auto",
    max_words: int = 3,
    delimiter: str = "_",
    asset_prefix: Optional[str] = None,
    verbose: bool = True
) -> Dict[str, dict]:
    """
    Extracts unique material names and associated meshes from a LOP stage.

    Args:
        lop_node: The LOP node to read the USD stage from.
        method: 'auto' (checks USD bindings first, then mesh names),
                'usd_bindings', or 'mesh_names'.
        max_words: Maximum number of tokens at the end of the mesh name (default: 3).
        delimiter: Delimiter separating name tokens (default: '_').
        asset_prefix: Explicit prefix to strip (e.g. 'F512S_'). If None, auto-detected.
        verbose: Whether to print status messages.

    Returns:
        Dictionary mapping clean material names to metadata and bound meshes.
    """
    if lop_node is None:
        raise ValueError("No source LOP node provided.")

    stage = lop_node.stage()
    if not stage:
        raise ValueError(f"Could not retrieve USD stage from node: {lop_node.path()}")

    meshes = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    if not meshes:
        if verbose:
            print(f"[USD Collections] Warning: No UsdGeom.Mesh prims found on stage of {lop_node.path()}")
        return {}

    if verbose:
        print(f"[USD Collections] Found {len(meshes)} mesh primitives on stage.")

    # -------------------------------------------------------------------------
    # 1. USD Material Bindings (Primary & most accurate)
    # -------------------------------------------------------------------------
    binding_map = {}
    raw_bindings: Set[str] = set()

    for m in meshes:
        binding = UsdShade.MaterialBindingAPI(m).GetDirectBinding()
        mat = binding.GetMaterial()
        if mat:
            mat_raw = mat.GetPath().name
            raw_bindings.add(mat_raw)
            binding_map.setdefault(mat_raw, []).append(m)

    can_use_bindings = len(raw_bindings) > 0 and (method in ("auto", "usd_bindings"))

    if can_use_bindings:
        # Auto-detect common asset prefix among bindings (e.g., 'F512S_')
        if asset_prefix is None:
            common_pref = os.path.commonprefix(list(raw_bindings))
            if delimiter in common_pref:
                asset_prefix = common_pref[:common_pref.rfind(delimiter) + 1]
            else:
                asset_prefix = ""

        material_dict = {}
        for raw_name, bound_meshes in binding_map.items():
            if asset_prefix and raw_name.startswith(asset_prefix):
                clean_name = raw_name[len(asset_prefix):]
            else:
                clean_name = raw_name

            material_dict[clean_name] = {
                "meshes": bound_meshes,
                "raw_name": raw_name,
                "source": "usd_binding"
            }

        if verbose:
            print(f"[USD Collections] Extracted {len(material_dict)} unique materials from USD bindings.")
            if asset_prefix:
                print(f"[USD Collections] Stripped asset prefix: '{asset_prefix}'")
        return material_dict

    # -------------------------------------------------------------------------
    # 2. Mesh Prim Name Suffix Parsing (Fallback)
    # -------------------------------------------------------------------------
    mesh_names = [m.GetName() for m in meshes]

    if asset_prefix is None:
        candidate_counts: Dict[str, int] = {}
        for name in mesh_names:
            matches = re.findall(r'_([A-Z0-9]{3,})_', name)
            for m in matches:
                candidate_counts[m] = candidate_counts.get(m, 0) + 1
        if candidate_counts:
            best_code = max(candidate_counts, key=candidate_counts.get)
            if candidate_counts[best_code] > len(mesh_names) * 0.5:
                asset_prefix = best_code + "_"

    asset_prefix = asset_prefix or ""
    material_dict = {}

    for m in meshes:
        name = m.GetName()
        clean_name = None

        if asset_prefix and f"_{asset_prefix}" in name:
            clean_name = name.split(f"_{asset_prefix}")[-1]
        elif asset_prefix and asset_prefix in name:
            clean_name = name.split(asset_prefix)[-1]
        else:
            parts = name.split(delimiter)
            token_count = min(len(parts), max_words)
            clean_name = delimiter.join(parts[-token_count:])

        if clean_name not in material_dict:
            material_dict[clean_name] = {
                "meshes": [],
                "raw_name": clean_name,
                "source": "mesh_name"
            }
        material_dict[clean_name]["meshes"].append(m)

    if verbose:
        print(f"[USD Collections] Extracted {len(material_dict)} unique materials from mesh name suffixes.")
        if asset_prefix:
            print(f"[USD Collections] Used asset prefix delimiter: '{asset_prefix}'")

    return material_dict


def compute_exclusion_rules(material_names: List[str]) -> Dict[str, List[str]]:
    """
    Detects collisions where a shorter wildcard pattern (e.g. '*Glass')
    would erroneously match longer material suffixes (e.g. '*Red_Glass' or '*Motor_Glass').

    Returns:
        Mapping of {material_name: [conflicting_longer_material_1, ...]}
    """
    exclusions = {}
    for m1 in material_names:
        longer = [
            m2 for m2 in material_names
            if m2 != m1 and (m2.endswith(f"_{m1}") or m2.endswith(m1))
        ]
        if longer:
            exclusions[m1] = longer
    return exclusions


def create_single_collection_node(
    parent_node: hou.Node,
    node_name: str = "MaterialsCollections",
    material_names: Optional[List[str]] = None,
    input_node: Optional[hou.Node] = None,
    default_prim_path: Optional[str] = None,
    use_prefix_anchor: bool = False,
    asset_prefix: str = "",
    auto_exclude: bool = True
) -> hou.LopNode:
    """
    Creates a single 'collection::2.0' LOP node containing all collections as multiparms.
    Leaves 'defaultprimpath' at factory default ('/collections') so that
    Solaris primitive pattern matching (%<collection_name>) automatically detects it.
    """
    if material_names is None:
        material_names = []

    sorted_mats = sorted(material_names)
    col_node = parent_node.node(node_name)
    if not col_node:
        col_node = parent_node.createNode("collection::2.0", node_name)

    if input_node:
        col_node.setInput(0, input_node)

    col_node.parm("createprimitive").set(1)

    # Preserve factory default (/collections) unless explicitly customized
    if default_prim_path is not None:
        col_node.parm("defaultprimpath").set(default_prim_path)
    else:
        col_node.parm("defaultprimpath").revertToDefaults()

    col_node.parm("collectioncount").set(len(sorted_mats))
    exclusions_map = compute_exclusion_rules(sorted_mats) if auto_exclude else {}

    for idx, mat_name in enumerate(sorted_mats, start=1):
        col_node.parm(f"enable{idx}").set(1)
        col_node.parm(f"collectionname{idx}").set(mat_name)
        col_node.parm(f"expansionrule{idx}").set("explicitOnly")

        # Keep default expression `chs("defaultprimpath")`
        if col_node.parm(f"primpath{idx}"):
            col_node.parm(f"primpath{idx}").revertToDefaults()

        if use_prefix_anchor and asset_prefix:
            pattern = f"**/*{asset_prefix}{mat_name}"
        else:
            pattern = f"**/*{mat_name}"

        col_node.parm(f"includepattern{idx}").set(pattern)

        # Apply exclusions for collision avoidance
        if mat_name in exclusions_map and not use_prefix_anchor:
            col_node.parm(f"doexclusions{idx}").set(1)
            excl_str = " ".join([f"**/*{m}" for m in exclusions_map[mat_name]])
            col_node.parm(f"excludepattern{idx}").set(excl_str)
        else:
            if col_node.parm(f"doexclusions{idx}"):
                col_node.parm(f"doexclusions{idx}").set(0)
                col_node.parm(f"excludepattern{idx}").set("")

    col_node.moveToGoodPosition()
    return col_node


def create_chained_collection_nodes(
    parent_subnet: hou.Node,
    material_names: List[str],
    input_node: Optional[hou.Node] = None,
    output_node: Optional[hou.Node] = None,
    default_prim_path: Optional[str] = None,
    use_prefix_anchor: bool = False,
    asset_prefix: str = "",
    auto_exclude: bool = True
) -> List[hou.LopNode]:
    """
    Creates individual 'collection::2.0' LOP nodes chained sequentially inside a Subnet LOP.
    Preserves factory default primitive path for seamless %<collection> querying.
    """
    sorted_mats = sorted(material_names)
    exclusions_map = compute_exclusion_rules(sorted_mats) if auto_exclude else {}

    created_nodes = []
    prev_node = input_node

    for mat_name in sorted_mats:
        node_name = f"col_{mat_name}"
        node = parent_subnet.node(node_name) or parent_subnet.createNode("collection::2.0", node_name)

        node.parm("createprimitive").set(1)
        if default_prim_path is not None:
            node.parm("defaultprimpath").set(default_prim_path)
        else:
            node.parm("defaultprimpath").revertToDefaults()

        node.parm("collectioncount").set(1)
        node.parm("enable1").set(1)
        node.parm("collectionname1").set(mat_name)
        node.parm("expansionrule1").set("explicitOnly")

        if node.parm("primpath1"):
            node.parm("primpath1").revertToDefaults()

        if use_prefix_anchor and asset_prefix:
            pattern = f"**/*{asset_prefix}{mat_name}"
        else:
            pattern = f"**/*{mat_name}"

        node.parm("includepattern1").set(pattern)

        if mat_name in exclusions_map and not use_prefix_anchor:
            node.parm("doexclusions1").set(1)
            excl_str = " ".join([f"**/*{m}" for m in exclusions_map[mat_name]])
            node.parm("excludepattern1").set(excl_str)
        else:
            node.parm("doexclusions1").set(0)
            node.parm("excludepattern1").set("")

        if prev_node:
            node.setInput(0, prev_node)
        prev_node = node
        created_nodes.append(node)

    if output_node and created_nodes:
        output_node.setInput(0, created_nodes[-1])

    parent_subnet.layoutChildren()
    return created_nodes


def populate_material_library(
    mat_lib_node: hou.LopNode,
    material_names: List[str],
    collection_prefix: str = "%",
    enable_assign: bool = True
) -> None:
    """
    Populates assignment rows in a Material Library LOP.
    Automatically configures geopath{i} to '%<material_name>' to bind shaders to collections.
    """
    if mat_lib_node is None:
        raise ValueError("Material library node not found.")

    sorted_mats = sorted(material_names)
    count = len(sorted_mats)
    mat_lib_node.parm("materials").set(count)

    for idx, mat_name in enumerate(sorted_mats, start=1):
        if mat_lib_node.parm(f"matnode{idx}"):
            mat_lib_node.parm(f"matnode{idx}").set(mat_name)
        if mat_lib_node.parm(f"matpath{idx}"):
            mat_lib_node.parm(f"matpath{idx}").set(mat_name)
        if mat_lib_node.parm(f"assign{idx}") and enable_assign:
            mat_lib_node.parm(f"assign{idx}").set(1)
        if mat_lib_node.parm(f"geopath{idx}"):
            mat_lib_node.parm(f"geopath{idx}").set(f"{collection_prefix}{mat_name}")

    print(f"[Material Library] Successfully populated {count} assignment rows in {mat_lib_node.path()}")


def run(
    source_node: Optional[hou.LopNode] = None,
    mode: str = "single",
    target_node_name: str = "MaterialsCollections",
    target_subnet: Optional[hou.Node] = None,
    auto_populate_matlibrary: bool = False,
    mat_lib_node: Optional[hou.LopNode] = None
) -> Optional[hou.Node]:
    """
    Main execution entry point.

    Args:
        source_node: Source LOP node. If None, uses active selection or '/stage/transform1'.
        mode: 'single' (1 collection node with all multi-parms, recommended)
              or 'chained' (creates sequential nodes inside a subnet).
        target_node_name: Name for the created Collection LOP (mode='single').
        target_subnet: Target subnet for chained mode (defaults to '/stage/subnet1').
        auto_populate_matlibrary: Whether to configure assignment rows in Material Library LOP.
        mat_lib_node: Material Library LOP node (defaults to '/stage/materiallibrary1').

    Returns:
        The created or updated collection node / subnet.
    """
    # 1. Resolve source node
    if source_node is None:
        selected = hou.selectedNodes()
        if selected and isinstance(selected[0], hou.LopNode):
            source_node = selected[0]
        else:
            source_node = hou.node("/stage/transform1") or hou.node("/stage/sublayer1")

    if not source_node:
        raise RuntimeError("No suitable source LOP node found. Please select a node in /stage.")

    # 2. Extract materials
    materials_dict = extract_material_names(source_node)
    material_names = list(materials_dict.keys())

    if not material_names:
        print("[USD Collections] Error: No materials extracted. Aborting.")
        return None

    parent = source_node.parent()

    # 3. Create collections
    if mode == "single":
        col_node = create_single_collection_node(
            parent_node=parent,
            node_name=target_node_name,
            material_names=material_names,
            input_node=source_node
        )
        print(f"[USD Collections] Created Collection LOP with {len(material_names)} collections: {col_node.path()}")

        # Wire downstream material library if connected
        mat_lib = mat_lib_node or hou.node("/stage/materiallibrary1")
        if mat_lib and mat_lib.inputs() and mat_lib.inputs()[0] == source_node:
            mat_lib.setInput(0, col_node)
            print(f"[USD Collections] Re-wired downstream: {col_node.path()} -> {mat_lib.path()}")

        result_node = col_node

    elif mode == "chained":
        subnet = target_subnet or hou.node("/stage/subnet1")
        if not subnet:
            subnet = parent.createNode("subnet", "MaterialsSubnet")
            subnet.setInput(0, source_node)

        sub_inputs = [n for n in subnet.children() if n.type().name() == "subinput"]
        sub_input = sub_inputs[0] if sub_inputs else None
        output0 = subnet.node("output0") or subnet.createNode("output", "output0")

        nodes = create_chained_collection_nodes(
            parent_subnet=subnet,
            material_names=material_names,
            input_node=sub_input or source_node,
            output_node=output0
        )
        print(f"[USD Collections] Created {len(nodes)} collection nodes chained inside {subnet.path()}")
        result_node = subnet
    else:
        raise ValueError(f"Unknown mode: {mode}. Choose 'single' or 'chained'.")

    # 4. Optional Material Library auto-population
    if auto_populate_matlibrary:
        mat_lib = mat_lib_node or hou.node("/stage/materiallibrary1")
        if mat_lib:
            populate_material_library(mat_lib, material_names)

    return result_node


if __name__ == "__main__":
    run(mode="single")
