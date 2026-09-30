"""Choosing an item's file to open, and the commands that open it (#104, DESIGN.md §11)."""

from pathlib import Path

import pytest
from sqlalchemy import update

from tagalot.core.detail import FileRow
from tagalot.core.handlers import (
    CannotOpen,
    Command,
    FileToOpen,
    choose_file,
    expand_command,
    files_to_open,
    open_with_command,
    program_command,
    program_name,
    resource_to_open,
    reveal_command,
    reveal_folder,
    split_command,
)
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.settings import Settings
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures

OK, OFFLINE, MISSING = ResourceStatus.OK, ResourceStatus.OFFLINE, ResourceStatus.MISSING
UNC = "\\\\nas\\music\\Miles Davis\\01 So What.flac"


def _row(
    relpath: str = "A/song.mp3", status: ResourceStatus = OK, path: str | None = "/m/A/song.mp3"
) -> FileRow:
    return FileRow(1, "Music", relpath, "file", 10, status, path)


def test_choose_the_first_file_that_can_be_opened() -> None:
    offline = _row("A/one.flac", OFFLINE, "/m/A/one.flac")
    ok = FileRow(2, "Music", "A/one.mp3", "file", 10, OK, "/m/A/one.mp3")
    assert choose_file([offline, ok]) == FileToOpen(2, "/m/A/one.mp3", False)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ([], "Nothing to open: this item has no files."),
        ([_row(status=OFFLINE)], "Can't open song.mp3: Music was offline at the last scan."),
        ([_row(status=MISSING)], "Can't open song.mp3: it was missing at the last scan."),
        ([_row(path=None)], "Can't open song.mp3: Music has no path on this computer."),
        ([_row("", path=None)], "Can't open Music: Music has no path on this computer."),
    ],
)
def test_why_nothing_can_be_opened(files: list[FileRow], message: str) -> None:
    with pytest.raises(CannotOpen) as caught:
        choose_file(files)
    assert str(caught.value) == message


def test_reveal_commands() -> None:
    windows = FileToOpen(1, UNC, False)
    assert reveal_command(windows, "win32") == Command("explorer.exe", ("/select,", UNC))
    mac = FileToOpen(1, "/Users/me/Music/a b.mp3", False)
    assert reveal_command(mac, "darwin") == Command("open", ("-R", "/Users/me/Music/a b.mp3"))
    linux = FileToOpen(1, "/home/me/Music/a.mp3", False)
    assert reveal_command(linux, "linux") is None
    assert reveal_folder(linux, "linux") == "/home/me/Music"
    assert reveal_folder(windows, "win32") == "\\\\nas\\music\\Miles Davis"
    album = FileToOpen(1, "/home/me/Music/Kind of Blue", True)
    assert reveal_folder(album, "linux") == "/home/me/Music/Kind of Blue"  # opened itself


def test_open_with_commands() -> None:
    song = FileToOpen(1, "/Users/me/a.mp3", False)
    assert open_with_command(song, "/Applications/VLC.app", "darwin") == Command(
        "open", ("-a", "/Applications/VLC.app", "/Users/me/a.mp3")
    )
    assert open_with_command(song, "/usr/bin/vlc", "linux") == Command(
        "/usr/bin/vlc", ("/Users/me/a.mp3",)
    )
    assert open_with_command(FileToOpen(1, UNC, False), "C:\\vlc.exe", "win32") == Command(
        "C:\\vlc.exe", (UNC,)
    )


def test_files_to_open_follow_the_primary_role(env: Env) -> None:
    env.scan()
    root = str(env.files)
    with env.reader.connect() as conn:
        song = files_to_open(conn, env.schema, env.entity("So What"), lambda _: root)
        album = files_to_open(conn, env.schema, env.entity("Kind of Blue"), lambda _: root)
        artist = files_to_open(conn, env.schema, env.entity("Miles Davis"), lambda _: root)
        elsewhere = files_to_open(conn, env.schema, env.entity("So What"), lambda _: None)
    assert [f.relpath for f in song] == [
        "Miles Davis/Kind of Blue/01 So What.flac",
        "Miles Davis/Kind of Blue/01 So What.mp3",
    ]  # both versions
    assert song[0].path == str(Path(root, "Miles Davis", "Kind of Blue", "01 So What.flac"))
    assert [(f.relpath, f.kind) for f in album] == [("Miles Davis/Kind of Blue", "dir")]
    assert artist == ()
    assert elsewhere[0].path is None


def test_a_missing_version_is_skipped(env: Env) -> None:
    env.scan()
    flac = "Miles Davis/Kind of Blue/01 So What.flac"
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource).where(Resource.relpath == flac).values(status=MISSING)
        )
    )
    root = str(env.files)
    with env.reader.connect() as conn:
        files = files_to_open(conn, env.schema, env.entity("So What"), lambda _: root)
        chosen = choose_file(files)
        one = resource_to_open(conn, env.entity("So What"), files[0].resource_id, lambda _: root)
    assert chosen.path.endswith("01 So What.mp3")
    assert [(f.relpath, f.status) for f in one] == [(flac, MISSING)]


# --- per-user overrides (#105) ---


@pytest.mark.parametrize(
    ("template", "words"),
    [
        (
            '"C:\\Program Files\\Krita\\krita.exe" "{path}"',
            ["C:\\Program Files\\Krita\\krita.exe", "{path}"],
        ),
        ("vlc --play-and-exit {path}", ["vlc", "--play-and-exit", "{path}"]),
        ('tool --title="{name}"  x', ["tool", "--title={name}", "x"]),
        ('empty ""', ["empty", ""]),
        ("   ", []),
    ],
)
def test_split_command(template: str, words: list[str]) -> None:
    assert split_command(template) == words


def test_expand_command() -> None:
    song = FileToOpen(1, UNC, False, "audio")
    assert song.ext == ".flac"
    assert expand_command('"C:\\vlc.exe" "{path}"', song) == Command("C:\\vlc.exe", (UNC,))
    assert expand_command("tool {dir} {name}", song) == Command(
        "tool", ("\\\\nas\\music\\Miles Davis", "01 So What.flac")
    )
    assert expand_command("vlc --fullscreen", song) == Command("vlc", ("--fullscreen", UNC))
    unix = FileToOpen(1, "/home/me/a b/c.PSD", False)
    assert unix.ext == ".psd"
    assert expand_command("gimp {path}", unix) == Command("gimp", ("/home/me/a b/c.PSD",))
    with pytest.raises(CannotOpen):
        expand_command("  ", unix)


def test_program_commands_and_names() -> None:
    exe = "C:\\Program Files\\Krita (x64)\\bin\\krita.exe"
    assert program_command(exe, "win32") == f'"{exe}" "{{path}}"'
    assert program_name(program_command(exe, "win32")) == "krita"
    app = "/Applications/Pixelmator Pro.app"
    assert program_command(app, "darwin") == 'open -a "/Applications/Pixelmator Pro.app" "{path}"'
    assert program_name(program_command(app, "darwin")) == "Pixelmator Pro"
    assert program_name('"/usr/bin/gimp-2.10" "{path}"') == "gimp-2.10"


def test_setting_and_removing_rules() -> None:
    settings = Settings()
    settings.set_handler("PSD", "krita {path}")
    settings.set_handler(".psd", "gimp {path}")  # replaces it
    settings.set_handler(".psd", "viewer {path}", role="preview")
    assert [(h.ext, h.role, h.command) for h in settings.handlers] == [
        (".psd", None, "gimp {path}"),
        (".psd", "preview", "viewer {path}"),
    ]
    rule = settings.handler_for(".PSD", "preview")
    assert rule is not None
    settings.remove_handler(rule)
    assert settings.handler_for(".psd", "preview") == settings.handler_for(".psd")
