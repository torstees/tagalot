# Writing a Tagalot theme

A **theme** tells Tagalot what a keep's files *are*: which kinds of items to make from them (songs, fonts, movies, recipes), what fields each item has, how items contain or relate to each other, and which searches, pages, thumbnails, and commands to offer. Tagalot handles everything else: scanning, tags, search, undo, the window.

A theme is **one Python file** with **one `Theme` subclass**, importing only `tagalot.themes.api`. Tagalot ships four you can read as examples, from simplest to richest: [`generic.py`](../src/tagalot/builtin_themes/generic.py) (one item per file), [`assets2d.py`](../src/tagalot/builtin_themes/assets2d.py) (artists, images, fonts, archives), [`music.py`](../src/tagalot/builtin_themes/music.py) (artists ⊃ albums ⊃ songs), and [`movies.py`](../src/tagalot/builtin_themes/movies.py) (collections, movies, a cast). The design behind all of this is [DESIGN.md §9](DESIGN.md#9-theme-api).

## Quick start

1. **Make a theme from the template:**

   ```
   tagalot --new-theme recipes --name "Recipes"
   ```

   (From a source checkout: `uv run tagalot --new-theme recipes --name "Recipes"`.) It writes `recipes.py` into your **themes folder**:

   | | |
   |---|---|
   | Windows | `%LOCALAPPDATA%\tagalot\themes` |
   | macOS | `~/Library/Application Support/tagalot/themes` |
   | Linux | `~/.local/share/tagalot/themes` |

   The template ([`src/tagalot/themes/template.py`](../src/tagalot/themes/template.py)) already works: it makes a **Group** of each folder under a root and an **Item** of each file in it, and reads text files' first lines as headlines. Every place to change is marked `TODO`.

2. **Check it** as you edit, without opening a window:

   ```
   tagalot --check-theme recipes
   ```

   It finds the theme by its id (`recipes.py` in your themes folder, or any theme declaring `id = "recipes"`; give a path to check a file elsewhere), imports it, and validates it the way the launcher does: `ok: 'recipes' (Recipes, version 1)`, or each problem with what to fix. If no theme has that id, it says where it looked and lists the theme files that failed to load, since a broken file can't say its id. (Packaged Windows builds have no console: add `--output check.txt` and read the file.)

3. **Use it:** in the launcher, **New keep…** lists your theme next to the built-in ones. Point the keep at a folder of your files and scan.

4. **Edit, then reopen:** themes are loaded each time a keep opens, so after an edit, close the keep (Keep → Close) and open it again. A theme with problems is listed in the launcher with them, and never crashes Tagalot. While a keep is open, the **Activity** panel (View → Activity) shows what your theme warned about.

## The shape of a theme

```python
from tagalot.themes.api import Entity, IngestContext, ResourceInfo, Theme, field, role

class Recipe(Entity):
    title_label = "Name"
    cuisine: str | None = field("Cuisine", card=True, search="choice")
    roles = [role("file", kinds={"any"}, primary=True)]

class RecipesTheme(Theme):
    id, name, version = "recipes", "Recipes", 1
    extensions = frozenset({".md", ".txt"})
    entities = [Recipe]

    def ingest(self, batch: list[ResourceInfo], ctx: IngestContext) -> None:
        for resource in batch:
            existing = ctx.entities_of(resource, "file")
            if existing:
                ctx.update(existing[0], title=resource.relpath.rsplit("/", 1)[-1])
            else:
                recipe = ctx.upsert(Recipe, f"recipe:{resource.relpath}",
                                    title=resource.relpath.rsplit("/", 1)[-1])
                ctx.link(recipe, resource, "file")
```

That is a complete theme. The rest of this guide is what you can add.

## Items: entity types and fields

An **entity type** is a class deriving from `Entity`. Its **fields** are annotated class attributes declared with `field()`:

```python
class Song(Entity):
    title_label = "Title"                      # what the item's name is called
    year: int | None = field("Year", card=True, search="range")
    genre: str | None = field("Genre", search="choice")
    duration: float | None = field("Length", search="range", editable=False, display="duration")
    folder: str = field("Folder", search="text", editable=False, detail=False)
```

- **Types:** `str`, `int`, `float`, `bool`, `date`, `datetime`, each optionally `| None`.
- **`field(label, *, card, search, editable, detail, display)`:**
  - `card=True` offers the field as a column and a card line.
  - `search` makes it filterable: `"text"` (contains, or starts with), `"range"` (numbers and dates, from–to), or `"choice"` (pick from its values).
  - `editable=False` stops people editing it on the item's page. Values people edit by hand always win: your `ingest` never overwrites them.
  - `detail=False` hides it from the item's page (a value you keep for yourself, like assets2d's picture hash).
  - `display` shows a number as `"bytes"` (3.0 MB) or `"duration"` (seconds as 3:25).
- **Class settings:** `label` and `plural` (how the type is named; "Category" pluralizes itself, but "Series" needs `plural`); `title_label`; `double_click = "open_file"` (double-click plays or opens the file, Ctrl+Enter the page; the default is the page); `card_lines` (fields shown under the title on grid cards); `contents_sort` (for a container, the order of what it holds, as `SortBy` keys).
- Every item also has a **title** (its name), **tags**, and **extra fields** people add themselves; you don't declare those.

## Files: roles

A **role** is what a file is to an item, declared on the type: `roles = [role("audio", kinds={"audio"}, many=True, primary=True)]`.

- `kinds`: which files the role takes: `"image"`, `"audio"`, `"video"`, `"font"`, `"archive"`, `"dir"` (a folder), or `"any"`. Kinds come from extensions (`KIND_EXTENSIONS`).
- `many=True`: the role holds several files (a song's versions, a movie's screenshots); otherwise linking another file replaces the one there.
- `primary=True`: the item's main file, the one Open file opens (at most one per type).
- `thumbnail=True`: tried early for the item's thumbnail.
- `label`: how the role is titled on the item's page (default: its name, capitalized).

## Containment and relationships

- **Containment** makes a hierarchy: `containment = [contains(Artist, Album), contains(Album, Song)]`. An item can be in several containers. Searches can list what matching containers hold, and tags on a container can count for its contents ("inherit tags").
- **Relationships** link two types without nesting: `relationships = [related("cast", Actor, Movie, label="Filmography", reverse_label="Cast")]`. `label` titles the section on the first type's page (an actor's Filmography), and `reverse_label` the one on the second's (a movie's Cast). People can also add and remove related items by hand on those pages; your `ingest` won't undo what they did.

## The theme class

```python
class MusicTheme(Theme):
    id, name, version = "music", "Music", 2
    api_version = 2              # the theme API it needs
    extensions = frozenset({".mp3", ".flac", ".jpg"})   # empty: every file
    dirs = True                                     # folders become resources too
    entities = [Artist, Album, Song]
    containment = [...]
    relationships = [...]
    views = [...]
    options = [option("artist_level", 1, label="Artist folder level")]
    dashboard = [...]
    thumbnail_max = 512          # the size thumbnails are made and cached at
    thumbnail_default = 160      # how big grid cards start
```

- **`id`** is stored in every keep made with the theme: choose it once (lowercase letters, digits, `_`).
- **`version`**: raise it when `ingest` starts reading something new, or the data changes shape. Keeps made with an older version ask to upgrade (backing up first), then read every file again once at the next scan ([Changing a theme people already use](#changing-a-theme-people-already-use)).
- **`api_version`**: the version of `tagalot.themes.api` the theme needs (2 if it defines `migrate_schema`); left out, it is the installed one.
- **`dirs`**: whether folders become resources you can link (an album's folder). `True`, `False`, or a function of the folder's relative path.
- **`options`**: settings a keep (or one of its folders) can change in its configuration window, read with `ctx.option(name)`. Changing one makes that folder's files be read again.

## Reading files: `prepare` and `ingest`

A scan hands your theme **batches** of new and changed files and folders, as `ResourceInfo` values: `id`, `root_id`, `relpath` (POSIX, relative to the root), `kind` (`"file"` or `"dir"`), `ext`, `size`, `mtime_ns`, and `path` (this computer's path, for reading).

- **`prepare(batch)`** runs first, in a worker, with **no database**. Read the files here: tags, image sizes, the first line of a text file. Return `{resource id: whatever ingest needs}`. Catch a bad file and return an error marker for it rather than raising; one bad file must never stop a scan. Several `prepare` calls run at once on different threads, each with a few resources, so keep anything it remembers in local variables, not on `self` or in module globals.
- **`ingest(batch, ctx)`** then runs in the database writer, inside a transaction: turn files into items, links, containment, and fields through `ctx`. Keep it quick, and don't read files here.

### What `ctx` can do

| | |
|---|---|
| `ctx.upsert(Type, key, title=…, **fields)` | Find the item of `Type` with this **key**, or make it; set its fields. |
| `ctx.update(item, title=…, **fields)` | Set fields on an item you already have. |
| `ctx.link(item, resource, role, sort_order=0)` / `ctx.unlink(…)` | Link a file to an item in a role, or unlink it. |
| `ctx.contain(parent, child)` / `ctx.uncontain(…)` | Put an item in a container, or take it out. |
| `ctx.relate(name, a, b)` / `ctx.unrelate(…)` / `ctx.related(name, item)` | Relationships, and what an item is related to. |
| `ctx.entities_of(resource, role)` | The items a file is linked to. |
| `ctx.find(Type, **equals)` / `ctx.get(item)` | Look items up; read their title, fields, and extra fields. |
| `ctx.contents(item)` / `ctx.linked(item, role)` | What an item holds; which files it has. |
| `ctx.delete(item)` | Delete an item your theme made (an artist left with nothing). |
| `ctx.prepared(resource)` | What `prepare` returned for a file. |
| `ctx.keywords(item, resource, ["Fantasy", "Genre/Space opera"])` | What a file says the item is about (front matter's tags, an EPUB's subjects). They become Tagalot tags only by matching tags the user defined (a path, an alias, or a name), never new ones; unmatched ones are listed for the user to map. |
| `ctx.option(name)` | A theme option's value for the folder being scanned. |
| `ctx.warn(resource, message)` | Report a problem with a file to the Activity panel. |

### Keys and identity

`upsert`'s **key** decides when two files are the same item, and it's the most important choice in a theme:

- **Natural keys** say what an item *is*: an album keyed by its folder (`album:Jazz/Kind of Blue`), an artist by normalized name, a song by album, disc, track, and title. Two files with the same key are one item, so a FLAC and an MP3 of a song become its two versions.
- **Per-file items** should find their item through the file, not its path: `ctx.entities_of(resource, role)` first, and only when that's empty make a new item with a **never-reused** key (`f"item:{uuid.uuid4().hex}"`). When a file moves or is renamed, Tagalot carries its links to the new path, so its item, tags, and edits follow it. Keying by path instead would make a moved file a new item.

### What people did stays done

Your theme runs on every scan, but it never undoes what people did by hand:

- **Fields they edited** keep their values: `upsert` and `update` leave them alone.
- **Files they linked by hand** aren't returned by `entities_of`, and your `unlink` can't remove them.
- **Relationships they added or removed** stay that way, whatever `relate` and `unrelate` say.
- **Items they merged** don't come back: an `upsert` of a merged item's key, or `entities_of` one of its files, gives a stand-in whose writes are dropped.
- **Items they made by hand** (a new actor) are never deleted by a scan.

## Views and pages

- **`SearchView(name, types, inherit_tags=False, show_contained=False, aggregate_up=False, layout="grid", default_sort=…)`**: a search under SEARCHES. `inherit_tags=True` lets a tag on a container count for its contents; `aggregate_up=True` starts the view with **By contents** on (a container matches when anything inside it does); `layout` is `"grid"`, `"list"`, or `"tree"` (containers expand to their contents).
- **`DetailView(Type, sections)`**: an item's page, top to bottom:
  - `Section.fields()`: its fields, editable in place.
  - `Section.role(name)`: a role's files.
  - `Section.gallery(name)`: a many-file image role as thumbnails.
  - `Section.related(name)`: a relationship, as a list or grid people can add to.
  - `Section.contents()`: a container's contents, as a search.

  Without a `DetailView`, a type's page shows its fields, its primary role, and its contents.

## Thumbnails

Each type's thumbnail comes from a **chain of providers**, tried in order until one gives a picture; the default is its `thumbnail=True` roles, then its primary file, then an icon. Override `thumbnail_chain(entity_type)` to choose:

```python
def thumbnail_chain(self, entity_type):
    if entity_type is Movie:
        return [RoleImage("poster"), RoleImage("screenshot"), Icon("video")]
    return super().thumbnail_chain(entity_type)
```

Providers in the API: `RoleImage(role)`, `ImageFile()` (the primary file, drawn by its kind: an audio file shows its embedded art, an archive its first picture), `FolderImage(names)` (`folder.jpg`, `cover.png`…), `EmbeddedAudioArt()`, `ArchiveFirstImage()`, `ParentThumbnail()`, and `Icon(kind)`, which ends the chain. For your own, subclass `ThumbnailProvider` and yield candidate files from `candidates(entity, ctx)`, using the read-only `ThumbnailContext` (`resources`, `parents`, `children`, `folder_files`, `thumbnail_of`).

## Commands: actions

An **action** is a method marked with `@action(label, applies_to)`. It appears on the right-click menus of the items it applies to and as a button on their pages:

```python
@action("Play album", [Album])
def play_album(self, albums, ctx):
    path = ctx.temp_path("album.m3u8")
    ...  # write a playlist from ctx.contents(...) and ctx.resources(...)
    ctx.open(path)
```

It runs in the writer, in one transaction: what it changes is one Edit → Undo step, and an exception undoes it all. Besides everything `ctx` above can do, an action's `ActionContext` can list files (`ctx.resources(item, role)`), write only to its own temporary folder (`ctx.temp_path(name)`), and, after it finishes, `ctx.open(path)`, `ctx.reveal(path)`, or say something in the status bar with `ctx.message(text)`.

## The dashboard

`dashboard = [stat("Total running time", Song, "duration"), top_values("Top genres", Album, "genre")]` adds cards to a keep's opening page: `stat` computes `"sum"`, `"avg"`, `"min"`, `"max"`, or `"count"` over a field, and `top_values` lists a field's most common values, each linking to a search. For anything else, mark a method `@dashboard_card(title)`: it gets a read-only `DashboardContext` (`count`, `find`, `get`, `stat`) and returns `(label, value)` rows or a text.

## Near-duplicates

To have Dedupe find items that are alike (the same song in two places, a resized picture), give each item cheap **blocking keys** and score pairs that share one:

```python
def blocking_keys(self, entity_type, record):
    if entity_type is Song:
        yield f"{normalize(record.fields['artist'] or '')}|{normalize(record.title)}"

def similarity(self, entity_type, a, b):
    ...  # 0 (not alike) to 1 (the same)
```

Only items sharing a key are compared, never all pairs; pairs at or above `near_duplicate_threshold` (0.9 by default) are listed.

## Changing a theme people already use

Keeps remember which `version` of your theme made their data. When a keep made with an older version opens, Tagalot asks before upgrading it, backs up `keep.db`, then:

1. runs your **`migrate_schema(from_version, ops)`**: renames, type changes, and drops you ask for;
2. **adds** any new types and fields itself;
3. runs your **`migrate(from_version, ctx)`**: changes to the data;
4. records the new version. Every file is read again once, at the next scan.

All of it is one transaction: if anything fails, nothing changes, the user is told why, and the backup stays. `from_version` is the version the keep had, so write each change under `if from_version < N:` for the version that made it; a keep several versions behind then gets every step in order.

| You want to | Write |
|---|---|
| add a type or field | nothing (raise `version` if files should be read again to fill it) |
| fill in or reshape data | `migrate()` |
| rename a field, change its type, or remove it | `migrate_schema()` (needs `api_version = 2`) |

**Adding a field.** Declare it. Tagalot adds the column when a keep opens, empty (or the type's default, if it can't be `None`). To fill it from the files, raise `version` too, and `ingest` sets it at the next scan.

**Renaming a field** (`tempo` becomes `bpm`, version 3):

```python
class Song(Entity):
    bpm: int | None = field("BPM", search="range")    # was tempo

class MusicTheme(Theme):
    id, name, version, api_version = "music", "Music", 3, 2

    def migrate_schema(self, from_version, ops):
        if from_version < 3:
            ops.rename_field(Song, "tempo", "bpm")
```

Values move across, values people edited stay protected from scans, and saved searches that filter or sort on `tempo` use `bpm`.

**Changing a field's type**: declare the new type, then `ops.change_type(Song, "year")`. Values that clearly convert are converted: `"1999"` to 1999, numbers to text, `"yes"`/`"no"` and 1/0 to `bool`, and ISO text to dates. Blank text becomes `None`. If a value doesn't convert, the upgrade fails and names the item, so nothing is half-done. To decide yourself, pass a function: `ops.change_type(Song, "year", lambda v: int(v[:4]) if v else None)`. It gets each stored value (`str`, `int`, `float`, or `None`) and raises `ValueError` to fail.

To rename and retype in one go, rename first, then change the type of the new name.

**Removing a field**: stop declaring it. Its column then stays in the keep, unused and harmless. To delete it and its values, call `ops.drop_field(Song, "legacy_code")` too.

**Splitting a field** (`credit`, "Composer / Arranger", becomes `composer` and `arranger`): keep `credit` declared for this version, hidden, so `migrate()` can read it, and fill the new fields:

```python
class Song(Entity):
    composer: str | None = field("Composer", search="text")
    arranger: str | None = field("Arranger", search="text")
    credit: str | None = field("Credit", detail=False, editable=False)   # going in version 5

class MusicTheme(Theme):
    id, name, version = "music", "Music", 4

    def migrate(self, from_version, ctx):
        if from_version < 4:
            for song in ctx.find(Song):
                credit = ctx.get(song).fields["credit"] or ""
                composer, _, arranger = credit.partition(" / ")
                ctx.update(song, composer=composer or None, arranger=arranger or None)
```

In version 5, remove `credit` and call `ops.drop_field(Song, "credit")` under `if from_version < 5:`.

**`version` and `api_version`.** `version` is your theme's own: raise it whenever its data changes shape or its files should be read again. `api_version` is the version of `tagalot.themes.api` the theme needs. A Tagalot providing an older API refuses the theme, rather than open it and get things wrong. `migrate_schema` arrived in API version 2, so a theme defining it sets `api_version = 2`, and `--check-theme` reminds you if you forget.

**Supporting Tagalot with API version 1**: don't define `migrate_schema`. To rename a field, declare the new one and keep the old one declared, hidden (`detail=False, editable=False`), for one version. Copy it across in `migrate()`, then stop declaring it in the next version. Its column stays in older keeps, unused.

## Rules

- **Import only `tagalot.themes.api`.** Everything else in Tagalot is internal and changes without notice.
- **Never write, move, rename, or delete anything under a root.** Read files; nothing more. An action writes only to `ctx.temp_path`.
- **Read files in `prepare`, not `ingest`.** `ingest` holds the database's write lock.
- **Paths are POSIX and relative** (`relpath`); use `path` only for reading this computer's copy.
- **Report, don't raise,** for a bad file: return an error from `prepare`, then `ctx.warn` about it in `ingest`.
- **Keep keys stable.** A key you change later makes new items of old files (their tags stay on the old ones).

## Testing a theme

- `tagalot --check-theme ID_OR_FILE` catches import errors and declaration mistakes.
- The launcher lists every theme with its problems, and the Activity panel shows `ctx.warn` messages and files that couldn't be read.
- For automated tests, see how the built-in themes are tested in [`tests/themes/`](../tests/themes/): each builds a small folder of files in a temporary directory, scans it with `scan_root`, and checks the items made. [`tests/themes/test_template.py`](../tests/themes/test_template.py) does it for the template in about forty lines.
