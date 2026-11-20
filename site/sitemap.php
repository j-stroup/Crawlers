<?php
// XML sitemap, served pretty at /sitemap.xml. Lists the main pages plus
// every find permalink, so search engines can discover the long-tail
// content pages (which is where the unique, indexable text lives).
require __DIR__ . '/lib.php';

$base  = base_url();
$finds = load_finds();

header('Content-Type: application/xml; charset=utf-8');
echo '<?xml version="1.0" encoding="UTF-8"?>' . "\n";
?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc><?= h($base) ?>/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>
  <url><loc><?= h($base) ?>/stats.php</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>
  <url><loc><?= h($base) ?>/about.php</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>
<?php $archPages = max(1, (int) ceil(count($finds) / 60));
for ($p = 1; $p <= $archPages; $p++): ?>
  <url><loc><?= h($base . '/archive' . ($p > 1 ? '/' . $p : '')) ?></loc><changefreq>weekly</changefreq><priority>0.4</priority></url>
<?php endfor; ?>
<?php foreach ($finds as $f):
    if (empty($f['id'])) continue;
    $lastmod = !empty($f['ts']) ? substr($f['ts'], 0, 10) : '';
?>
  <url><loc><?= h($base . '/find/' . $f['id']) ?></loc><?php if ($lastmod): ?><lastmod><?= h($lastmod) ?></lastmod><?php endif; ?><changefreq>monthly</changefreq><priority>0.6</priority></url>
<?php endforeach; ?>
</urlset>
