<?php
$title = $title ?? 'v0rt';
$nav   = $nav ?? 'home';
// A page may preset $cmd (already HTML-safe) to show its own terminal line;
// otherwise derive it from the nav.
$cmd   = $cmd ?? ($nav === 'about' ? '--about' : ($nav === 'stats' ? '--stats' : ($nav === 'archive' ? '--all' : '--show-loot')));
// Open Graph / Twitter-card defaults; a page (e.g. find.php) may set these
// before requiring this partial to get its own share preview.
$site_desc = 'v0rt - a crawler that roams the open web collecting the human things hidden in page source: jokes, confessions, ASCII art and Easter eggs left in HTML comments, console logs and response headers.';
$og_title = $og_title ?? 'v0rt // human things hiding in the source of the open web';
$og_desc  = $og_desc  ?? $site_desc;
$og_url   = $og_url   ?? current_url();
$og_image = $og_image ?? (base_url() . '/assets/og.png');
$og_type  = $og_type  ?? 'website';
$cfg = cfg();
?><!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<!--
   you view-sourced a site about view-sourcing.
   of course you did. welcome, fellow digger.

   everything v0rt collects out there, it hides a little of in here:
   this comment (you found it), a hello waiting in the console, a couple
   of silly response headers, a /humans.txt and a /robots.txt with
   opinions. go look. then go leave your own, somewhere out there.

   the web is still handmade if you know where to look.
                                                      love, v0rt
-->
<title><?= h($title) ?></title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="<?= h($og_desc) ?>">
<meta name="robots" content="<?= h($robots ?? 'index,follow') ?>">
<?php if (!empty($cfg['gsc_verify'])): ?>
<meta name="google-site-verification" content="<?= h($cfg['gsc_verify']) ?>">
<?php endif; ?>
<link rel="canonical" href="<?= h($og_url) ?>">
<meta property="og:site_name" content="v0rt">
<meta property="og:type" content="<?= h($og_type) ?>">
<meta property="og:title" content="<?= h($og_title) ?>">
<meta property="og:description" content="<?= h($og_desc) ?>">
<meta property="og:url" content="<?= h($og_url) ?>">
<meta property="og:image" content="<?= h($og_image) ?>">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="<?= h($og_title) ?>">
<meta name="twitter:description" content="<?= h($og_desc) ?>">
<meta name="twitter:image" content="<?= h($og_image) ?>">
<!-- Real icon files (a data: URI works on desktop but mobile browsers and
     Google Search/Search Console won't use it - they need crawlable files). -->
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="manifest" href="/site.webmanifest">
<link rel="alternate" type="application/rss+xml" title="v0rt // latest loot" href="/feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/assets/style.css">
<script type="application/ld+json"><?= json_encode([
  '@context' => 'https://schema.org', '@type' => 'WebSite',
  'name' => 'v0rt', 'url' => base_url() . '/', 'description' => $site_desc,
], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?></script>
<?php if (!empty($jsonld)): ?>
<script type="application/ld+json"><?= json_encode($jsonld, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?></script>
<?php endif; ?>
<?php if (!empty($cfg['ga_id'])): ?>
<script async src="https://www.googletagmanager.com/gtag/js?id=<?= h($cfg['ga_id']) ?>"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','<?= h($cfg['ga_id']) ?>');</script>
<?php endif; ?>
<!-- Cloudflare Web Analytics --><script type="module" src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='{"token": "3d8c2c246f834dc0a996c0894e7cbacd"}'></script><!-- End Cloudflare Web Analytics -->
</head>
<body>
<div class="sweep"></div>
<div id="boot"><pre id="bootlog"></pre></div>
<div class="wrap">
  <div class="term">
    <div class="titlebar"><span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>
      <span class="tt">root@v0rt: ~/finds &mdash; 80x24</span></div>
    <div class="termbody">
      <div class="line"><span style="color:var(--green)">visitor@v0rt</span>:<span style="color:var(--cyan)">~</span>$ <span class="typed">./v0rt <?= $cmd ?></span></div>
      <h1 class="glitch" data-text="v0rt.com">v0rt.com</h1>
      <p class="sub">// human things hiding in the source of the open web</p>
      <nav class="nav">
        <a class="<?= $nav === 'home' ? 'on' : '' ?>" href="/">[ loot ]</a>
        <a class="<?= $nav === 'search' ? 'on' : '' ?>" href="/search">[ search ]</a>
        <a class="<?= $nav === 'archive' ? 'on' : '' ?>" href="/archive">[ archive ]</a>
        <a class="<?= $nav === 'stats' ? 'on' : '' ?>" href="/stats.php">[ stats ]</a>
        <a class="<?= $nav === 'about' ? 'on' : '' ?>" href="/about.php">[ about ]</a>
      </nav>
    </div>
  </div>
