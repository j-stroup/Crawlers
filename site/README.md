# v0rt public site

A small PHP site that shows the loot from `comment_hunter.py` and lets
visitors "like" finds. Flat files + SQLite, no framework, no build step -
drop it on any PHP 7.4+/8 host with `pdo_sqlite` enabled.

## Files

```
index.php        the gallery (server-rendered cards + like buttons)
about.php        the about page
vote.php         AJAX endpoint that records a like
lib.php          shared helpers (finds loader, SQLite likes, sanitizing)
config.php       paths + your removal-request contact email  <-- edit this
partials/        shared <head>/banner and footer
assets/          style.css + app.js (boot, filter, sort, likes)
data/            public_finds.json (you upload) + votes.sqlite (auto-created)
```

## Deploy

1. **Edit `config.php`** - set `contact` to a real address (shown in the
   footer so site owners can request removal).
2. **Produce the data** on your machine:
   ```
   python comment_hunter.py hunt_seeds.txt --public
   ```
   That writes a redacted, publish-safe `public_finds.json` (emails masked,
   IPs stripped, any find containing a real secret dropped). Re-running with
   `--public` after a finished crawl just re-exports without re-fetching.
3. **Upload** the whole `site/` folder, and copy your fresh
   `public_finds.json` into `site/data/`.
4. Make `data/` **writable** by the web server (so `votes.sqlite` can be
   created). On most shared hosts the uploaded dir is already writable; if
   not, `chmod 775 data` (or 777 if your host requires it).

To refresh the gallery later, just re-run step 2 and re-upload
`data/public_finds.json`. Likes persist across refreshes because each find's
id is a stable hash of its content, kept in `votes.sqlite` (which you do
**not** overwrite).

## Notes

- Likes are deduped per visitor by a coarse IP+User-Agent fingerprint plus a
  per-browser `localStorage` flag. It's a fun counter, not ballot security.
- The data file is the only thing the site reads; the raw `finds.jsonl`
  (which may contain unredacted emails) never leaves your machine.
- If your host lacks `pdo_sqlite`, the likes layer in `lib.php` is the only
  thing to swap (to MySQL or a flat file).
