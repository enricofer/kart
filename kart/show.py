import contextlib
from datetime import datetime, timezone, timedelta

import click

from .crs_util import CoordinateReferenceString
from .log import commit_obj_to_json
from .output_util import dump_json_output, resolve_output_path
from .timestamps import datetime_to_iso8601_utc, timedelta_to_iso8601_tz
from . import diff


@click.command()
@click.pass_context
@click.option(
    "--output-format",
    "-o",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format",
)
@click.option(
    "--crs",
    type=CoordinateReferenceString(encoding="utf-8"),
    help="Reproject geometries into the given coordinate reference system. Accepts: 'EPSG:<code>'; proj text; OGC WKT; OGC URN; PROJJSON.)",
)
@click.option(
    "--json-style",
    type=click.Choice(["extracompact", "compact", "pretty"]),
    default="pretty",
    help="How to format the output. Only used with --output-format=json",
)
@click.argument("refish", default="HEAD", required=False)
def show(ctx, *, refish, output_format, crs, json_style, **kwargs):
    """
    Show the given commit, or HEAD
    """
    show_writer = globals()[f"show_output_{output_format}"]
    return diff.diff_with_writer(
        ctx,
        show_writer,
        exit_code=False,
        target_crs=crs,
        commit_spec=f"{refish}^?...{refish}",
        filters=[],
        json_style=json_style,
    )


@click.command(name="create-patch")
@click.pass_context
@click.option(
    "--json-style",
    type=click.Choice(["extracompact", "compact", "pretty"]),
    default="pretty",
    help="How to format the output",
)
# NOTE: this is *required* for now.
# A future version might create patches from working-copy changes.
@click.argument("refish")
def create_patch(ctx, *, refish, json_style, **kwargs):
    """
    Creates a JSON patch from the given ref.
    The patch can be applied with `kart apply`.
    """
    return diff.diff_with_writer(
        ctx,
        patch_output,
        exit_code=False,
        commit_spec=f"{refish}^?...{refish}",
        filters=[],
        json_style=json_style,
    )


@contextlib.contextmanager
def show_output_text(*, target, output_path, **kwargs):
    """
    Contextmanager.

    Arguments:
        target: a RepoStructure instance for the commit to show a patch for
        output_path:   where the output should go; a path, file-like object or '-'

    All other kwargs are passed to kart.diff.diff_output_text.

    Yields a callable which can be called with dataset diffs.
    The callable takes two arguments:
        dataset: A kart.base_dataset.BaseDataset instance representing
                 either the old or new version of the dataset.
        diff:    The kart.diff.Diff instance to serialize

    On exit, writes a human-readable patch as text to the given output file.

    This patch may not be apply-able; it is intended for human readability.
    In particular, geometry WKT is abbreviated and null values are represented
    by a unicode "␀" character.
    """
    commit = target.commit
    fp = resolve_output_path(output_path)
    pecho = {"file": fp, "color": fp.isatty()}
    with diff.diff_output_text(output_path=fp, **kwargs) as diff_writer:
        author = commit.author
        author_time_utc = datetime.fromtimestamp(author.time, timezone.utc)
        author_timezone = timezone(timedelta(minutes=author.offset))
        author_time_in_author_timezone = author_time_utc.astimezone(author_timezone)

        click.secho(f"commit {commit.hex}", fg="yellow")
        click.secho(f"Author: {author.name} <{author.email}>", **pecho)
        click.secho(
            f'Date:   {author_time_in_author_timezone.strftime("%c %z")}', **pecho
        )
        click.secho(**pecho)
        for line in commit.message.splitlines():
            click.secho(f"    {line}", **pecho)
        click.secho(**pecho)
        yield diff_writer


@contextlib.contextmanager
def show_output_json(*, target, output_path, json_style, **kwargs):
    """
    Contextmanager.

    Same arguments and usage as `show_output_text`; see that docstring for usage.

    On exit, writes the output as JSON to the given output file.
    If the output file is stdout and isn't piped anywhere,
    the json is prettified first.

    The patch JSON contains two top-level keys:
        "kart.diff/v1+hexwkb": contains a JSON diff. See `kart.diff.diff_output_json` docstring.
        "kart.show/v1": contains metadata about the commit:
          {
            "authorEmail": "joe@example.com",
            "authorName": "Joe Bloggs",
            "authorTime": "2020-04-15T01:19:16Z",
            "authorTimeOffset": "+12:00",
            "message": "Commit title\n\nThis commit makes some changes\n"
          }

    authorTime is always returned in UTC, in Z-suffixed ISO8601 format.
    """

    commit = target.commit

    def dump_function(data, *args, **kwargs):
        data["kart.show/v1"] = commit_obj_to_json(commit)
        dump_json_output(data, *args, **kwargs)

    with diff.diff_output_json(
        output_path=output_path,
        json_style=json_style,
        dump_function=dump_function,
        **kwargs,
    ) as diff_writer:
        yield diff_writer


@contextlib.contextmanager
def patch_output(*, target, output_path, json_style, **kwargs):
    """
    Almost the same as show_output_json but uses the `kart.patch/v1` key instead of `kart.show/v1`

    This is duplicated for clarity, because all this diff callback stuff is complex enough.
    """

    commit = target.commit
    author = commit.author
    author_time = datetime.fromtimestamp(author.time, timezone.utc)
    author_time_offset = timedelta(minutes=author.offset)

    def dump_function(data, *args, **kwargs):
        data["kart.patch/v1"] = {
            "authorName": author.name,
            "authorEmail": author.email,
            "authorTime": datetime_to_iso8601_utc(author_time),
            "authorTimeOffset": timedelta_to_iso8601_tz(author_time_offset),
            "message": commit.message,
        }
        dump_json_output(data, *args, **kwargs)

    with diff.diff_output_json(
        output_path=output_path,
        json_style=json_style,
        dump_function=dump_function,
        **kwargs,
    ) as diff_writer:
        yield diff_writer
