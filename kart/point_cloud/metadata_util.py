from enum import IntFlag
import logging
import json
from pathlib import Path
import re
from subprocess import CalledProcessError

from osgeo import osr

from kart.crs_util import normalise_wkt
from kart.exceptions import (
    InvalidOperation,
    INVALID_FILE_FORMAT,
)
from kart.list_of_conflicts import ListOfConflicts
from kart.lfs_util import get_hash_and_size_of_file
from kart.geometry import ring_as_wkt
from kart.point_cloud import pdal_execute_pipeline
from kart.point_cloud.schema_util import (
    get_schema_from_pdrf,
    get_record_length_from_pdrf,
    equivalent_copc_pdrf,
    pdal_schema_to_kart_schema,
)


L = logging.getLogger(__name__)


class RewriteMetadata(IntFlag):
    """Different ways to interpret metadata depending on the type of import."""

    NO_REWRITE = 0x0

    # We're about to convert this file to COPC - update the metadata to be as if we'd already done this.
    # This affects both the format and the schema - only certain PDRFs are allowed in COPC, which constrains the schema.
    AS_IF_CONVERTED_TO_COPC = 0x1

    # Drop all the optimization info from the format info - we don't need to verify it or store it.
    # (ie, because we don't care about whether tiles are optimized, or, we're about to change the tile's optimization anyway.)
    DROP_OPTIMIZATION = 0x2

    # Drop all the format info - we don't need to verify it or store it.
    # (ie, because we're about to convert the tile to a different format anyway).
    DROP_FORMAT = 0x4

    # Drop the schema info - we don't need to verify it or store it.
    # (ie, because we're about to convert the tile to have a different schema anyway).
    DROP_SCHEMA = 0x8


def rewrite_and_merge_metadata(
    tile_metadata_list, rewrite_metadata=RewriteMetadata.NO_REWRITE
):
    """
    Given a list of tile metadata, merges the parts we expect to be homogenous into a single piece of tile metadata in
    the same format that describes the whole list.

    Depending on how we are to import the tiles, some differences in the metadata may be allowed - to allow for this, we
    drop those parts of the metadata in accordance with the rewrite_metadata option. This means a) the merge will happen
    cleanly in spite of possible differences and b) we won't store any metadata that can't describe every tile in the
    dataset (ie, we won't store anything about whether tiles are COPC if we're going to allow a mix of both COPC and not).
    """
    result = {}
    for tile_metadata in tile_metadata_list:
        _merge_metadata_field(
            result, "format.json", rewrite_format(tile_metadata, rewrite_metadata)
        )
        _merge_metadata_field(
            result, "schema.json", rewrite_schema(tile_metadata, rewrite_metadata)
        )
        _merge_metadata_field(result, "crs.wkt", tile_metadata["crs.wkt"])
        # Don't copy anything from "tile" to the result - these fields are tile specific and needn't be merged.
    return result


def rewrite_format(tile_metadata, rewrite_metadata=RewriteMetadata.NO_REWRITE):
    orig_format = tile_metadata["format.json"]
    if RewriteMetadata.DROP_FORMAT in rewrite_metadata:
        return {}
    elif RewriteMetadata.DROP_OPTIMIZATION in rewrite_metadata:
        return {
            k: v for k, v in orig_format.items() if not k.startswith("optimization")
        }
    elif RewriteMetadata.AS_IF_CONVERTED_TO_COPC in rewrite_metadata:
        orig_pdrf = orig_format["pointDataRecordFormat"]
        new_pdrf = equivalent_copc_pdrf(orig_pdrf)
        return {
            "compression": "laz",
            "lasVersion": "1.4",
            "optimization": "copc",
            "optimizationVersion": "1.0",
            "pointDataRecordFormat": new_pdrf,
            "pointDataRecordLength": get_record_length_from_pdrf(new_pdrf),
        }
    else:
        return orig_format


def rewrite_schema(tile_metadata, rewrite_metadata=RewriteMetadata.NO_REWRITE):
    if RewriteMetadata.DROP_SCHEMA in rewrite_metadata:
        return {}

    orig_schema = tile_metadata["schema.json"]
    if RewriteMetadata.AS_IF_CONVERTED_TO_COPC in rewrite_metadata:
        orig_pdrf = tile_metadata["format.json"]["pointDataRecordFormat"]
        return get_schema_from_pdrf(equivalent_copc_pdrf(orig_pdrf))
    else:
        return orig_schema


def _merge_metadata_field(output, key, value):
    if key not in output:
        output[key] = value
        return
    existing_value = output[key]
    if isinstance(existing_value, ListOfConflicts):
        if value not in existing_value:
            existing_value.append(value)
    elif existing_value != value:
        output[key] = ListOfConflicts([existing_value, value])


def get_copc_version(info):
    if info.get("copc"):
        # PDAL now hides the COPC VLR from us so we can't do better than this without peeking at the file directly.
        # See https://github.com/PDAL/PDAL/blob/3e33800d85d48f726dcd0931cefe062c4af2b573/io/private/las/Vlr.cpp#L53
        return "1.0"
    else:
        return None


def get_native_extent(info):
    return (
        info["minx"],
        info["maxx"],
        info["miny"],
        info["maxy"],
        info["minz"],
        info["maxz"],
    )


def extract_pc_tile_metadata(pc_tile_path):
    """
    Use pdal to get any and all point-cloud metadata we can make use of in Kart.
    This includes metadata that must be dataset-homogenous and would be stored in the dataset's /meta/ folder,
    along with other metadata that is tile-specific and would be stored in the tile's pointer file.

    Output:
    {
        "format.json": - Information about file format, as stored at meta/format.json (or some subset thereof).
        "schema.json": - PDRF schema, as stored in meta/schema.json
        "crs.wkt":    - CRS as stored at meta/crs.wkt
        "tile":   - Tile-specific (non-homogenous) information, as stored in individual tile pointer files.
    }

    Although any two point cloud tiles can differ in any way imaginable, we specifically constrain tiles in the
    same dataset to be homogenous enough that the meta items format.json, schema.json and crs.wkt
    describe *all* of the tiles in that dataset. The "tile" field is where we keep all information
    that can be different for every tile in the dataset, which is why it must be stored in pointer files.
    """
    pipeline = [
        {
            "type": "readers.las",
            "filename": str(pc_tile_path),
            "count": 0,  # Don't read any individual points.
        },
        {"type": "filters.info"},
    ]

    try:
        metadata = pdal_execute_pipeline(pipeline)
    except CalledProcessError:
        raise InvalidOperation(
            f"Error reading {pc_tile_path}", exit_code=INVALID_FILE_FORMAT
        )

    info = metadata["readers.las"]

    native_extent = get_native_extent(info)
    compound_crs = info["srs"].get("compoundwkt")
    horizontal_crs = info["srs"].get("wkt")
    is_copc = info.get("copc") or False
    format_json = {
        "compression": "laz" if info["compressed"] else "las",
        "lasVersion": f"{info['major_version']}.{info['minor_version']}",
        "optimization": "copc" if is_copc else None,
        "optimizationVersion": get_copc_version(info) if is_copc else None,
        "pointDataRecordFormat": info["dataformat_id"],
        "pointDataRecordLength": info["point_length"],
    }

    schema_json = pdal_schema_to_kart_schema(metadata["filters.info"]["schema"])
    oid, size = get_hash_and_size_of_file(pc_tile_path)

    # Keep tile info keys in alphabetical order, except oid and size should be last.
    tile_info = {
        "name": Path(pc_tile_path).name,
        # Reprojection seems to work best if we give it only the horizontal CRS here:
        "crs84Extent": _calc_crs84_extent(
            native_extent, horizontal_crs or compound_crs
        ),
        "format": get_format_summary(format_json),
        "nativeExtent": _format_list_as_str(native_extent),
        "pointCount": info["count"],
        "oid": f"sha256:{oid}",
        "size": size,
    }

    result = {
        "format.json": format_json,
        "schema.json": schema_json,
        "crs.wkt": normalise_wkt(compound_crs or horizontal_crs),
        "tile": tile_info,
    }

    return result


def _format_list_as_str(array):
    """
    We treat a pointer file as a place to store JSON, but its really for storing string-string key-value pairs only.
    Some of our values are a lists of numbers - we turn them into strings by comma-separating them, and we don't
    put them inside square brackets as they would be in JSON.
    """
    return json.dumps(array, separators=(",", ":"))[1:-1]


def get_format_summary(format_info):
    """
    Given format info as stored in format.json, return a short string summary such as: laz-1.4/copc-1.0
    """
    if "format.json" in format_info:
        format_info = format_info["format.json"]

    format_summary = f"{format_info['compression']}-{format_info['lasVersion']}"
    if format_info["optimization"]:
        format_summary += (
            f"/{format_info['optimization']}-{format_info['optimizationVersion']}"
        )
    return format_summary


def _calc_crs84_extent(src_extent, src_crs):
    """
    Given a 3D extent with a particular CRS, return a CRS84 extent that surrounds that extent.
    """
    src_srs = osr.SpatialReference()
    src_srs.ImportFromWkt(src_crs)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    dest_srs = osr.SpatialReference()
    dest_srs.SetWellKnownGeogCS("CRS84")

    transform = osr.CoordinateTransformation(src_srs, dest_srs)
    min_x, max_x, min_y, max_y, min_z, max_z = src_extent
    result = transform.TransformPoints(
        [
            (min_x, min_y),
            (min_x, max_y),
            (max_x, max_y),
            (max_x, min_y),
        ]
    )
    return "POLYGON(" + ring_as_wkt(*result, dp=7) + ")"


def is_copc(tile_format):
    tile_format = extract_format(tile_format)
    if isinstance(tile_format, dict):
        return tile_format.get("optimization") == "copc"
    elif isinstance(tile_format, str):
        return "copc" in tile_format
    raise ValueError("Bad tile format")


def get_las_version(tile_format):
    tile_format = extract_format(tile_format)
    if isinstance(tile_format, dict):
        return tile_format.get("lasVersion")
    elif isinstance(tile_format, str):
        match = re.match(r"la[sz]-([0-9\.]+)", tile_format, re.IGNORECASE)
        if match:
            return match.group(1)
    raise ValueError("Bad tile format")


def extract_format(tile_format):
    if isinstance(tile_format, dict):
        if "format.json" in tile_format:
            return tile_format["format.json"]
        if "format" in tile_format:
            return tile_format["format"]
    return tile_format
