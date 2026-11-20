<?php
// RSS 2.0 feed of the most recent finds. Served pretty at /feed.xml.
require __DIR__ . '/lib.php';

$finds = load_finds();
usort($finds, function ($a, $b) { return strcmp($b['ts'] ?? '', $a['ts'] ?? ''); });
$finds = array_slice($finds, 0, 30);

$base = base_url();
$built = !empty($finds) ? date('r', strtotime($finds[0]['ts'])) : date('r');

header('Content-Type: application/rss+xml; charset=utf-8');
echo '<?xml version="1.0" encoding="UTF-8"?>' . "\n";
?>
<rss version="2.0">
  <channel>
    <title>v0rt // latest loot</title>
    <link><?= h($base) ?>/</link>
    <atom:link xmlns:atom="http://www.w3.org/2005/Atom" href="<?= h($base) ?>/feed.xml" rel="self" type="application/rss+xml"/>
    <description>human things hiding in the source of the open web - the latest finds from v0rt.</description>
    <language>en</language>
    <lastBuildDate><?= h($built) ?></lastBuildDate>
<?php foreach ($finds as $f):
    $label = KIND_LABELS[$f['kind']] ?? $f['kind'];
    $url   = $base . '/find/' . $f['id'];
    $pub   = !empty($f['ts']) ? date('r', strtotime($f['ts'])) : date('r');
?>
    <item>
      <title><?= h('[' . $label . '] from ' . ($f['domain'] ?? 'the web')) ?></title>
      <link><?= h($url) ?></link>
      <guid isPermaLink="true"><?= h($url) ?></guid>
      <pubDate><?= h($pub) ?></pubDate>
      <description><?= h(find_snippet($f, 500)) ?></description>
    </item>
<?php endforeach; ?>
  </channel>
</rss>
