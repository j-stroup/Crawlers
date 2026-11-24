<?php
// Search the collection: matches a find's text OR the domain it was found on.
// Server-rendered so results are shareable (/search?q=sorry) and work with no JS.
require __DIR__ . '/lib.php';

$q = trim((string)($_GET['q'] ?? ''));
if (function_exists('mb_substr') && mb_strlen($q) > 100) $q = mb_substr($q, 0, 100);

$results = [];
if ($q !== '') {
    $find = function_exists('mb_stripos') ? 'mb_stripos' : 'stripos';
    foreach (load_finds() as $f) {
        $hay = ($f['text'] ?? '') . "\n" . ($f['domain'] ?? '');
        if ($find($hay, $q) !== false) $results[] = $f;
    }
    // best first: higher score, then newest
    usort($results, function ($a, $b) {
        $s = (int)($b['score'] ?? 0) <=> (int)($a['score'] ?? 0);
        return $s !== 0 ? $s : strcmp($b['ts'] ?? '', $a['ts'] ?? '');
    });
}

$per   = 60;
$total = count($results);
$pages = max(1, (int) ceil($total / $per));
$page  = isset($_GET['p']) ? (int) $_GET['p'] : 1;
if ($page < 1) $page = 1;
if ($page > $pages) $page = $pages;
$slice = array_slice($results, ($page - 1) * $per, $per);
$likes = like_counts(array_map(function ($f) { return $f['id']; }, $slice));

function search_pager($page, $pages, $q) {
    if ($pages < 2) return;
    $base = '/search?q=' . rawurlencode($q);
    echo '<nav class="pager">';
    echo $page > 1 ? '<a href="' . h($base . '&p=' . ($page - 1)) . '">&laquo; back</a>'
                   : '<span class="off">&laquo; back</span>';
    echo '<span class="pg">page ' . $page . ' / ' . $pages . '</span>';
    echo $page < $pages ? '<a href="' . h($base . '&p=' . ($page + 1)) . '">more &raquo;</a>'
                        : '<span class="off">more &raquo;</span>';
    echo '</nav>';
}

$nav     = 'search';
$robots  = 'noindex,follow';   // query pages: don't ask Google to index them
$cmd     = '--grep' . ($q !== '' ? ' "' . h($q) . '"' : '');
$title   = $q !== '' ? ('search: ' . $q . ' — v0rt') : 'search v0rt';
$og_title = 'v0rt // search the collected web';
$og_desc = 'Search v0rt for recurring strings, jokes and confessions — and the domains they were found on.';
require __DIR__ . '/partials/head.php';
?>
  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">grep -ri<?= $q !== '' ? ' "' . h($q) . '"' : '' ?> .</span></h2>

  <form class="searchbar" method="get" action="/search" role="search">
    <input id="q" name="q" type="search" value="<?= h($q) ?>"
           placeholder="a word, a phrase, or a domain&hellip;"
           autocomplete="off" spellcheck="false" <?= $q === '' ? 'autofocus' : '' ?>>
    <button type="submit">search</button>
  </form>

<?php if ($q === ''): ?>
  <p class="intro">Search every find by its text <b>or</b> the domain it was found on. Recurring
     strings surface the web's shared habits &mdash; try a confession, a greeting, or a site.</p>
  <p class="stat">try:
     <a href="/search?q=sorry">sorry</a> &middot;
     <a href="/search?q=hiring">hiring</a> &middot;
     <a href="/search?q=TODO">TODO</a> &middot;
     <a href="/search?q=coffee">coffee</a> &middot;
     <a href="/search?q=github.com">github.com</a> &middot;
     <a href="/search?q=%C2%AF%5C_(%E3%83%84)_%2F%C2%AF">&macr;\_(&#12484;)_/&macr;</a></p>
<?php elseif (!$total): ?>
  <p class="stat">no finds match <b><?= h($q) ?></b>. <span class="cursor">&nbsp;</span></p>
  <p class="empty">try a shorter string, a different spelling, or just a domain.</p>
<?php else: ?>
  <p class="stat"><b><?= number_format($total) ?></b> find<?= $total === 1 ? '' : 's' ?>
     match <b><?= h($q) ?></b><span class="cursor">&nbsp;</span></p>
  <?php search_pager($page, $pages, $q); ?>
  <div class="grid" id="grid">
<?php foreach ($slice as $f): echo render_card($f, $likes[$f['id']] ?? 0, false, $q); endforeach; ?>
  </div>
  <?php search_pager($page, $pages, $q); ?>
<?php endif; ?>
<?php require __DIR__ . '/partials/foot.php'; ?>
