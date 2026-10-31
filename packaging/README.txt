Tagalot
=======

Keep this folder together: tagalot.exe (tagalot on Linux) needs the _internal folder
beside it. Move or copy the whole folder, then run tagalot.exe inside it.

To install Tagalot instead (a Start menu entry and an uninstaller on Windows), use the
installer from the same release: the setup .exe, the macOS .dmg, or the Linux .AppImage.

To check what this build can do without opening a window:

    tagalot.exe --check --output check.txt

For scheduled backups on Windows, run tagalot-cli.exe (in this folder) instead of
tagalot.exe: it prints to the console and returns an exit code.

    tagalot-cli.exe --backup "D:\Keeps\Movies.keep" --to "E:\Backups\Tagalot" --keep-last 14

See https://torstees.github.io/tagalot/#/guide/backups

https://github.com/torstees/tagalot
