# 2D assets

For art packs, game assets, and fonts: **artists** holding **images**, **fonts**, and **archives**.

## What it makes

- **Image** (PNG, JPEG, GIF, WebP, BMP, TIFF, PSD, TGA): width, height, and dimensions ("1920 × 1080").
- **Font** (TTF, OTF, WOFF, WOFF2): family and style, read from the font. Its thumbnail is a sample: "Aa" and "Quick fox" drawn in it.
- **Archive** (ZIP, 7z, RAR, CBZ, CBR, CB7): how many images it holds; its thumbnail is the best image inside (one named like a cover or preview, else the first).
- All three have the artist, extension, folder, size, and when modified. Double-click opens the file.
- **Artist:** a folder at the level the **Artist folder level** option says (1, the default: `Root/<Artist>/…`; 2 for `Root/<Store>/<Artist>/…`). Files above that level have no artist. The same artist's folders in two watched folders are one artist. An artist's thumbnail is its `folder.jpg` (or `cover.png`…), else one of its assets'.

## Searches

All grids: **Assets** (images, fonts, and archives), **Artists**, **Images**, **Fonts** (by family, then style), and **Archives**. The asset searches inherit tags, so tagging an artist `Pixel art` makes all its assets match `Pixel art`.

## Dashboard

Space used by images, image types, and the biggest artists.

## Duplicates

Images that look alike (the same picture resized or re-saved, but not a recolored one) are listed as similar items.

## Options

**Artist folder level** (Keep configuration's Keep tab, or per folder): which folder under a watched folder names the artist. Changing it moves assets to their new artists at the next scan; an artist left with nothing is removed.

Reading RAR archives' images needs a RAR tool (`unrar`, 7-Zip, or `bsdtar`) installed; without one they show an icon. See [FAQ](../reference/faq.md).
