import collections
import json
import sqlite3
import struct

from osgeo import ogr, osr


def ident(identifier):
    """ Sqlite identifier replacement """
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def param_str(value):
    """
    Sqlite parameter string replacement.

    Generally don't use this. Needed for creating triggers/etc though.
    """
    if value is None:
        return "NULL"
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def db(path, **kwargs):
    db = sqlite3.connect(path, **kwargs)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON;")
    db.enable_load_extension(True)
    db.execute("SELECT load_extension('mod_spatialite');")
    return db


def get_meta_info(db, layer, repo_version='0.0.1'):
    yield ("version", json.dumps({"version": repo_version}))

    dbcur = db.cursor()
    table = layer

    QUERIES = {
        "gpkg_contents": (
            # we ignore dynamic fields (last-change, min_x, min_y, max_x, max_y)
            f"SELECT table_name, data_type, identifier, description, srs_id FROM gpkg_contents WHERE table_name=?;",
            (table,),
            dict,
        ),
        "gpkg_geometry_columns": (
            f"SELECT table_name, column_name, geometry_type_name, srs_id, z, m FROM gpkg_geometry_columns WHERE table_name=?;",
            (table,),
            dict,
        ),
        "sqlite_table_info": (f"PRAGMA table_info({ident(table)});", (), list),
        "gpkg_metadata_reference": (
            """
            SELECT MR.*
            FROM gpkg_metadata_reference MR
                INNER JOIN gpkg_metadata M ON (MR.md_file_id = M.id)
            WHERE
                MR.table_name=?
                AND MR.column_name IS NULL
                AND MR.row_id_value IS NULL;
            """,
            (table,),
            list,
        ),
        "gpkg_metadata": (
            """
            SELECT M.*
            FROM gpkg_metadata_reference MR
                INNER JOIN gpkg_metadata M ON (MR.md_file_id = M.id)
            WHERE
                MR.table_name=?
                AND MR.column_name IS NULL
                AND MR.row_id_value IS NULL;
            """,
            (table,),
            list,
        ),
        "gpkg_spatial_ref_sys": (
            """
            SELECT DISTINCT SRS.*
            FROM gpkg_spatial_ref_sys SRS
                LEFT OUTER JOIN gpkg_contents C ON (C.srs_id = SRS.srs_id)
                LEFT OUTER JOIN gpkg_geometry_columns G ON (G.srs_id = SRS.srs_id)
            WHERE
                (C.table_name=? OR G.table_name=?)
            """,
            (table, table),
            list,
        ),
    }
    try:
        for filename, (sql, params, rtype) in QUERIES.items():
            # check table exists, the metadata ones may not
            if not filename.startswith("sqlite_"):
                dbcur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
                    (filename,),
                )
                if not dbcur.fetchone():
                    continue

            dbcur.execute(sql, params)
            value = [
                collections.OrderedDict(sorted(zip(row.keys(), row))) for row in dbcur
            ]
            if rtype is dict:
                value = value[0] if len(value) else None
            yield (filename, json.dumps(value))
    except Exception:
        print(f"Error building meta/{filename}")
        raise


def pk(db, table):
    """ Find the primary key for a GeoPackage table """

    # Requirement 150:
    # A feature table or view SHALL have a column that uniquely identifies the
    # row. For a feature table, the column SHOULD be a primary key. If there
    # is no primary key column, the first column SHALL be of type INTEGER and
    # SHALL contain unique values for each row.

    q = db.execute(f"PRAGMA table_info({ident(table)});")
    fields = []
    for field in q:
        if field["pk"]:
            return field["name"]
        fields.append(field)

    if fields[0]["type"] == "INTEGER":
        return fields[0]["name"]
    else:
        raise ValueError("No valid GeoPackage primary key field found")


def geom_cols(db, table):
    q = db.execute("""
            SELECT column_name
            FROM gpkg_geometry_columns
            WHERE table_name=?
            ORDER BY column_name;
        """, (table,)
    )
    return tuple(r[0] for r in q.fetchall())


def geom_to_ogr(gpkg_geom, parse_srs=False):
    """
    Parse GeoPackage geometry values to an OGR Geometry object
    http://www.geopackage.org/spec/#gpb_format
    """
    if gpkg_geom is None:
        return None

    if not isinstance(gpkg_geom, bytes):
        raise TypeError("Expected bytes")

    if gpkg_geom[0:2] != b"GP":  # 0x4750
        raise ValueError("Expected GeoPackage Binary Geometry")
    (version, flags) = struct.unpack_from("BB", gpkg_geom, 2)
    if version != 0:
        raise NotImplementedError("Expected GeoPackage v1 geometry, got %d", version)

    is_le = (flags & 0b0000001) != 0  # Endian-ness

    if flags & (0b00100000):  # GeoPackageBinary type
        raise NotImplementedError("ExtendedGeoPackageBinary")

    envelope_typ = (flags & 0b000001110) >> 1
    wkb_offset = 8
    if envelope_typ == 1:
        wkb_offset += 32
    elif envelope_typ in (2, 3):
        wkb_offset += 48
    elif envelope_typ == 4:
        wkb_offset += 64
    elif envelope_typ > 4:
        wkb_offset += 32
    else:  # 0
        pass

    geom = ogr.CreateGeometryFromWkb(gpkg_geom[wkb_offset:])

    if parse_srs:
        srid = struct.unpack_from(f"{'<' if is_le else '>'}i", gpkg_geom, 4)[0]
        if srid > 0:
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(srid)
            geom.AssignSpatialReference(srs)

    return geom
