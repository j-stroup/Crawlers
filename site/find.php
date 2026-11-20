<?php
// A single find on its own permalinked, shareable page (with its own
// Open Graph card). Reached pretty as /find/<id> via .htaccess, or
// directly as /find.php?id=<id>.
require __DIR__ . '/lib.php';

$id = $_GET['id'] ?? '';
$f  = preg_match('/^[a-f0-9]{8,32}$/', $id) ? find_by_id($id) : null;

if (!$f) {
    http_response_code(404);
    $title    = 'v0rt // not found';
    $og_title = 'v0rt // find not found';
    $nav      = 'home';
    require __DIR__ . '/partials/head.php';
    ?>
    <p class="stat">[ <b>404</b> ] that find isn't here &mdash; it may have scrolled out of the top finds on a refresh.<span class="cursor">&nbsp;</span></p>
    <p><a class="morebtn" style="display:inline-block" href="/">&laquo; back to all loot</a></p>
    <?php
    require __DIR__ . '/partials/foot.php';
    exit;
}

$lk    = like_counts([$f['id']])[$f['id']] ?? 0;
$label = KIND_LABELS[$f['kind']] ?? $f['kind'];

$dom      = $f['domain'] ?? 'the open web';
$title    = ucfirst($label) . ' from ' . $dom . ' — v0rt';
$nav      = 'home';
$og_title = 'v0rt // a ' . $label . ' from ' . $dom;
$og_desc  = find_snippet($f);
$og_url   = base_url() . '/find/' . $f['id'];
$og_type  = 'article';
$jsonld   = [
    '@context'    => 'https://schema.org',
    '@type'       => 'CreativeWork',
    'name'        => ucfirst($label) . ' from ' . $dom,
    'genre'       => $label,
    'url'         => $og_url,
    'text'        => find_snippet($f, 300),
    'dateCreated' => $f['ts'] ?? '',
    'isPartOf'    => ['@type' => 'WebSite', 'name' => 'v0rt', 'url' => base_url() . '/'],
];
require __DIR__ . '/partials/head.php';
?>
  <p class="stat"><a href="/" style="color:var(--green2);text-decoration:none">&laquo; all loot</a>
     &nbsp;/&nbsp; one find, permalinked<span class="cursor">&nbsp;</span></p>
<?php $freshIds = fresh_ids(load_finds()); ?>
  <div class="grid" id="grid">
<?= render_card($f, $lk, isset($freshIds[$f['id']])) ?>
  </div>
<?php require __DIR__ . '/partials/foot.php'; ?>
