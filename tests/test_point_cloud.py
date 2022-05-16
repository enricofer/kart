from glob import glob
import json
import re
import shutil
import subprocess
import pytest

from kart.exceptions import INVALID_FILE_FORMAT
from kart.repo import KartRepo

DUMMY_REPO = "git@example.com/example.git"

# using a fixture instead of a skipif decorator means we get one aggregated skip
# message rather than one per test
@pytest.fixture(scope="session")
def requires_pdal():
    has_pdal = False
    try:
        import pdal

        assert pdal.Pipeline
        has_pdal = True
    except ModuleNotFoundError:
        pass

    pytest.helpers.feature_assert_or_skip(
        "pdal package installed", "KART_EXPECT_PDAL", has_pdal, ci_require=False
    )


@pytest.fixture(scope="session")
def requires_git_lfs():
    r = subprocess.run(["git", "lfs", "--version"])
    has_git_lfs = r.returncode == 0

    pytest.helpers.feature_assert_or_skip(
        "Git LFS installed", "KART_EXPECT_GIT_LFS", has_git_lfs, ci_require=False
    )


def test_import_single_las(
    tmp_path, chdir, cli_runner, data_archive_readonly, requires_pdal, requires_git_lfs
):
    with data_archive_readonly("point-cloud/las-autzen.tgz") as autzen:
        repo_path = tmp_path / "point-cloud-repo"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0, r.stderr

        repo = KartRepo(repo_path)
        with chdir(repo_path):
            r = cli_runner.invoke(
                ["point-cloud-import", f"{autzen}/autzen.las", "--dataset-path=autzen"]
            )
            assert r.exit_code == 0, r.stderr

            r = cli_runner.invoke(["data", "ls"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines() == ["autzen"]

            r = cli_runner.invoke(["meta", "get", "autzen", "schema.json", "-ojson"])
            assert r.exit_code == 0, r.stderr
            assert json.loads(r.stdout) == {
                "autzen": {
                    "schema.json": {
                        "dimensions": [
                            {"name": "X", "size": 8, "type": "floating"},
                            {"name": "Y", "size": 8, "type": "floating"},
                            {"name": "Z", "size": 8, "type": "floating"},
                            {"name": "Intensity", "size": 2, "type": "unsigned"},
                            {"name": "ReturnNumber", "size": 1, "type": "unsigned"},
                            {"name": "NumberOfReturns", "size": 1, "type": "unsigned"},
                            {
                                "name": "ScanDirectionFlag",
                                "size": 1,
                                "type": "unsigned",
                            },
                            {"name": "EdgeOfFlightLine", "size": 1, "type": "unsigned"},
                            {"name": "Classification", "size": 1, "type": "unsigned"},
                            {"name": "ScanAngleRank", "size": 4, "type": "floating"},
                            {"name": "UserData", "size": 1, "type": "unsigned"},
                            {"name": "PointSourceId", "size": 2, "type": "unsigned"},
                            {"name": "GpsTime", "size": 8, "type": "floating"},
                        ],
                    }
                }
            }

            r = cli_runner.invoke(["show", "HEAD", "autzen:tile:autzen.copc.laz"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines()[4:] == [
                '    Importing 1 LAZ tiles as autzen',
                '',
                '+++ autzen:tile:autzen.copc.laz',
                '+                                     name = autzen.copc.laz',
                '+                             extent.crs84 = 6356.163100000001,6388.646,8489.777900000001,8533.6237,4.0735,5.3684',
                '+                            extent.native = 6356.163100000001,6388.646,8489.777900000001,8533.6237,4.0735,5.3684',
                '+                                   format = pc:v1/copc-1.0',
                '+                             points.count = 106',
                '+                                      oid = sha256:213ef4211ba375e2eec60aa61b6c230d1a3d1498b8fcc39150fd3040ee8f0512',
                '+                                     size = 3607',
            ]

            r = cli_runner.invoke(["remote", "add", "origin", DUMMY_REPO])
            assert r.exit_code == 0, r.stderr
            repo.config[f"lfs.{DUMMY_REPO}/info/lfs.locksverify"] = False

            stdout = subprocess.check_output(
                ["kart", "lfs", "push", "origin", "--all", "--dry-run"], encoding="utf8"
            )
            assert re.match(
                r"push [0-9a-f]{64} => autzen/.point-cloud-dataset.v1/tile/e8/autzen.copc.laz",
                stdout.splitlines()[0],
            )

            assert (repo_path / "autzen" / "tiles" / "autzen.copc.laz").is_file()


@pytest.mark.slow
def test_import_several_las(
    tmp_path, chdir, cli_runner, data_archive_readonly, requires_pdal, requires_git_lfs
):
    # Using postgres here because it has the best type preservation
    with data_archive_readonly("point-cloud/laz-auckland.tgz") as auckland:
        repo_path = tmp_path / "point-cloud-repo"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0

        repo = KartRepo(repo_path)
        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "point-cloud-import",
                    *glob(f"{auckland}/auckland_*.laz"),
                    "--dataset-path=auckland",
                ]
            )
            assert r.exit_code == 0, r.stderr

            r = cli_runner.invoke(["data", "ls"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines() == ["auckland"]

            r = cli_runner.invoke(["meta", "get", "auckland", "schema.json", "-ojson"])
            assert r.exit_code == 0, r.stderr
            assert json.loads(r.stdout) == {
                "auckland": {
                    "schema.json": {
                        "dimensions": [
                            {"name": "X", "size": 8, "type": "floating"},
                            {"name": "Y", "size": 8, "type": "floating"},
                            {"name": "Z", "size": 8, "type": "floating"},
                            {"name": "Intensity", "size": 2, "type": "unsigned"},
                            {"name": "ReturnNumber", "size": 1, "type": "unsigned"},
                            {"name": "NumberOfReturns", "size": 1, "type": "unsigned"},
                            {
                                "name": "ScanDirectionFlag",
                                "size": 1,
                                "type": "unsigned",
                            },
                            {"name": "EdgeOfFlightLine", "size": 1, "type": "unsigned"},
                            {"name": "Classification", "size": 1, "type": "unsigned"},
                            {"name": "ScanAngleRank", "size": 4, "type": "floating"},
                            {"name": "UserData", "size": 1, "type": "unsigned"},
                            {"name": "PointSourceId", "size": 2, "type": "unsigned"},
                            {"name": "GpsTime", "size": 8, "type": "floating"},
                            {"name": "Red", "size": 2, "type": "unsigned"},
                            {"name": "Green", "size": 2, "type": "unsigned"},
                            {"name": "Blue", "size": 2, "type": "unsigned"},
                        ],
                    }
                }
            }

            r = cli_runner.invoke(["remote", "add", "origin", DUMMY_REPO])
            assert r.exit_code == 0, r.stderr
            repo.config[f"lfs.{DUMMY_REPO}/info/lfs.locksverify"] = False

            stdout = subprocess.check_output(
                ["kart", "lfs", "push", "origin", "--all", "--dry-run"], encoding="utf8"
            )
            lines = stdout.splitlines()
            for i in range(16):
                assert re.match(
                    r"push [0-9a-f]{64} => auckland/.point-cloud-dataset.v1/tile/[0-9a-f]{2}/auckland_\d_\d.copc.laz",
                    lines[i],
                )

            for x in range(4):
                for y in range(4):
                    assert (
                        repo_path / "auckland" / "tiles" / f"auckland_{x}_{y}.copc.laz"
                    ).is_file()


def test_import_no_convert(
    tmp_path, chdir, cli_runner, data_archive_readonly, requires_pdal, requires_git_lfs
):
    with data_archive_readonly("point-cloud/laz-auckland.tgz") as auckland:
        repo_path = tmp_path / "point-cloud-repo"
        r = cli_runner.invoke(["init", repo_path])
        assert r.exit_code == 0

        with chdir(repo_path):
            r = cli_runner.invoke(
                [
                    "point-cloud-import",
                    *glob(f"{auckland}/auckland_0_0.laz"),
                    "--dataset-path=auckland",
                    "--no-convert-to-copc",
                ]
            )
            assert r.exit_code == 0, r.stderr

            r = cli_runner.invoke(["show", "HEAD", "auckland:tile:auckland_0_0.laz"])
            assert r.exit_code == 0, r.stderr
            assert r.stdout.splitlines()[4:] == [
                '    Importing 1 LAZ tiles as auckland',
                '',
                '+++ auckland:tile:auckland_0_0.laz',
                '+                                     name = auckland_0_0.laz',
                '+                             extent.crs84 = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
                '+                            extent.native = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
                '+                                   format = pc:v1/laz-1.2',
                '+                             points.count = 4231',
                '+                                      oid = sha256:6b980ce4d7f4978afd3b01e39670e2071a792fba441aca45be69be81cb48b08c',
                '+                                     size = 51489',
            ]


def test_import_mismatched_las(
    tmp_path, chdir, cli_runner, data_archive_readonly, requires_pdal, requires_git_lfs
):
    # Using postgres here because it has the best type preservation
    with data_archive_readonly("point-cloud/laz-auckland.tgz") as auckland:
        with data_archive_readonly("point-cloud/las-autzen.tgz") as autzen:
            repo_path = tmp_path / "point-cloud-repo"
            r = cli_runner.invoke(["init", repo_path])
            assert r.exit_code == 0, r.stderr
            with chdir(repo_path):
                r = cli_runner.invoke(
                    [
                        "point-cloud-import",
                        *glob(f"{auckland}/auckland_*.laz"),
                        f"{autzen}/autzen.las",
                        "--dataset-path=mixed",
                    ]
                )
                assert r.exit_code == INVALID_FILE_FORMAT
                assert "Non-homogenous" in r.stderr


def test_working_copy_edit(cli_runner, data_working_copy, monkeypatch, requires_pdal):
    monkeypatch.setenv("X_KART_POINT_CLOUDS", "1")

    # TODO - remove Kart's requirement for a GPKG working copy
    with data_working_copy("point-cloud/auckland.tgz") as (repo_path, wc_path):
        r = cli_runner.invoke(["diff"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == []

        tiles_path = repo_path / "auckland" / "tiles"
        assert tiles_path.is_dir()

        shutil.copy(
            tiles_path / "auckland_0_0.copc.laz", tiles_path / "auckland_1_1.copc.laz"
        )
        # TODO - add rename detection.
        (tiles_path / "auckland_3_3.copc.laz").rename(
            tiles_path / "auckland_4_4.copc.laz"
        )

        r = cli_runner.invoke(["status"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == [
            "On branch main",
            "",
            "Changes in working copy:",
            '  (use "kart commit" to commit)',
            '  (use "kart restore" to discard changes)',
            "",
            "  auckland:",
            "    tile:",
            "      1 inserts",
            "      1 updates",
            "      1 deletes",
        ]

        r = cli_runner.invoke(["diff"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == [
            '--- auckland:tile:auckland_1_1.copc.laz',
            '+++ auckland:tile:auckland_1_1.copc.laz',
            '-                             extent.crs84 = 17559.8903,17569.8713,59212.2062,59222.1949,-0.0148,0.3515',
            '+                             extent.crs84 = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
            '-                            extent.native = 17559.8903,17569.8713,59212.2062,59222.1949,-0.0148,0.3515',
            '+                            extent.native = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
            '-                             points.count = 1558',
            '+                             points.count = 4231',
            '-                                      oid = sha256:c00ad390503389ceebef26ff0a29f98842c82773f998b3b2efde2369584c1f9d',
            '+                                      oid = sha256:c667eeb6603f22fd36c7be97f672c9c940eb23b2c924701d898501cf8db8abf4',
            '-                                     size = 24505',
            '+                                     size = 69559',
            '--- auckland:tile:auckland_3_3.copc.laz',
            '-                                     name = auckland_3_3.copc.laz',
            '-                             extent.crs84 = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '-                            extent.native = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '-                                   format = pc:v1/copc-1.0',
            '-                             points.count = 29',
            '-                                      oid = sha256:64895828ea03ce9cafaef4f387338aab8d498c8eccaef1503b8b3bd97e57c5a3',
            '-                                     size = 2319',
            '+++ auckland:tile:auckland_4_4.copc.laz',
            '+                                     name = auckland_4_4.copc.laz',
            '+                             extent.crs84 = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '+                            extent.native = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '+                                   format = pc:v1/copc-1.0',
            '+                             points.count = 29',
            '+                                      oid = sha256:64895828ea03ce9cafaef4f387338aab8d498c8eccaef1503b8b3bd97e57c5a3',
            '+                                     size = 2319',
        ]

        r = cli_runner.invoke(["commit", "-m", "Edit point cloud tiles"])
        assert r.exit_code == 0, r.stderr

        r = cli_runner.invoke(["show"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines()[4:] == [
            '    Edit point cloud tiles',
            '',
            '--- auckland:tile:auckland_1_1.copc.laz',
            '+++ auckland:tile:auckland_1_1.copc.laz',
            '-                             extent.crs84 = 17559.8903,17569.8713,59212.2062,59222.1949,-0.0148,0.3515',
            '+                             extent.crs84 = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
            '-                            extent.native = 17559.8903,17569.8713,59212.2062,59222.1949,-0.0148,0.3515',
            '+                            extent.native = 17549.878500000003,17559.8777,59202.1976,59212.1964,-0.0166,0.9983',
            '-                             points.count = 1558',
            '+                             points.count = 4231',
            '-                                      oid = sha256:c00ad390503389ceebef26ff0a29f98842c82773f998b3b2efde2369584c1f9d',
            '+                                      oid = sha256:c667eeb6603f22fd36c7be97f672c9c940eb23b2c924701d898501cf8db8abf4',
            '-                                     size = 24505',
            '+                                     size = 69559',
            '--- auckland:tile:auckland_3_3.copc.laz',
            '-                                     name = auckland_3_3.copc.laz',
            '-                             extent.crs84 = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '-                            extent.native = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '-                                   format = pc:v1/copc-1.0',
            '-                             points.count = 29',
            '-                                      oid = sha256:64895828ea03ce9cafaef4f387338aab8d498c8eccaef1503b8b3bd97e57c5a3',
            '-                                     size = 2319',
            '+++ auckland:tile:auckland_4_4.copc.laz',
            '+                                     name = auckland_4_4.copc.laz',
            '+                             extent.crs84 = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '+                            extent.native = 17580.9346,17589.2534,59232.198,59232.2938,-0.0128,0.098',
            '+                                   format = pc:v1/copc-1.0',
            '+                             points.count = 29',
            '+                                      oid = sha256:64895828ea03ce9cafaef4f387338aab8d498c8eccaef1503b8b3bd97e57c5a3',
            '+                                     size = 2319',
        ]

        r = cli_runner.invoke(["diff"])
        assert r.exit_code == 0, r.stderr
        assert r.stdout.splitlines() == []
