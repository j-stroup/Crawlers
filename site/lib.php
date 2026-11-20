<?php
// Shared helpers for the v0rt public site.

// --- v0rt's own easter eggs ---------------------------------------------
// This site collects jokey response headers out in the wild, so of course
// it leaves a few of its own for anyone who thinks to read them. (v0rt's
// crawler would happily catch the X-Clacks line on its own site.)
if (!headers_sent()) {
    header('X-Clacks-Overhead: GNU Terry Pratchett');
    header('X-Powered-By: a human who still thinks the web should be weird');
    header('X-Easter-Egg: you read the headers of a site that reads headers. respect. -> /humans.txt');
    header('X-Hand: \\||||||/');
    header('X-Fingers: his fingers are here');
}

function cfg() {
    static $c = null;
    if ($c === null) $c = require __DIR__ . '/config.php';
    return $c;
}

function h($s) {
    return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8');
}

function db() {
    static $pdo = null;
    if ($pdo !== null) return $pdo;
    $c = cfg();
    @mkdir(dirname($c['db_file']), 0775, true);
    $pdo = new PDO('sqlite:' . $c['db_file']);
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $pdo->exec('PRAGMA journal_mode=WAL');
    // one row per (find, voter): the like count is just COUNT(*), and a
    // repeat like from the same voter is a harmless no-op / toggle.
    $pdo->exec('CREATE TABLE IF NOT EXISTS likes (
        find_id TEXT NOT NULL, voter TEXT NOT NULL, ts INTEGER NOT NULL,
        PRIMARY KEY (find_id, voter))');
    return $pdo;
}

// Loads+parses the redacted export once (memoized). Handles both the object
// format {total_collected, generated, finds:[...]} and the older bare array.
function finds_data() {
    static $d = null;
    if ($d !== null) return $d;
    $d = ['finds' => [], 'total_collected' => 0, 'domains_crawled' => 0, 'generated' => ''];
    $c = cfg();
    if (is_file($c['data_file'])) {
        $j = json_decode((string)file_get_contents($c['data_file']), true);
        if (is_array($j) && isset($j['finds']) && is_array($j['finds'])) {
            $d['finds'] = $j['finds'];
            $d['total_collected'] = isset($j['total_collected']) ? (int)$j['total_collected'] : count($j['finds']);
            $d['domains_crawled'] = (int)($j['domains_crawled'] ?? 0);
            $d['generated'] = $j['generated'] ?? '';
        } elseif (is_array($j)) {
            $d['finds'] = $j;                     // old bare-array format
            $d['total_collected'] = count($j);
        }
    }
    return $d;
}
function load_finds() { return finds_data()['finds']; }
function finds_total_collected() { return finds_data()['total_collected']; }
function finds_domains_crawled() { return finds_data()['domains_crawled']; }

// Newest timestamp in a set of finds (ISO strings sort lexically).
function newest_ts(array $finds) {
    $m = '';
    foreach ($finds as $f) { if (($f['ts'] ?? '') > $m) $m = $f['ts']; }
    return $m;
}

// The ids of the N most-recently-found items, as a set {id: true}. Used to
// badge the latest catch as "new" - bounded on purpose, so a crawl that
// collects everything in one short burst can't end up marking every card.
function fresh_ids(array $finds, $n = 12) {
    usort($finds, function ($a, $b) { return strcmp($b['ts'] ?? '', $a['ts'] ?? ''); });
    $out = [];
    foreach (array_slice($finds, 0, $n) as $f) {
        if (!empty($f['id'])) $out[$f['id']] = true;
    }
    return $out;
}

// A valid find id must exist in the dataset - this stops votes being cast
// against arbitrary/garbage ids.
function find_id_exists($id) {
    foreach (load_finds() as $f) {
        if (isset($f['id']) && $f['id'] === $id) return true;
    }
    return false;
}

function like_counts(array $ids) {
    if (!$ids) return [];
    $place = implode(',', array_fill(0, count($ids), '?'));
    $st = db()->prepare("SELECT find_id, COUNT(*) c FROM likes WHERE find_id IN ($place) GROUP BY find_id");
    $st->execute(array_values($ids));
    $out = [];
    foreach ($st->fetchAll(PDO::FETCH_ASSOC) as $row) $out[$row['find_id']] = (int)$row['c'];
    return $out;
}

// A coarse per-visitor fingerprint to blunt refresh-spam. NOT authentication;
// just enough that a casual reload can't inflate a count.
function voter_hash() {
    $ip = $_SERVER['REMOTE_ADDR'] ?? '0';
    $ua = $_SERVER['HTTP_USER_AGENT'] ?? '';
    return substr(hash('sha256', $ip . '|' . $ua . '|chunt-v1'), 0, 24);
}

function toggle_like($find_id) {
    $pdo = db();
    $v = voter_hash();
    $st = $pdo->prepare('SELECT 1 FROM likes WHERE find_id=? AND voter=?');
    $st->execute([$find_id, $v]);
    if ($st->fetchColumn()) {
        $pdo->prepare('DELETE FROM likes WHERE find_id=? AND voter=?')->execute([$find_id, $v]);
        $liked = false;
    } else {
        $pdo->prepare('INSERT OR IGNORE INTO likes(find_id,voter,ts) VALUES(?,?,?)')
            ->execute([$find_id, $v, time()]);
        $liked = true;
    }
    $c = $pdo->prepare('SELECT COUNT(*) FROM likes WHERE find_id=?');
    $c->execute([$find_id]);
    return ['liked' => $liked, 'count' => (int)$c->fetchColumn()];
}

const KIND_LABELS = [
    'html_comment' => 'HTML comment',
    'ascii_art'    => 'ASCII art',
    'console'      => 'console message',
    'header'       => 'jokey header',
    'humans_txt'   => 'humans.txt',
    'css_comment'  => 'CSS comment',
    'js_comment'   => 'JS comment',
    'custom_404'   => '404 page',
    'security_txt' => 'security.txt',
    'dns_txt'      => 'DNS TXT',
];

// --- URLs (absolute, for Open Graph + permalinks) ----------------------
function base_url() {
    $https = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off')
        || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https')
        || (($_SERVER['SERVER_PORT'] ?? '') == 443);
    $host = $_SERVER['HTTP_HOST'] ?? 'v0rt.com';
    return ($https ? 'https' : 'http') . '://' . $host;
}
function current_url() {
    return base_url() . ($_SERVER['REQUEST_URI'] ?? '/');
}

// --- single-find lookup + helpers --------------------------------------
function find_by_id($id) {
    foreach (load_finds() as $f) {
        if (($f['id'] ?? null) === $id) return $f;
    }
    return null;
}

// A one-line, trimmed snippet of a find's text, for meta descriptions.
function find_snippet($f, $max = 180) {
    $t = preg_replace('/\s+/', ' ', trim((string)($f['text'] ?? '')));
    if (function_exists('mb_substr')) {
        if (mb_strlen($t) > $max) $t = mb_substr($t, 0, $max - 1) . "\xE2\x80\xA6";
    } elseif (strlen($t) > $max) {
        $t = substr($t, 0, $max - 1) . '...';
    }
    return $t;
}

// Shared find-card markup, used by both the gallery and the single-find page.
function render_card($f, $lk, $is_new = false) {
    $kind  = $f['kind'] ?? 'html_comment';
    $label = strtoupper(KIND_LABELS[$kind] ?? $kind);
    $id    = $f['id'] ?? '';
    ob_start(); ?>
    <div class="card k-<?= h($kind) ?>" data-kind="<?= h($kind) ?>"
         data-score="<?= (int)($f['score'] ?? 0) ?>" data-ts="<?= h($f['ts'] ?? '') ?>"
         data-likes="<?= (int)$lk ?>" data-id="<?= h($id) ?>">
      <div class="meta"><span class="kind">[<?= h($label) ?>]</span>
        <?php if ($is_new): ?><span class="tag new">new</span><?php endif; ?>
        <?php foreach (($f['tags'] ?? []) as $t): ?><span class="tag"><?= h($t) ?></span><?php endforeach; ?>
        <span class="score">::<?= (int)($f['score'] ?? 0) ?></span></div>
      <pre><?= h($f['text'] ?? '') ?></pre>
      <div class="src"><span class="arr">&gt;&gt;</span>
        <a href="<?= h($f['url'] ?? '#') ?>" target="_blank" rel="noopener noreferrer nofollow"><?= h($f['domain'] ?? '') ?></a>
        <?php $seen = (int)($f['seen_on'] ?? 1); if ($seen > 1): ?><span class="seen">seen on <?= $seen ?> sites</span><?php endif; ?>
        <button class="like" type="button" data-id="<?= h($id) ?>">&#9650; <span class="lc"><?= (int)$lk ?></span></button>
        <button class="permalink" type="button" data-id="<?= h($id) ?>" title="copy a link to this find">link</button>
        <span class="when">found <?= h(str_replace('T', ' ', substr($f['ts'] ?? '', 0, 16))) ?></span>
      </div>
    </div>
<?php return ob_get_clean();
}
