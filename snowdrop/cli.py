#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

import click
import pygit2


from .core import ogr
from . import core, gpkg
from . import checkout, commit, diff, init, fsck


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return

    import osgeo
    import pkg_resources  # part of setuptools
    version = pkg_resources.require("snowdrop")[0].version

    click.echo(f"Project Snowdrop v{version}")
    click.echo(f"GDAL v{osgeo._gdal.__version__}")
    click.echo(f"PyGit2 v{pygit2.__version__}; Libgit2 v{pygit2.LIBGIT2_VERSION}")
    ctx.exit()


@click.group()
@click.option(
    "repo_dir",
    "--repo",
    type=click.Path(file_okay=False, dir_okay=True),
    default=os.curdir,
    metavar="PATH",
)
@click.option(
    "--version",
    is_flag=True,
    callback=print_version,
    expose_value=False,
    is_eager=True,
    help="Show version information and exit.",
)
@click.pass_context
def cli(ctx, repo_dir):
    ctx.ensure_object(dict)
    ctx.obj["repo_dir"] = repo_dir


def _execvp(file, args):
    if "_SNOWDROP_NO_EXEC" in os.environ:
        # used in testing. This is pretty hackzy
        p = subprocess.run([file] + args[1:], capture_output=True, encoding="utf-8")
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        sys.exit(p.returncode)
    else:
        os.execvp(file, args)


def _pc(count):
    """ Simple pluraliser for commit/commits """
    if count == 1:
        return "commit"
    else:
        return "commits"


def _pf(count):
    """ Simple pluraliser for feature/features """
    if count == 1:
        return "feature"
    else:
        return "features"


# commands from modules
cli.add_command(checkout.checkout)
cli.add_command(commit.commit)
cli.add_command(diff.diff)
cli.add_command(fsck.fsck)
cli.add_command(init.import_gpkg)


OFTMap = {
    "INTEGER": ogr.OFTInteger,
    "MEDIUMINT": ogr.OFTInteger,
    "TEXT": ogr.OFTString,
    "REAL": ogr.OFTReal,
}


@cli.command()
@click.option(
    "--ff/--no-ff",
    default=True,
    help=(
        "When the merge resolves as a fast-forward, only update the branch pointer, without creating a merge commit. "
        "With --no-ff create a merge commit even when the merge resolves as a fast-forward."
    ),
)
@click.option(
    "--ff-only",
    default=False,
    is_flag=True,
    help=(
        "Refuse to merge and exit with a non-zero status unless the current HEAD is already up to date or the merge can be resolved as a fast-forward."
    ),
)
@click.argument("commit", required=True, metavar="COMMIT")
@click.pass_context
def merge(ctx, ff, ff_only, commit):
    """ Incorporates changes from the named commits (usually other branch heads) into the current branch. """
    repo_dir = ctx.obj["repo_dir"]
    repo = pygit2.Repository(repo_dir)

    if ff_only and not ff:
        raise click.BadParameter(
            "Conflicting parameters: --no-ff & --ff-only", param_hint="--ff-only"
        )

    c_base = repo[repo.head.target]

    # accept ref-ish things (refspec, branch, commit)
    c_head, r_head = repo.resolve_refish(commit)

    print(f"Merging {c_head.id} to {c_base.id} ...")
    merge_base = repo.merge_base(c_base.oid, c_head.oid)
    print(f"Found merge base: {merge_base}")

    # We're up-to-date if we're trying to merge our own common ancestor.
    if merge_base == c_head.oid:
        print("Already merged!")
        return

    # We're fastforwardable if we're our own common ancestor.
    can_ff = merge_base == c_base.id

    if ff_only and not can_ff:
        print("Can't resolve as a fast-forward merge and --ff-only specified")
        ctx.exit(1)

    if can_ff and ff:
        # do fast-forward merge
        repo.head.set_target(c_head.id, "merge: Fast-forward")
        commit_id = c_head.id
        print("Fast-forward")
    else:
        ancestor_tree = repo[merge_base].tree

        merge_index = repo.merge_trees(
            ancestor=ancestor_tree, ours=c_base.tree, theirs=c_head.tree
        )
        if merge_index.conflicts:
            print("Merge conflicts!")
            for path, (ancestor, ours, theirs) in merge_index.conflicts:
                print(f"Conflict: {path:60} {ancestor} | {ours} | {theirs}")
            ctx.exit(1)

        print("No conflicts!")
        merge_tree_id = merge_index.write_tree(repo)
        print(f"Merge tree: {merge_tree_id}")

        user = repo.default_signature
        merge_message = "Merge '{}'".format(r_head.shorthand if r_head else c_head.id)
        commit_id = repo.create_commit(
            repo.head.name,
            user,
            user,
            merge_message,
            merge_tree_id,
            [c_base.oid, c_head.oid],
        )
        print(f"Merge commit: {commit_id}")

    # update our working copy
    wc = core.get_working_copy(repo)
    click.echo(f"Updating {wc.path} ...")
    commit = repo[commit_id]
    return checkout.checkout_update(repo, wc.path, wc.layer, commit, base_commit=c_base)


@cli.command()
@click.option(
    "--ff/--no-ff",
    default=True,
    help=(
        "When the merge resolves as a fast-forward, only update the branch pointer, without creating a merge commit. "
        "With --no-ff create a merge commit even when the merge resolves as a fast-forward."
    ),
)
@click.option(
    "--ff-only",
    default=False,
    is_flag=True,
    help=(
        "Refuse to merge and exit with a non-zero status unless the current HEAD is already up to date or the merge can be resolved as a fast-forward."
    ),
)
@click.option(
    "--ff-only",
    default=False,
    is_flag=True,
    help=(
        "Refuse to merge and exit with a non-zero status unless the current HEAD is already up to date or the merge can be resolved as a fast-forward."
    ),
)
@click.argument("repository", required=False, metavar="REMOTE")
@click.argument("refspecs", nargs=-1, required=False, metavar="REFISH")
@click.pass_context
def pull(ctx, ff, ff_only, repository, refspecs):
    """ Fetch from and integrate with another repository or a local branch """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    if repository is None:
        # matches git-pull behaviour
        if repo.head_is_detached:
            raise click.UsageError((
                "You are not currently on a branch. "
                "Please specify which branch you want to merge with."
            ))

        # git-fetch:
        # When no remote is specified, by default the origin remote will be used,
        # unless there's an upstream branch configured for the current branch.

        current_branch = repo.branches[repo.head.shorthand]
        if current_branch.upstream:
            repository = current_branch.upstream.remote_name
        else:
            try:
                repository = repo.remotes['origin'].name
            except KeyError:
                # git-pull seems to just exit 0 here...?
                raise click.BadParameter("Please specify the remote you want to fetch from", param_hint="repository")

    remote = repo.remotes[repository]

    # do the fetch
    print("Running fetch:", repository, refspecs)
    remote.fetch((refspecs or None))
    # subprocess.check_call(["git", "-C", ctx.obj['repo_dir'], 'fetch', repository] + list(refspecs))

    # now merge with FETCH_HEAD
    print("Running merge:", {'ff': ff, 'ff_only': ff_only, 'commit': "FETCH_HEAD"})
    ctx.invoke(merge, ff=ff, ff_only=ff_only, commit="FETCH_HEAD")


@cli.command()
@click.pass_context
def status(ctx):
    """ Show the working copy status """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    commit = repo.head.peel(pygit2.Commit)

    if repo.head_is_detached:
        click.echo(f"{click.style('HEAD detached at', fg='red')} {commit.short_id}")
    else:
        branch = repo.branches[repo.head.shorthand]
        click.echo(f"On branch {branch.shorthand}")

        if branch.upstream:
            upstream_head = branch.upstream.peel(pygit2.Commit)
            n_ahead, n_behind = repo.ahead_behind(commit.id, upstream_head.id)
            if n_ahead == n_behind == 0:
                click.echo(f"Your branch is up to date with '{branch.upstream.shorthand}'.")
            elif n_ahead > 0 and n_behind > 0:
                click.echo((
                    f"Your branch and '{branch.upstream.shorthand}' have diverged,\n"
                    f"and have {n_ahead} and {n_behind} different commits each, respectively.\n"
                    "  (use \"snow pull\" to merge the remote branch into yours)"
                ))
            elif n_ahead > 0:
                click.echo((
                    f"Your branch is ahead of '{branch.upstream.shorthand}' by {n_ahead} {_pc(n_ahead)}.\n"
                    "  (use \"snow push\" to publish your local commits)"
                ))
            elif n_behind > 0:
                click.echo((
                    f"Your branch is behind '{branch.upstream.shorthand}' by {n_behind} {_pc(n_behind)}, "
                    "and can be fast-forwarded.\n"
                    "  (use \"snow pull\" to update your local branch)"
                ))

    # working copy state
    working_copy = core.get_working_copy(repo)
    if not working_copy:
        click.echo('\nNo working copy.\n  (use "snow checkout" to create a working copy)')
        return

    db = gpkg.db(working_copy.path, isolation_level="DEFERRED")
    with db:
        dbcur = db.cursor()

        sql = """
            SELECT state, COUNT(feature_key) AS mod, COUNT(*) AS count
            FROM __kxg_map
            WHERE
                table_name = ?
                AND state != 0
                AND NOT (feature_key IS NULL AND state < 0)  -- ignore INSERT then DELETE
            GROUP BY state;
        """
        dbcur.execute(sql, [working_copy.layer])
        change_counts = {r['state']: (r['mod'], r['count']) for r in dbcur.fetchall() if r['state'] is not None}

        # TODO: check meta/ tree

        if not change_counts:
            click.echo("\nNothing to commit, working copy clean")
        else:
            click.echo((
                "\nChanges in working copy:\n"
                '  (use "snow commit" to commit)\n'
                '  (use "snow reset" to discard changes)\n'
            ))

            if 1 in change_counts:
                n_mod = change_counts[1][0]
                n_add = change_counts[1][1] - n_mod
                if n_mod:
                    click.echo(f"    modified:   {n_mod} {_pf(n_mod)}")
                if n_add:
                    click.echo(f"    new:        {n_add} {_pf(n_add)}")

            if -1 in change_counts:
                n_del = change_counts[-1][1]
                click.echo(f"    deleted:    {n_del} {_pf(n_del)}")


@cli.command('workingcopy-set-path')
@click.pass_context
@click.argument("new", nargs=1, type=click.Path(exists=True, dir_okay=False))
def workingcopy_set_path(ctx, new):
    """ Change the path to the working-copy """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    repo_cfg = repo.config
    if "kx.workingcopy" in repo_cfg:
        fmt, path, layer = repo_cfg["kx.workingcopy"].split(":")
    else:
        raise click.ClickException("No working copy? Try `snow checkout`")

    new = Path(new)
    if not new.is_absolute():
        new = os.path.relpath(new, repo_dir)

    repo.config["kx.workingcopy"] = f"{fmt}:{new}:{layer}"


# aliases/shortcuts


@cli.command()
@click.pass_context
def show(ctx):
    """ Show the current commit """
    ctx.invoke(log, args=["-1"])


@cli.command()
@click.pass_context
def reset(ctx):
    """ Discard changes made in the working copy (ie. reset to HEAD """
    ctx.invoke(checkout.checkout, force=True, refish="HEAD")


# straight process-replace commands

@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def log(ctx, args):
    """ Show commit logs """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "log"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def push(ctx, args):
    """ Update remote refs along with associated objects """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "push"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def fetch(ctx, args):
    """ Download objects and refs from another repository """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "fetch"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def branch(ctx, args):
    """ List, create, or delete branches """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "branch"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def remote(ctx, args):
    """ Manage set of tracked repositories """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "remote"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def tag(ctx, args):
    """ Create, list, delete or verify a tag object signed with GPG """
    repo_dir = ctx.obj["repo_dir"] or "."
    repo = pygit2.Repository(repo_dir)
    if not repo or not repo.is_bare:
        raise click.BadParameter(
            "Not an existing repository", param_hint="--repo"
        )

    _execvp("git", ["git", "-C", repo_dir, "tag"] + list(args))


@cli.command(context_settings=dict(ignore_unknown_options=True))
@click.argument("repository", nargs=1)
@click.argument("directory", required=False)
def clone(repository, directory):
    """ Clone a repository into a new directory """
    repo_dir = directory or os.path.split(repository)[1]
    if not repo_dir.endswith(".snow") or len(repo_dir) == 4:
        raise click.BadParameter("Repository should be myproject.snow")

    subprocess.check_call(["git", "clone", "--bare", repository, repo_dir])
    subprocess.check_call(
        [
            "git",
            "-C",
            repo_dir,
            "config",
            "--local",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ]
    )
    subprocess.check_call(["git", "-C", repo_dir, "fetch"])

    repo = pygit2.Repository(repo_dir)
    head_ref = repo.head.shorthand  # master
    subprocess.check_call(
        [
            "git",
            "-C",
            repo_dir,
            "config",
            "--local",
            f"branch.{head_ref}.remote",
            "origin",
        ]
    )
    subprocess.check_call(
        [
            "git",
            "-C",
            repo_dir,
            "config",
            "--local",
            f"branch.{head_ref}.merge",
            "refs/heads/master",
        ]
    )


if __name__ == "__main__":
    cli()
