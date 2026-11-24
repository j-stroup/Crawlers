<?php
require __DIR__ . '/lib.php';

$all = load_finds();
// best score first, then newest
usort($all, function ($a, $b) {
    return ($b['score'] <=> $a['score']) ?: strcmp($b['ts'] ?? '', $a['ts'] ?? '');
});
$collected = finds_total_collected();   // true tally (everything v0rt logged)
$published = count($all);
$finds = array_slice($all, 0, 500);     // homepage = the top 500 by score

$ids    = array_map(function ($f) { return $f['id']; }, $finds);
$likes  = like_counts($ids);
$by     = [];
foreach ($finds as $f) { $by[$f['kind']] = ($by[$f['kind']] ?? 0) + 1; }
$domains = count(array_unique(array_map(function ($f) { return $f['domain']; }, $all)));
$newest  = newest_ts($all);
$last    = $newest ? substr($newest, 0, 10) : '—';
$freshIds = fresh_ids($all);
// the single freshest find, for the spotlight
$spot = null;
foreach ($all as $f) { if (!$spot || ($f['ts'] ?? '') > ($spot['ts'] ?? '')) $spot = $f; }
$hasMore = $published > count($finds);

$title = 'v0rt — hidden messages & easter eggs in website source code';
$nav   = 'home';
require __DIR__ . '/partials/head.php';
?>
  <p class="stat">[ <b><?= number_format($collected) ?></b> finds ] [ <b><?= number_format($domains) ?></b> domains ] [ latest <?= h($last) ?> ]<span class="cursor">&nbsp;</span></p>
  <p class="intro">// v0rt is a crawler that collects the hidden human messages developers leave in website source code: jokes and confessions in HTML comments, greetings in the JavaScript console, ASCII art, playful HTTP response headers like X-Clacks-Overhead, and humans.txt credits &mdash; gathered from across the open web.</p>
<?php if ($spot): ?>
  <div class="spotlight">
    <span class="sp-label">&gt;&gt; freshest catch</span>
    <span class="sp-kind">[<?= h(strtoupper(KIND_LABELS[$spot['kind']] ?? $spot['kind'])) ?>]</span>
    <span class="sp-text"><?= h(find_snippet($spot, 110)) ?></span>
    <a href="/find/<?= h($spot['id']) ?>">see it &raquo;</a>
  </div>
<?php endif; ?>

  <form class="searchbar" method="get" action="/search" role="search">
    <input name="q" type="search" placeholder="search every find &amp; the domains they're from&hellip;"
           autocomplete="off" spellcheck="false">
    <button type="submit">search</button>
  </form>

  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">./loot --browse</span></h2>

  <div class="toolbar"><span class="tlbl">filter:</span>
    <button class="chip active" type="button" data-filter="all">all <span class="cn"><?= count($finds) ?></span></button>
    <?php foreach (KIND_LABELS as $k => $label): if (empty($by[$k])) continue; ?>
      <button class="chip" type="button" data-filter="<?= h($k) ?>"><?= h($label) ?> <span class="cn"><?= (int)$by[$k] ?></span></button>
    <?php endforeach; ?>
  </div>
  <div class="toolbar"><span class="tlbl">sort:</span>
    <button class="sortbtn active" type="button" data-sort="score">score</button>
    <button class="sortbtn" type="button" data-sort="likes">likes</button>
    <button class="sortbtn" type="button" data-sort="newest">newest</button>
    <button class="sortbtn" type="button" data-sort="oldest">oldest</button>
    <span class="showing" id="showing">showing <?= count($finds) ?></span>
  </div>
<?php if ($hasMore): ?>
  <p class="archnote">// the <b>500</b> highest-scoring of <b><?= number_format($published) ?></b> published &mdash; <a href="/archive">browse the full archive &raquo;</a></p>
<?php endif; ?>

  <div class="grid" id="grid">
<?php if (!$finds): ?>
    <p class="empty">no loot yet &mdash; let it crawl_</p>
<?php else: foreach ($finds as $f): echo render_card($f, $likes[$f['id']] ?? 0, isset($freshIds[$f['id']])); endforeach; endif; ?>
  </div>
  <div class="morewrap"><button class="morebtn" id="more" type="button" hidden>load more</button></div>
<?php require __DIR__ . '/partials/foot.php'; ?>
