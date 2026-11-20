<?php
// The numbers: a terminal-styled dashboard over the published finds.
require __DIR__ . '/lib.php';

$finds     = load_finds();
$total     = count($finds);
$collected = finds_total_collected();
$crawled   = finds_domains_crawled();

$byKind = $byDomain = $byMonth = $byTld = $byTag = [];
$firstTs = $lastTs = '';
$scoreSum = $scoreMax = $chars = $seenMax = 0;
$topSeenFind = null;
foreach ($finds as $f) {
    $byKind[$f['kind'] ?? '?']     = ($byKind[$f['kind'] ?? '?'] ?? 0) + 1;
    $dom = $f['domain'] ?? '?';
    $byDomain[$dom] = ($byDomain[$dom] ?? 0) + 1;
    $tld = preg_match('/\.([a-z0-9-]+)$/i', $dom, $m) ? '.' . strtolower($m[1]) : '?';
    $byTld[$tld] = ($byTld[$tld] ?? 0) + 1;
    foreach (($f['tags'] ?? []) as $t) { $byTag[$t] = ($byTag[$t] ?? 0) + 1; }
    $mo = substr($f['ts'] ?? '', 0, 7);
    if ($mo) $byMonth[$mo] = ($byMonth[$mo] ?? 0) + 1;
    $ts = $f['ts'] ?? '';
    if ($ts) { if ($firstTs === '' || $ts < $firstTs) $firstTs = $ts; if ($ts > $lastTs) $lastTs = $ts; }
    $sc = (int)($f['score'] ?? 0); $scoreSum += $sc; if ($sc > $scoreMax) $scoreMax = $sc;
    $chars += function_exists('mb_strlen') ? mb_strlen((string)($f['text'] ?? '')) : strlen((string)($f['text'] ?? ''));
    $so = (int)($f['seen_on'] ?? 1); if ($so > $seenMax) { $seenMax = $so; $topSeenFind = $f; }
}
$avgScore = $total ? round($scoreSum / $total, 1) : 0;
$domains  = count($byDomain);
arsort($byKind); arsort($byDomain); arsort($byTld); arsort($byTag); ksort($byMonth);
$topDomains = array_slice($byDomain, 0, 12, true);
$topTld     = array_slice($byTld, 0, 10, true);

// score distribution
$dist = ['0-2' => 0, '3-4' => 0, '5-6' => 0, '7-8' => 0, '9+' => 0];
foreach ($finds as $f) {
    $s = (int)($f['score'] ?? 0);
    if ($s <= 2) $dist['0-2']++; elseif ($s <= 4) $dist['3-4']++;
    elseif ($s <= 6) $dist['5-6']++; elseif ($s <= 8) $dist['7-8']++; else $dist['9+']++;
}

// most-seen leaderboard + cumulative growth
$fById = [];
foreach ($finds as $f) { $fById[$f['id']] = $f; }
$seen = [];
foreach ($finds as $f) { $so = (int)($f['seen_on'] ?? 1); if ($so > 1) $seen[$f['id']] = $so; }
arsort($seen);
$topSeen = array_slice($seen, 0, 8, true);
$cum = []; $run = 0;
foreach ($byMonth as $mo => $c) { $run += $c; $cum[$mo] = $run; }

// most-liked
$likes = like_counts(array_map(function ($f) { return $f['id']; }, $finds));
arsort($likes);
$topLiked = [];
foreach ($likes as $id => $c) { if ($c > 0 && count($topLiked) < 8) $topLiked[$id] = $c; }

function stat_bar($label, $count, $max, $href = null, $title = '') {
    $pct = $max > 0 ? max(2, round($count / $max * 100)) : 0;
    $lbl = $href
        ? '<a class="lbl" href="' . h($href) . '"' . ($title ? ' title="' . h($title) . '"' : '') . '>' . h($label) . '</a>'
        : '<span class="lbl"' . ($title ? ' title="' . h($title) . '"' : '') . '>' . h($label) . '</span>';
    echo '<div class="statrow">' . $lbl
       . '<span class="bar"><span class="barfill" style="width:' . $pct . '%"></span></span>'
       . '<span class="num">' . number_format((int)$count) . '</span></div>';
}
function tile($num, $label, $sub = '') {
    echo '<div class="tile"><span class="tnum">' . h($num) . '</span>'
       . '<span class="tlabel">' . h($label) . '</span>'
       . ($sub ? '<span class="tsub">' . h($sub) . '</span>' : '') . '</div>';
}

$title    = 'v0rt stats — the numbers behind the finds';
$nav      = 'stats';
$og_title = 'v0rt // the numbers';
$og_desc  = 'The numbers behind v0rt: how many easter eggs, HTML comments, console messages, ASCII art and jokey headers it has collected, by type, domain, TLD and tag.';
require __DIR__ . '/partials/head.php';
?>
  <p class="stat">[ <b><?= number_format($collected) ?></b> collected ]
     [ <?= h(substr($firstTs, 0, 10)) ?: '—' ?> &rarr; <?= h(substr($lastTs, 0, 10)) ?: '—' ?> ]<span class="cursor">&nbsp;</span></p>

  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">./loot --stats</span></h2>

<?php if (!$total): ?>
  <p class="empty">no numbers yet &mdash; let it crawl_</p>
<?php else: ?>
  <div class="tiles">
    <?php
    tile(number_format($collected), 'finds collected');
    tile(number_format($total), 'published');
    if ($crawled > 0) tile(number_format($crawled), 'domains crawled');
    tile(number_format($domains), 'domains with finds');
    tile(number_format($scoreMax), 'top score');
    tile($avgScore, 'avg score');
    tile($seenMax > 1 ? $seenMax . '×' : '1×', 'most widespread', $topSeenFind && $seenMax > 1 ? 'on ' . $seenMax . ' sites' : 'all unique so far');
    tile(number_format($chars), 'characters preserved');
    ?>
  </div>

  <div class="stats">
    <h3>&gt; finds by type</h3>
    <?php $mk = max($byKind); foreach ($byKind as $k => $c): stat_bar(KIND_LABELS[$k] ?? $k, $c, $mk); endforeach; ?>

    <h3>&gt; finds by TLD</h3>
    <?php $mt = max($topTld); foreach ($topTld as $t => $c): stat_bar($t, $c, $mt); endforeach; ?>

<?php if ($byTag): ?>
    <h3>&gt; finds by tag</h3>
    <?php $mg = max($byTag); foreach ($byTag as $t => $c): stat_bar($t, $c, $mg); endforeach; ?>
<?php endif; ?>

    <h3>&gt; score distribution</h3>
    <?php $mdx = max($dist); foreach ($dist as $band => $c): stat_bar($band, $c, $mdx); endforeach; ?>

    <h3>&gt; top domains</h3>
    <?php $md = max($topDomains); foreach ($topDomains as $d => $c): stat_bar($d, $c, $md, 'https://' . $d, 'open ' . $d); endforeach; ?>

<?php if ($topSeen): ?>
    <h3>&gt; most widespread finds</h3>
    <?php $ms = max($topSeen); foreach ($topSeen as $id => $c):
        $f = $fById[$id] ?? null; if (!$f) continue;
        $lbl = '[' . (KIND_LABELS[$f['kind']] ?? $f['kind']) . '] ' . find_snippet($f, 52);
        stat_bar($lbl, $c, $ms, '/find/' . $id, 'seen on ' . $c . ' sites');
    endforeach; ?>
<?php endif; ?>

    <h3>&gt; finds by month</h3>
    <?php $mm = max($byMonth); foreach ($byMonth as $mo => $c): stat_bar($mo, $c, $mm); endforeach; ?>

    <h3>&gt; total over time</h3>
    <?php $mc = max($cum); foreach ($cum as $mo => $c): stat_bar($mo, $c, $mc); endforeach; ?>

<?php if ($topLiked): ?>
    <h3>&gt; most-liked finds</h3>
    <?php $ml = max($topLiked); foreach ($topLiked as $id => $c):
        $f = $fById[$id] ?? null; if (!$f) continue;
        $lbl = '[' . (KIND_LABELS[$f['kind']] ?? $f['kind']) . '] ' . find_snippet($f, 52);
        stat_bar($lbl, $c, $ml, '/find/' . $id, 'see this find');
    endforeach; ?>
<?php endif; ?>
  </div>
<?php endif; ?>
<?php require __DIR__ . '/partials/foot.php'; ?>
