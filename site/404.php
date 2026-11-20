<?php
require __DIR__ . '/lib.php';
http_response_code(404);

// The path the visitor tried to reach, sanitised for display. Apache serves
// this document for any missing URL (ErrorDocument 404), so REQUEST_URI is the
// original path. Keep it short, drop the query string, and HTML-escape it -
// it's echoed into the page and must never carry markup.
$raw  = (string)($_SERVER['REQUEST_URI'] ?? '/');
$raw  = explode('?', $raw, 2)[0];
if (strlen($raw) > 80) $raw = substr($raw, 0, 80) . '...';
$path = h($raw);

$title   = '404 // not here — v0rt';
$nav     = '404';
$cmd     = '--follow ' . $path;   // already HTML-safe
$og_desc = 'There is nothing collected at this path. A few things that do exist: the loot wall, the archive, the numbers.';
require __DIR__ . '/partials/head.php';

// Nothing lives here - but something does, elsewhere. Pull one real find so a
// lost visitor still leaves with a little of what v0rt is for.
$all  = load_finds();
$pick = $all ? $all[array_rand($all)] : null;
?>
  <!--
     you took a wrong turn and still went looking at the source.
     good instinct. there's nothing at this address, but the web is
     mostly doors that go nowhere - that's half of why it's worth
     digging through at all.                                  - v0rt
                                                                \||||||/
  -->
  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">ls <?= $path ?></span></h2>
  <p class="stat" style="color:var(--amber)">ls: cannot access '<?= $path ?>': No such file or directory</p>

  <div class="prose">
    <p><b>404.</b> Nothing was ever collected at this path &mdash; it was never
       crawled, or it's long gone. Either way, this door opens onto an empty room.</p>
    <p class="sub" style="margin:0 0 4px">// you can always find your way back:</p>
    <p style="margin:0 0 20px">
      <a href="/">&gt;&gt; the loot wall</a> &nbsp;&middot;&nbsp;
      <a href="/archive">&gt;&gt; the full archive</a> &nbsp;&middot;&nbsp;
      <a href="/stats.php">&gt;&gt; the numbers</a> &nbsp;&middot;&nbsp;
      <a href="/about.php">&gt;&gt; what this is</a>
    </p>
  </div>

<?php if ($pick): ?>
  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">cat $(ls | shuf -n1)</span></h2>
  <p class="stat">// but since you're here &mdash; one thing that <em>does</em> exist:</p>
  <div class="grid">
    <?php
      $lk = like_counts([$pick['id'] ?? '']);
      echo render_card($pick, $lk[$pick['id'] ?? ''] ?? 0);
    ?>
  </div>
<?php endif; ?>

<?php require __DIR__ . '/partials/foot.php'; ?>
