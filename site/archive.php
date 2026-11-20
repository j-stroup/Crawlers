<?php
// The full archive: every published find, newest first, server-paginated so
// each page loads only a slice (nothing lost, nothing heavy).
require __DIR__ . '/lib.php';

$all = load_finds();
usort($all, function ($a, $b) { return strcmp($b['ts'] ?? '', $a['ts'] ?? ''); }); // newest first

$per   = 60;
$total = count($all);
$pages = max(1, (int) ceil($total / $per));
$page  = isset($_GET['p']) ? (int) $_GET['p'] : 1;
if ($page < 1) $page = 1;
if ($page > $pages) $page = $pages;
$slice = array_slice($all, ($page - 1) * $per, $per);
$likes = like_counts(array_map(function ($f) { return $f['id']; }, $slice));

function arch_pager($page, $pages) {
    if ($pages < 2) return;
    echo '<nav class="pager">';
    echo $page > 1 ? '<a href="/archive/' . ($page - 1) . '">&laquo; newer</a>'
                   : '<span class="off">&laquo; newer</span>';
    echo '<span class="pg">page ' . $page . ' / ' . $pages . '</span>';
    echo $page < $pages ? '<a href="/archive/' . ($page + 1) . '">older &raquo;</a>'
                        : '<span class="off">older &raquo;</span>';
    echo '</nav>';
}

$title    = 'v0rt archive &mdash; every find (page ' . $page . ')';
$nav      = 'archive';
$og_title = 'v0rt // the full archive';
$og_desc  = 'The complete archive of every human artifact v0rt has collected from the open web, newest first.';
require __DIR__ . '/partials/head.php';
?>
  <p class="stat"><a href="/" style="color:var(--green2);text-decoration:none">&laquo; home</a>
     &nbsp;/&nbsp; the full archive &mdash; <b><?= number_format($total) ?></b> published, newest first<span class="cursor">&nbsp;</span></p>

  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">./loot --all | less</span></h2>

<?php if (!$total): ?>
  <p class="empty">no loot yet &mdash; let it crawl_</p>
<?php else: ?>
  <?php arch_pager($page, $pages); ?>
  <div class="grid" id="grid">
<?php foreach ($slice as $f): echo render_card($f, $likes[$f['id']] ?? 0); endforeach; ?>
  </div>
  <?php arch_pager($page, $pages); ?>
<?php endif; ?>
<?php require __DIR__ . '/partials/foot.php'; ?>
