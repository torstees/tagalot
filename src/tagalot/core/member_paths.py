"""How a file inside an archive (#391) is named and shown (#524).

A member's ``relpath`` is its archive's and its path inside it
(``Aurora Studio/pack.zip/fonts/Regular.ttf``); it is shown as the archive's path and the
path inside it, with :data:`PATH_MARK` between.
"""

PATH_MARK = "\u203a"
"""Between a root's name and a path inside it, and between an archive and a member."""


def member_relpath(archive: str, name: str) -> str:
    """The relpath of the member ``name`` (its path inside the archive) of the archive at
    ``archive`` (its relpath)."""
    return f"{archive}/{name}"


def member_name(archive: str, relpath: str) -> str:
    """The member's path inside the archive, from its relpath."""
    return relpath[len(archive) + 1 :]


def shown_path(relpath: str, archive: str | None) -> str:
    """A file's path as shown: for a member of an archive, the archive's path and the
    member's path inside it, with :data:`PATH_MARK` between."""
    if not archive:
        return relpath
    return f"{archive} {PATH_MARK} {member_name(archive, relpath)}"
