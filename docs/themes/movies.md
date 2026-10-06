# Movies

For video files named the way Plex, Kodi, and Jellyfin like them: **collections** ⊃ **movies**, and the **actors** in them.

## What it makes

- **Movie:** from a video's name (`Inception (2010)`, `Blade Runner [1982]`, `The.Matrix.1999.1080p.BluRay`). Videos side by side whose names differ only by a part, quality, or edition (`- 720p`, `cd2`, `part1`, `- Extended`) are one movie with several files. A folder named like a movie (`Inception (2010)/`), or whose videos are all one movie, is that movie's folder.
- **Extras aren't movies:** names ending `-trailer`, `-sample`, `-featurette`, `-behindthescenes`, `-deleted`, `-interview`, `-scene`, `-short`, `-other`, and videos in `Extras`, `Featurettes`, `Trailers`, `Behind The Scenes`, and similar folders. [Triage](../guide/triage.md) lists them; **Skip in scans** hides them.
- **Video details** (read with MediaInfo): length, resolution, quality (`4K`, `1440p`, `1080p`, `720p`, `SD`), video codec, and audio languages. A movie with several versions shows its best.
- **`.nfo` files** (`movie.nfo`, or named like the video): title, year, plot, genre, director, length, and rating, which win over the file name; the **cast** in order; and a **set**, which becomes a collection.
- **Collection:** a folder holding two or more movies' folders, or an `.nfo`'s set.
- **Actor:** from `.nfo` casts, or added by hand. Their photo is Kodi's `.actors/First_Last.jpg`.

## Pictures

In a movie's folder, its **poster** is `poster.jpg` (or `folder`, `cover`, `movie`, `default`, or a picture named like the movie); its other pictures, and those in `screenshots`, `extrafanart`, `backdrops`… subfolders, are its **screenshots**. Beside loose videos, `Heat (1995)-poster.jpg` is that movie's poster and `-fanart`, `-landscape`, `-thumb`, `-banner` pictures are screenshots.

## Cast by hand

A movie's page has **Cast**, an actor's **Filmography**: both with **Add…** (search for an actor or movie, or make a new actor) and **Remove from …**. What you add or remove by hand stays through later scans, whatever the `.nfo` says.

## Searches

All grids, sorted by title: **Movies** (inheriting tags: tag a collection `Fantasy` and its movies match), **Actors**, and **Collections**. Movie cards show the year and quality; a collection lists its movies by year.

## Genres as tags

The `.nfo`'s genres are [file keywords](../guide/file-keywords.md).
