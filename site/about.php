<?php
require __DIR__ . '/lib.php';
$title   = 'About v0rt — a crawler for the web\'s hidden human artifacts';
$nav     = 'about';
$og_desc = 'How v0rt works: a gentle crawler that collects jokes, confessions, ASCII art and easter eggs from HTML comments, console logs, response headers and humans.txt across the open web.';
require __DIR__ . '/partials/head.php';
?>
  <h2><span class="pr">root@v0rt</span><span class="pa">:~/finds</span># <span class="cmd">cat README</span></h2>
  <div class="prose">
    <p>Most of the web is written by people, and people leave traces of themselves
       in the parts no visitor is meant to see &mdash; the source. A joke in an
       HTML comment. A note of apology above a horrible hack. An ASCII-art banner.
       A "we're hiring" tucked into a <code>console.log</code>. A memorial baked
       into a response header. <b>v0rt</b> is a crawler that wanders the open web
       collecting those little human artifacts and puts them on one wall.</p>

    <h3>&gt; what it looks for</h3>
    <p>Hand-written HTML comments (jokes, confessions, TODOs, hiring notes,
       "if you're reading this..."), ASCII art and kaomoji, deliberate
       <code>console</code> greetings, playful response headers (the famous
       <code>X-Clacks-Overhead</code>), and <code>humans.txt</code> credits.
       The hard part isn't finding comments &mdash; it's that ~99% are
       boilerplate, so each candidate is scored and only the ones that read like
       a human wrote them on purpose make it here, at least in theory.</p>

    <h3>&gt; how it behaves</h3>
    <p>It crawls <b>gently</b>: one page at a time, with a pause between requests,
       and it <b>respects <code>robots.txt</code></b> everywhere it goes. It mostly
       just peeks at a site's homepage &mdash; the finds cluster there &mdash; and
       moves on to the next domain. It is run periodically by hand, not constantly.</p>

    <h3>&gt; what it won't do</h3>
    <p>This is a celebration of the web's humanity, never a place to expose anything
       sensitive. Before anything is published, emails are masked to their domain,
       IP addresses are stripped, and <b>any find that contained a real credential
       or key is dropped entirely</b> &mdash; we don't spotlight someone's accidental
       leak, even redacted. Every find links back to its source, and if it's your
       site and you'd rather not be here, say the word and it's removed.</p>

    <h3>&gt; the likes</h3>
    <p>Hit the <span style="color:var(--green)">&#9650;</span> on any find you enjoy.
       It's just for fun &mdash; a way to float the best discoveries to the top
       (sort by <b>likes</b> on the loot page).</p>

    <h3>&gt; yes, but why?</h3>
    <p>Well, because I like building crawlers and watching them move from page to page,
       site to site. I've built dozens of different crawlers, all with different
       intentions. This one felt like something worth sharing. I hope you enjoy it.</p>
  </div>
<?php require __DIR__ . '/partials/foot.php'; ?>
