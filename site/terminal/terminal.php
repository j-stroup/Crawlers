<?php
session_start();

error_reporting(0);
ini_set('display_errors', 0);

// --- 1. THE LOGGER (Updated to ignore UI updates) ---
function write_to_system_log($user, $command) {
    // List of commands that the JavaScript runs automatically
    $ignoreList = ['pwd', 'whoami'];
    
    if (in_array($command, $ignoreList) || empty($command)) {
        return; // Don't log these!
    }

    $logFile = __DIR__ . '/logs/system.log';
    $timestamp = date('Y-m-d H:i:s');
    $logEntry = "[$timestamp] USER: $user | CMD: $command" . PHP_EOL;

    file_put_contents($logFile, $logEntry, FILE_APPEND);
}

// Safe arithmetic evaluator for `calc` - a tiny recursive-descent parser so we
// never eval() user input. Handles + - * / ( ) decimals and unary minus;
// returns null on any error (bad syntax, division by zero, trailing junk).
function safe_calc($expr) {
    $expr = preg_replace('/\s+/', '', (string)$expr);
    if ($expr === '' || !preg_match('#^[0-9+\-*/().]+$#', $expr)) return null;
    $pos = 0; $len = strlen($expr);
    $peek = function () use (&$pos, $expr, $len) { return $pos < $len ? $expr[$pos] : ''; };
    $parseExpr = $parseTerm = $parseFactor = null;
    $parseFactor = function () use (&$pos, $expr, $len, &$parseExpr, &$parseFactor, $peek) {
        $c = $peek();
        if ($c === '-') { $pos++; $v = $parseFactor(); return $v === null ? null : -$v; }
        if ($c === '+') { $pos++; return $parseFactor(); }
        if ($c === '(') { $pos++; $v = $parseExpr(); if ($peek() !== ')') return null; $pos++; return $v; }
        $start = $pos;
        while ($pos < $len && (ctype_digit($expr[$pos]) || $expr[$pos] === '.')) $pos++;
        if ($pos === $start) return null;
        $num = substr($expr, $start, $pos - $start);
        return is_numeric($num) ? $num + 0 : null;
    };
    $parseTerm = function () use (&$pos, &$parseFactor, $peek) {
        $v = $parseFactor(); if ($v === null) return null;
        while (($c = $peek()) === '*' || $c === '/') {
            $pos++; $r = $parseFactor(); if ($r === null) return null;
            if ($c === '/') { if ($r == 0) return null; $v = $v / $r; } else { $v = $v * $r; }
        }
        return $v;
    };
    $parseExpr = function () use (&$pos, &$parseTerm, $peek) {
        $v = $parseTerm(); if ($v === null) return null;
        while (($c = $peek()) === '+' || $c === '-') {
            $pos++; $r = $parseTerm(); if ($r === null) return null;
            $v = ($c === '+') ? $v + $r : $v - $r;
        }
        return $v;
    };
    $result = $parseExpr();
    return ($result === null || $pos !== $len) ? null : $result;
}

$cmdInput = isset($_GET['c']) ? trim((string)base64_decode($_GET['c'], true)) : (isset($_GET['cmd']) ? trim($_GET['cmd']) : '');
$currentUser = $_SESSION['user'] ?? 'guest';

// Call the logger
write_to_system_log($currentUser, $cmdInput);

// A mark left in the margins, for anyone who reads headers. (worm-world)
if (!headers_sent()) {
    header('X-Hand: \\||||||/');
    header('X-Fingers: his fingers are here');
}

// 1. Expanded File System Initialization
if (!isset($_SESSION['fs'])) {
    $_SESSION['fs'] = [
        'root' => [
            'bin' => ['bash' => 'BIN', 'ls' => 'BIN', 'su' => 'BIN', 'apt' => 'BIN', 'rm' => 'BIN'],
            'boot' => [
                'vmlinuz-5.15.0-generic' => 'KERNEL_IMG',
                'grub' => ['grub.cfg' => 'Boot configuration loaded.']
            ],
            'dev' => ['null' => 'DEV', 'random' => 'DEV', 'sda1' => 'BLOCK_DEV', 'tty0' => 'CHAR_DEV'],
            'etc' => [
                'shadow' => 'root:$6$78291... (Permission Denied)', 
                'hostname' => 'core-node-01',
                'motd' => "UNAUTHORIZED ACCESS PROHIBITED - LOGS ARE BEING MONITORED",
                'network' => ['interfaces' => 'auto lo\niface lo inet loopback'],
                'passwd' => "root:x:0:0:root:/root:/bin/bash\n" .
                            "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n" .
                            "bin:x:2:2:bin:/bin:/usr/sbin/nologin\n" .
                            "sys:x:3:3:sys:/dev:/usr/sbin/nologin\n" .
                            "guest:x:1000:1000:guest,,,:/home/guest:/bin/bash\n" .
                            "admin:x:1001:1001:System Admin,,,:/home/admin:/bin/bash",
              ],
            'home' => [
                'guest' => [
                    'readme.txt' => "System uptime is 99.9%. Report bugs to sysadmin@local.",
                    'projects' => [
                        'web_terminal' => ['index.php' => 'PHP Terminal Source v1.0', 'style.css' => 'Green on Black theme.'],
                        'bot_script.py' => 'import time\nwhile True:\n  print("Running...")'
                    ],
                    'downloads' => ['patch_v4.2.tar.gz' => 'BINARY_DATA_STREAM']
                ],
                'admin' => [
                    'private_key.pem' => "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7...",
                    'reminders.txt' => "1. Change root password.\n2. Fix the leak in /var/tmp."
                ]
            ],
            'mnt' => ['external_disk' => []],
            'opt' => ['custom_app' => ['binary' => 'EXEC', 'config.json' => '{"version": "1.0.4"}']],
            'root' => [
                '.bash_history' => "apt update\nrm -rf /tmp/*\ncat /etc/shadow",
                'classified' => ['top_secret.txt' => 'The system is actually running on a toaster.']
            ],
            'usr' => [
                'share' => ['doc' => [], 'man' => []],
                'local' => ['bin' => [], 'lib' => []]
            ],
            'var' => [
                'log' => [
                    'auth.log' => "May 12 20:10:01 sshd: session opened for user guest",
                    'syslog' => "System health: OK",
                    'apache2' => ['access.log' => '127.0.0.1 - - [13/May/2026] "GET / HTTP/1.1" 200']
                ],
                'tmp' => ['session_732' => 'TEMP_DATA', 'crash_dump' => 'CORE_DUMP_0x00FF']
            ]
        ]
    ];
}
if (!isset($_SESSION['cwd'])) $_SESSION['cwd'] = ['root', 'home', 'guest'];
if (!isset($_SESSION['user'])) $_SESSION['user'] = 'guest';

// Doorways: a folder you can `cd` into, but instead of entering it the shell
// opens the site (see the cd case + the OPEN_URL handler in terminal.js).
// The operator's own sites live in ~/projects/websites/. The realm is NOT one
// of them -- it's mounted in /mnt, where nothing should be, by something else.
$projectLinks = [
    'wormmanhasfingers'      => 'https://wormmanhasfingers.com',
    'manableed.com'          => 'https://manableed.com',
    'stroupphoto.com'        => 'https://stroupphoto.com',
    'archtrave.com'          => 'https://archtrave.com',
    'pixelscry.com'          => 'https://pixelscry.com',
    'scarabhold.com'         => 'https://scarabhold.com',
    'trashlowlifescum.com'   => 'https://trashlowlifescum.com',
    'aionsecurity.io'        => 'https://aionsecurity.io',
    'abandonedcleveland.com' => 'https://abandonedcleveland.com',
    'neowineries.com'        => 'https://neowineries.com',
];
$sites = ['manableed.com', 'stroupphoto.com', 'archtrave.com', 'pixelscry.com',
          'scarabhold.com', 'trashlowlifescum.com', 'aionsecurity.io',
          'abandonedcleveland.com', 'neowineries.com'];
$web = &$_SESSION['fs']['root']['home']['guest']['projects']['websites'];
if (!is_array($web)) $web = [];
foreach ($sites as $s) {
    if (!isset($web[$s])) $web[$s] = [];
}
unset($web);
// tidy up: the realm used to sit in the home dir; it belongs apart now
unset($_SESSION['fs']['root']['home']['guest']['wormmanhasfingers']);
// the realm, mounted where nothing should be -- not his, not a website
if (isset($_SESSION['fs']['root']['mnt']) && is_array($_SESSION['fs']['root']['mnt'])
    && !isset($_SESSION['fs']['root']['mnt']['wormmanhasfingers'])) {
    $_SESSION['fs']['root']['mnt']['wormmanhasfingers'] = [];
}

// --- the first finger: traces the operator left behind. Discoverable by
// poking around (ls -a / cat), never required. Tone + canon: worm-world BIBLE. ---
$home = &$_SESSION['fs']['root']['home']['guest'];
if (!isset($home['todo.txt'])) {
    $home['todo.txt'] =
        "- rotate the API key (it's in .env, I KNOW, I'll move it)\n"
      . "- the crawler keeps finding things it shouldn't. redact harder.\n"
      . "- stop reading the logs at 3am\n"
      . "- reply to the email. you know the one.\n"
      . "- if you're reading this: hi. you weren't supposed to get this far.";
}
// The lone diary became scattered notes - a dev circling a thing he can't
// name. Clear the old single file for any returning session.
unset($home['diary.txt']);
if (!isset($home['notes.txt'])) {
    $home['notes.txt'] =
        "day 73 - it's in the logs again. 3am. always 3am. i don't schedule\n"
      . "anything for 3am.\n\n"
      . "day 88 - the crawler pulled a page that linked a page that didn't\n"
      . "exist. went back to look. gone. of course it's gone.\n\n"
      . "day 1?? - lost count. stopped counting. the counting is the problem\n"
      . "anyway.\n\n"
      . "- it is not a bug. i keep calling it a bug. it is not a bug.\n"
      . "- half these finds i don't remember collecting.\n"
      . "- something pinged the box from an address i don't recognize.\n"
      . "  probably nothing. probably you.\n\n"
      . "i tell myself i'm preserving something. some nights i'm just reading\n"
      . "other people's mail.\n\n"
      . "        \\||||||/\n\n"
      . "i keep drawing it. i don't know what it is or why my hand does it.\n"
      . "(more in ./notes, if you're the type. you're the type.)";
}
if (!isset($home['notes'])) {
    $home['notes'] = [
        'theories.txt' =>
            "THEORIES (none of them good)\n\n"
          . "1. it's a process that was never started.\n"
          . "2. it's the gaps between the finds -- the thing i'm NOT collecting.\n"
          . "3. it's old. older than the web. the web just gave it fingers.\n"
          . "4. it's me. (crossed this out. wrote it again.)\n"
          . "5. it lives in /mnt. i did not mount /mnt.\n\n"
          . "i don't want any of these to be true.\n"
          . "i want all of them to be true.\n"
          . "i want to KNOW.",
        'count.txt' =>
            "the count is never the same twice.\n\n"
          . "six. then six. then seven. i counted seven. went back. six.\n\n"
          . "HOW MANY FINGERS. that's the whole question. that's the only\n"
          . "question. that's the question you don't get to ask him.",
        'the_hall.txt' =>
            "there's a door in /mnt i didn't put there.\n\n"
          . "did not open it. will not open it.\n\n"
          . "(opened it.)\n\n"
          . "don't go to the end of the hall. i'm not telling you.\n"
          . "i'm telling me.",
    ];
}
if (!isset($home['.whatisit'])) {
    $home['.whatisit'] =
        "i know how this looks. a grown man. a crawler. a folder of notes\n"
      . "about a thing that doesn't have a name.\n\n"
      . "but you've read this far too, haven't you.\n\n"
      . "so. how many fingers did YOU count.";
}
if (!isset($home['.bash_history'])) {
    $home['.bash_history'] =
        "python3 crawl.py --resume\n"
      . "tail -f logs/finds.log\n"
      . "rm finds_backup.jsonl      # ugh. didn't mean to.\n"
      . "cat notes.txt\n"
      . "ls notes/\n"
      . "vim notes/the_hall.txt\n"
      . "who\n"
      . "last\n"
      . "cat /var/log/syslog\n"
      . "clear\n"
      . "ls -a\n";
}
if (!isset($home['.env'])) {
    $home['.env'] =
        "V0RT_DB=sqlite:///var/v0rt/finds.db\n"
      . "API_KEY=sk_live_****************  # rotated. relax.\n"
      . "ADMIN_EMAIL=                      # left blank on purpose\n"
      . "# note to self: stop keeping secrets in files you can read.";
}
if (!isset($home['.reminder'])) {
    $home['.reminder'] =
        "root pw = oatmeal-cookie\n"
      . "(CHANGE THIS. you always say that.)";
}
unset($home);
$rootdir = &$_SESSION['fs']['root']['root'];
if (is_array($rootdir) && !isset($rootdir['README_IF_FOUND.txt'])) {
    $rootdir['README_IF_FOUND.txt'] =
        "If you're reading this as root, you either know me or you broke in.\n"
      . "Either way -- the finds are in ~/v0rt. Be gentle with them.\n"
      . "They were people once.\n\n"
      . "And close the door on your way out.";
}
unset($rootdir);
// a scrawl left somewhere that forgets things -- fitting
if (isset($_SESSION['fs']['root']['var']['tmp']) && is_array($_SESSION['fs']['root']['var']['tmp'])
    && !isset($_SESSION['fs']['root']['var']['tmp']['.note'])) {
    $_SESSION['fs']['root']['var']['tmp']['.note'] =
        "(in /var/tmp because nothing here is meant to last anyway)\n\n"
      . "it answered once. i don't remember what i asked.\n"
      . "i remember the answer had the wrong number of fingers.";
}

// Helper for FS navigation
function &get_dir_ref(&$fs, $path) {
    $curr = &$fs;
    foreach ($path as $dir) {
        if (isset($curr[$dir])) {
            $curr = &$curr[$dir];
        }
    }
    return $curr;
}
// Resolve a cwd-style path array to its node (dir array / file string), or
// null if any segment is missing. Non-mutating; powers multi-level + ../.. cd.
function fs_node($fs, $path) {
    $cur = $fs;
    foreach ($path as $seg) {
        if (is_array($cur) && isset($cur[$seg])) $cur = $cur[$seg];
        else return null;
    }
    return $cur;
}
// The real display path for the cwd. The first cwd element is the filesystem
// root (stored under the key 'root'), so drop it: home shows as /home/guest,
// the fs root as /, etc. - not the old /root/home/guest.
function cwd_path() {
    $p = $_SESSION['cwd'];
    array_shift($p);
    return '/' . implode('/', $p);
}
$currentDir = &get_dir_ref($_SESSION['fs'], $_SESSION['cwd']);

$cmdInput = isset($_GET['c']) ? trim((string)base64_decode($_GET['c'], true)) : (isset($_GET['cmd']) ? trim($_GET['cmd']) : '');
$root_password = "oatmeal-cookie";
$parts = explode(' ', $cmdInput);
// Responses are injected as innerHTML on the client, so escape every token up
// front - any user text echoed back would otherwise be reflected XSS. Legit
// commands/filenames are alphanumeric, so this is a no-op for them.
$parts = array_map(function ($p) { return htmlspecialchars($p, ENT_QUOTES, 'UTF-8'); }, $parts);
$cmd = strtolower($parts[0]);
$arg = isset($parts[1]) ? $parts[1] : '';

$currentDir = &get_dir_ref($_SESSION['fs'], $_SESSION['cwd']);

// Recursive pretty-printer for `tree`.
function fs_tree($dir, $prefix = '') {
    $out = '';
    $n = count($dir);
    $i = 0;
    foreach ($dir as $name => $val) {
        $i++;
        $last = ($i === $n);
        $out .= $prefix . ($last ? '└── ' : '├── ') . $name . (is_array($val) ? '/' : '') . "\n";
        if (is_array($val)) {
            $out .= fs_tree($val, $prefix . ($last ? '    ' : '│   '));
        }
    }
    return $out;
}

// Recursive name search for `find`.
function fs_find($dir, $needle, $path = '') {
    $hits = [];
    foreach ($dir as $name => $val) {
        $full = $path . '/' . $name;
        if ($needle !== '' && stripos($name, $needle) !== false) $hits[] = ltrim($full, '/');
        if (is_array($val)) $hits = array_merge($hits, fs_find($val, $needle, $full));
    }
    return $hits;
}

switch ($cmd) {
    case 'uptime':
        echo " 20:37:42 up 142 days,  4:12,  1 user,  load average: 0.08, 0.03, 0.01";
        break;

    case 'df':
        echo "Filesystem     Size  Used  Avail Use% Mounted on\n";
        echo "/dev/sda1       40G   14G    24G  37% /\n";
        echo "tmpfs          797M  1.1M   796M   1% /run";
        break;

    case 'netstat':
        echo "Active Internet connections (w/o servers)\n";
        echo "Proto Recv-Q Send-Q Local Address           Foreign Address         State\n";
        echo "tcp        0      0 192.168.1.42:443       172.217.1.1:54312      ESTABLISHED";
        break;

    case 'ssh':
        if (!$arg) {
            echo "usage: ssh [user]@[host]";
        } else {
            echo "Connecting to $arg...\n";
            echo "Connection refused by remote host (Firewall Rule 403).";
        }
        break;

    case 'nmap':
        echo "Starting Nmap 7.80 ( https://nmap.org )\n";
        echo "Nmap scan report for localhost (127.0.0.1)\n";
        echo "PORT     STATE  SERVICE\n";
        echo "22/tcp   open   ssh\n";
        echo "80/tcp   open   http\n";
        echo "443/tcp  open   https";
        break;

    case 'history':
        // If you want to track history, you'd save commands to a session array
        echo "1 ls\n2 cd ..\n3 su root\n4 nmap localhost";
        break;

    case 'date':
        echo date('D M d H:i:s T Y');
        break;

    case 'sudo':
        $lecture = "";
        if (!isset($_SESSION['sudo_lectured'])) {
            $_SESSION['sudo_lectured'] = true;
            $lecture = "We trust you have received the usual lecture from the local System\n" .
                       "Administrator. It usually boils down to these three things:\n\n" .
                       "    #1) Respect the privacy of others.\n" .
                       "    #2) Think before you type.\n" .
                       "    #3) With great power comes great responsibility.\n\n";
        }

        // IMPORTANT: Join all parts after 'sudo' into one string
        $fullRequestedCmd = implode(' ', array_slice($parts, 1));
        echo $lecture . "SUDO_PASS_REQUIRED " . $fullRequestedCmd;
        break;

    case 'sudo_auth':
        $submittedPass = $arg;
        // Re-assemble the command after sudo (e.g., "cat shadow")
        $originalCmdInput = isset($parts[2]) ? implode(' ', array_slice($parts, 2)) : '';
        
        if ($submittedPass === "password123") {
            // Step 1: Temporarily elevate to root
            $previousUser = $_SESSION['user'];
            $_SESSION['user'] = 'root';

            // Step 2: Extract the sub-command and its argument
            $subParts = explode(' ', $originalCmdInput);
            $subCmd = strtolower($subParts[0]);
            $subArg = isset($subParts[1]) ? $subParts[1] : '';

            // Step 3: Trigger the logic for that specific command
            // We can just echo what the normal case would have echoed
            if ($subCmd === 'cat') {
                // Since we are now session user 'root', we can just 
                // run a internal check or a simulated response:
                if ($subArg === 'shadow' || $subArg === 'etc/shadow') {
                    echo "root:\$6\$78291\$6hX8... [DECRYPTED] : password123";
                } else {
                    // Fallback for other files
                    echo "Reading $subArg as superuser...\n";
                    // You could include more logic here if needed
                }
            } elseif ($subCmd === 'apt-get' || $subCmd === 'apt') {
                if ($subArg === 'update') {
                    echo "Hit:1 http://archive.ubuntu.com/ubuntu bookworm InRelease\n"
                       . "Hit:2 http://security.debian.org bookworm-security InRelease\n"
                       . "Reading package lists... Done\n"
                       . rand(20, 90) . " packages can be upgraded. Run 'apt list --upgradable' to see them.";
                } elseif ($subArg === 'upgrade') {
                    echo "Reading package lists... Done\nBuilding dependency tree... Done\n"
                       . "Calculating upgrade... Done\n"
                       . "The following packages will be upgraded:\n  libc6 openssl python3.11\n"
                       . "Fetching archives...\nUnpacking libc6 (2.35) over (2.34) ...\n"
                       . "Setting up libc6 (2.35) ...\nUpgrade complete. System is up to date.";
                } elseif ($subArg === 'install') {
                    $pkg = isset($subParts[2]) ? $subParts[2] : 'package';
                    echo "Reading package lists... Done\nBuilding dependency tree... Done\n"
                       . "The following NEW packages will be installed:\n  $pkg\n"
                       . "Fetching $pkg ...\nUnpacking $pkg ...\nSetting up $pkg ...\n$pkg is now installed.";
                } else {
                    echo "Reading package lists... Done\nBuilding dependency tree... Done\nAll packages are up to date.";
                }
            } else {
                echo "sudo: $subCmd: command not found";
            }

            // Step 4: Revert back to guest so you aren't root forever
            $_SESSION['user'] = $previousUser;

        } else {
            echo "sudo: 1 incorrect password attempt";
        }
        break;
    case 'rm':
        if ($arg === '-rf' || $arg === '-rf /*') {
            if ($_SESSION['user'] === 'root') {
                echo "SYSTEM_DESTROYED";
            } else {
                echo "rm: cannot remove '/': Permission denied. (Nice try, kid.)";
            }
        } else {
            // Your existing rm logic for single files
            if (isset($currentDir[$arg])) {
                unset($currentDir[$arg]);
                echo "Removed $arg";
            } else {
                echo "rm: $arg: No such file or directory";
            }
        }
        break;
    case 'reset_system':
        session_destroy();
        echo "System formatting... [OK]\nReload the page to re-initialize.";
        break;

    case 'whoami':
        echo $_SESSION['user'];
        break;

    case 'pwd':
        echo cwd_path();
        break;

case 'ls':
        $showAll = false;
        $targetKey = '';
        foreach (array_slice($parts, 1) as $p) {
            if ($p !== '' && $p[0] === '-') {
                if (strpos($p, 'a') !== false || strpos($p, 'A') !== false) $showAll = true;
            } elseif ($targetKey === '') {
                $targetKey = $p;
            }
        }
        $targetDir = $currentDir;
        if ($targetKey !== '' && isset($currentDir[$targetKey]) && is_array($currentDir[$targetKey])) {
            $targetDir = $currentDir[$targetKey];
        }
        $out = '';
        foreach ($targetDir as $key => $val) {
            if (!$showAll && $key !== '' && $key[0] === '.') continue;   // hidden unless -a
            $out .= is_array($val)
                ? "<span class='fs-directory'>$key/</span>  "
                : "<span class='fs-file'>$key</span>  ";
        }
        echo $out;
        break;

    case 'cd':
        // Full path navigation: absolute (/var/tmp), home (~, ~/notes),
        // relative, multi-level (projects/websites), and ../.. in any mix.
        if ($arg === '' || $arg === '~') { $_SESSION['cwd'] = ['root', 'home', 'guest']; break; }
        $rel = $arg;
        if (strpos($rel, '~/') === 0) { $path = ['root', 'home', 'guest']; $rel = substr($rel, 2); }
        elseif ($rel[0] === '/')      { $path = ['root']; }
        else                          { $path = $_SESSION['cwd']; }
        $cd_err = null; $cd_opened = false;
        foreach (explode('/', $rel) as $seg) {
            if ($seg === '' || $seg === '.') continue;
            if ($seg === '..') { if (count($path) > 1) array_pop($path); continue; }
            if (isset($projectLinks[$seg])) {   // a doorway: open the site, don't enter
                echo "OPEN_URL " . $projectLinks[$seg]; $cd_opened = true; break;
            }
            $node = fs_node($_SESSION['fs'], array_merge($path, [$seg]));
            if ($node === null)   { $cd_err = "bash: cd: $arg: No such file or directory"; break; }
            if (!is_array($node)) { $cd_err = "bash: cd: $arg: Not a directory"; break; }
            if ($seg === 'root' && count($path) === 1 && $_SESSION['user'] !== 'root') {
                $cd_err = "bash: cd: $arg: Permission denied"; break;
            }
            $path[] = $seg;
        }
        if ($cd_opened) break;
        if ($cd_err !== null) { echo $cd_err; break; }
        $_SESSION['cwd'] = $path;
        break;

    case 'su':
        if ($arg === 'root') {
            // This is handled by a special flag for the JS to prompt for password
            echo "AUTH_REQUIRED"; 
        } else {
            echo "su: user $arg does not exist";
        }
        break;

    case 'auth_root':
        // Secret internal command used by JS after password prompt
        if ($arg === $root_password) {
            $_SESSION['user'] = 'root';
            echo "Success: Logged in as root.";
        } else {
            echo "su: Authentication failure";
        }
        break;

    case 'exit':
        if ($_SESSION['user'] === 'root') {
            $_SESSION['user'] = 'guest';
            echo "Logged out to guest.";
        } else {
            echo "Session terminated.";
        }
        break;
    case 'uname':
        echo ($arg === '-a') 
            ? "Linux terminal-vm 5.15.0-101-generic #111-Ubuntu SMP Wed Feb 11 x86_64 GNU/Linux" 
            : "Linux";
        break;
    case 'calc':
        // calc 5+5  (safe arithmetic only - no eval; see safe_calc above)
        $expression = implode('', array_slice($parts, 1));
        $res = safe_calc($expression);
        echo ($res === null) ? "Error: Invalid expression."
                             : ($expression . " = " . (0 + $res));
        break;
    case 'grep':
        // Usage: grep "search_term" filename
        $searchTerm = str_replace(['"', "'"], '', $arg); // Basic cleanup
        $fileName = isset($parts[2]) ? $parts[2] : '';
        
        if (isset($currentDir[$fileName]) && !is_array($currentDir[$fileName])) {
            $content = $currentDir[$fileName];
            $lines = explode("\n", $content);
            $matches = array_filter($lines, function($line) use ($searchTerm) {
                return stripos($line, $searchTerm) !== false;
            });
            
            if (!empty($matches)) {
                // Highlight the match in red for extra realism
                echo str_ireplace($searchTerm, "<span style='color:red'>$searchTerm</span>", implode("\n", $matches));
            }
        } else {
            echo "grep: $fileName: No such file";
        }
        break;

    case 'cat':
        $isRoot = ($_SESSION['user'] === 'root');

        // 1. Check for Real Server Logs (system.log)
        if ($arg === '/var/log/system.log' || $arg === 'logs/system.log') {
            $logPath = __DIR__ . '/logs/system.log';
            if (file_exists($logPath)) {
                $lines = file($logPath);
                echo htmlspecialchars(implode("", array_slice($lines, -20)));
            } else {
                echo "cat: /var/log/system.log: No such file";
            }
        } 
        
        // 2. Check for Simulated Dynamic Logs (syslog)
        elseif ($arg === '/var/log/syslog' || $arg === 'syslog') {
            $events = [
                "[" . date('H:i:s', strtotime("-14 minutes")) . "] kernel: [UFW BLOCK] IN=eth0 OUT= MAC=00:...",
                "[" . date('H:i:s', strtotime("-12 minutes")) . "] systemd[1]: Starting Daily Cleanup...",
                "[" . date('H:i:s', strtotime("-9 minutes")) . "] sshd[2121]: Failed password for root from 1.2.3.4",
                "[" . date('H:i:s', strtotime("-3 minutes")) . "] kernel: \\||||||/",
                "[" . date('H:i:s') . "] sshd[" . rand(2000, 9999) . "]: accepted connection on pts/1 -- origin withheld",
                "[" . date('H:i:s') . "] CRON[4201]: (root) CMD (python3 /home/guest/bot.py)"
            ];
            echo implode("\n", $events);
        }
        // 2. Simulated Passwd File (The "Hacker" Target)
        elseif ($arg === '/etc/passwd' || $arg === 'etc/passwd' || $arg === 'passwd') {
            // We pull this from the FS array we just updated
            // Assuming $currentDir is correctly set to etc if they are there, 
            // or we can hard-reference it:
            $etc = $_SESSION['fs']['root']['etc'];
            echo htmlspecialchars($etc['passwd']);
        }
        // 3. Check for Virtual Filesystem Files
        elseif (isset($currentDir[$arg])) {
            // Check if it's a directory
            if (is_array($currentDir[$arg])) {
                echo "cat: $arg: Is a directory";
            } 
            // Handle sensitive "shadow" file
            elseif ($arg === 'shadow' || $arg === 'etc/shadow' || $arg === '/etc/shadow') {
                if ($isRoot) {
                    echo "root:\$6\$78291\$6hX8... [DECRYPTED]: oatmeal-cookie";
                } else {
                    echo "cat: $arg: Permission denied (Nice try, guest)";
                }
            } 
            // Standard file output
            else {
                echo htmlspecialchars($currentDir[$arg]);
            }
        } 
        elseif ($arg === '/etc/passwd' || $arg === 'etc/passwd' || $arg === 'passwd') {
            $etc = $_SESSION['fs']['root']['etc'] ?? []; // Fallback to empty array if etc doesn't exist
            if (isset($etc['passwd'])) {
                echo htmlspecialchars($etc['passwd']);
            } else {
                echo "cat: $arg: No such file or directory";
            }
        }
        // 4. Default: File not found
        else {
            echo "cat: $arg: No such file or directory";
        }
        break;

    case 'tail':
        $logPath = __DIR__ . '/logs/system.log';
        if ($arg === '-f' && isset($parts[2]) && strpos($parts[2], 'system.log') !== false) {
            echo "[FOLLOW MODE ACTIVE]\n";
            echo "Press Ctrl+C to exit (Simulated)\n\n";
            
            if (file_exists($logPath)) {
                $lines = file($logPath);
                // Show the last 5 entries to look like a live feed
                echo nl2br(htmlspecialchars(implode("", array_slice($lines, -5))));
            }
        } else {
            echo "usage: tail -f /var/log/system.log";
        }
        break;
    case 'man':
        if (!$arg) {
            echo "What manual page do you want?";
            break;
        }

        switch ($arg) {
            case 'ls':
                echo "LS(1)                          User Commands                         LS(1)\n\n";
                echo "NAME\n       ls - list directory contents\n\n";
                echo "SYNOPSIS\n       ls [DIRECTORY]\n\n";
                echo "DESCRIPTION\n       List information about the FILEs (the current directory by default).\n";
                echo "       Entries are sorted alphabetically. Subdirectories appear in blue.";
                break;

            case 'cd':
                echo "CD(1)                          User Commands                         CD(1)\n\n";
                echo "NAME\n       cd - change the shell working directory\n\n";
                echo "SYNOPSIS\n       cd [DIRECTORY]\n\n";
                echo "DESCRIPTION\n       Change the current directory to DEST. The default DEST is the\n";
                echo "       home directory. '..' moves to the parent directory.";
                break;

            case 'cat':
                echo "CAT(1)                         User Commands                        CAT(1)\n\n";
                echo "NAME\n       cat - concatenate files and print on the standard output\n\n";
                echo "SYNOPSIS\n       cat [FILE]\n\n";
                echo "DESCRIPTION\n       Read the content of FILE and display it in the terminal.\n";
                echo "       Warning: Cannot read directories or binary streams.";
                break;

            case 'sudo':
                echo "SUDO(8)                       System Manager                        SUDO(8)\n\n";
                echo "NAME\n       sudo - execute a command as another user\n\n";
                echo "SYNOPSIS\n       sudo [COMMAND]\n\n";
                echo "DESCRIPTION\n       sudo allows a permitted user to execute a command as the\n";
                echo "       superuser (root). Restricted usage is logged to /var/log/auth.log.";
                break;

            case 'su':
                echo "SU(1)                          User Commands                         SU(1)\n\n";
                echo "NAME\n       su - run a command with substitute user and group ID\n\n";
                echo "SYNOPSIS\n       su [USER]\n\n";
                echo "DESCRIPTION\n       Change the current session to USER. If no USER is specified,\n";
                echo "       it defaults to root and requires a password.";
                break;

            case 'rm':
                echo "RM(1)                          User Commands                         RM(1)\n\n";
                echo "NAME\n       rm - remove files or directories\n\n";
                echo "SYNOPSIS\n       rm [OPTION] [FILE]\n\n";
                echo "DESCRIPTION\n       Remove (unlink) the FILE(s). Use -rf for recursive force.\n";
                echo "       DANGER: rm -rf /* will attempt to destroy the entire system.";
                break;

            case 'ping':
                echo "PING(8)                        System Manager                       PING(8)\n\n";
                echo "NAME\n       ping - send ICMP ECHO_REQUEST to network hosts\n\n";
                echo "SYNOPSIS\n       ping [HOST]\n\n";
                echo "DESCRIPTION\n       Uses the ICMP protocol's mandatory ECHO_REQUEST datagram to\n";
                echo "       elicit an ICMP ECHO_RESPONSE from a host or gateway.";
                break;

            case 'nmap':
                echo "NMAP(1)                        User Commands                        NMAP(1)\n\n";
                echo "NAME\n       nmap - Network exploration tool and security / port scanner\n\n";
                echo "DESCRIPTION\n       Nmap is used to discover hosts and services on a computer network\n";
                echo "       by sending packets and analyzing the responses.";
                break;

            case 'whoami':
                echo "WHOAMI(1)                      User Commands                      WHOAMI(1)\n\n";
                echo "NAME\n       whoami - print effective userid\n\n";
                echo "DESCRIPTION\n       Print the user name associated with the current effective user ID.";
                break;

            case 'pwd':
                echo "PWD(1)                         User Commands                        PWD(1)\n\n";
                echo "NAME\n       pwd - print name of current/working directory\n\n";
                echo "DESCRIPTION\n       Print the full filename of the current working directory.";
                break;

            case 'clear':
                echo "CLEAR(1)                       User Commands                       CLEAR(1)\n\n";
                echo "NAME\n       clear - clear the terminal screen\n\n";
                echo "DESCRIPTION\n       clear clears your screen if this is possible, including its\n";
                echo "       scrollback buffer.";
                break;

            case 'help':
                echo "HELP(1)                        User Commands                        HELP(1)\n\n";
                echo "NAME\n       help - display information about builtin commands\n\n";
                echo "DESCRIPTION\n       Displays a brief summary of available terminal commands.";
                break;

            default:
                echo "No manual entry for " . htmlspecialchars($arg);
                break;
        }
        break;
    case 'scan':
        echo "Scanning local network (192.168.1.0/24)...\n";
        echo "Hold for discovery...\n\n";
        sleep(1);
        echo "192.168.1.1   - Gateway [Online]\n";
        echo "192.168.1.42  - localhost [Online]\n";
        echo "192.168.1.105 - Unknown Device [Online]\n";
        echo "192.168.1.200 - Backup-Server [Offline]";
        break;
    case 'touch':
        if ($arg) {
            $currentDir[$arg] = "File created " . date('H:i:s');
            echo "Created $arg";
        } else {
            echo "touch: missing file operand";
        }
        break;

    case 'mkdir':
        if ($arg) {
            $currentDir[$arg] = [];
            echo "Created directory $arg";
        }
        break;

    case 'rm':
        if (isset($currentDir[$arg])) {
            unset($currentDir[$arg]);
            echo "Removed $arg";
        } else {
            echo "rm: $arg: No such file or directory";
        }
        break;
    case 'host':
    case 'dig':
        if (!$arg) {
            echo "Usage: host <domain>";
        } else {
            $result = dns_get_record($arg, DNS_A);
            if ($result) {
                foreach ($result as $record) {
                    echo $arg . " has address " . $record['ip'] . "\n";
                }
            } else {
                echo "Host $arg not found.";
            }
        }
        break;
    case 'ps':
        echo "  PID TTY          TIME CMD\n";
        echo " 1542 pts/0    00:00:00 bash\n";
        echo " 2031 pts/0    00:00:00 ps\n";
        echo " 0842 ?        00:01:12 systemd";
        break;
    case 'weather':
        // Using a free, no-auth weather API
        $city = $arg ? $arg : "Cleveland";
        $data = @file_get_contents("https://wttr.in/" . urlencode($city) . "?0AT");
        if ($data) {
            echo $data;
        } else {
            echo "Error: Could not connect to weather service.";
        }
        break;
    case 'free':
        echo "              total        used        free      shared  buff/cache   available\n";
        echo "Mem:       16384000     4120000    12264000      250000     1100000    11800000\n";
        echo "Swap:       2048000           0     2048000";
        break;

    case 'ping':
        $target = $arg ? $arg : "8.8.8.8";
        $out = "PING $target ($target) 56(84) bytes of data.\n";
        for ($i = 1; $i <= 4; $i++) {
            $out .= "64 bytes from $target: icmp_seq=$i ttl=" . rand(52, 64)
                 . " time=" . round(rand(80, 420) / 10, 1) . " ms\n";
        }
        $out .= "\n--- $target ping statistics ---\n"
             . "4 packets transmitted, 4 received, 0% packet loss, time 3004ms";
        echo $out;
        break;

    case 'whoami':
        echo "guest";
        break;

    case 'help':
    case '?':
    case 'commands':
        echo "Available commands:\n\n"
           . "  files    : ls  cd  pwd  cat  tree  find  touch  mkdir  rm  grep  echo\n"
           . "  system   : whoami  id  hostname  uname  uptime  date  cal  ps  top  df  free  env  neofetch\n"
           . "  network  : ping  ifconfig  netstat  nmap  scan  ssh  host  weather\n"
           . "  fun      : cowsay  fortune  sl  calc  man\n"
           . "  session  : su  sudo  exit  clear  history  help\n\n"
           . "Tips: try `man [command]`, `sudo [command]`, and poke around /home/guest.\n"
           . "Not everything is listed here. Some things you have to find.";
        break;

    case 'clear':
        echo "COMMAND_CLEAR";
        break;

    case 'motd':
        $ip = "192.168.1." . rand(2, 250);
        $when = date('D M j H:i:s', strtotime("-" . rand(1, 5) . " days -" . rand(1, 59) . " minutes"));
        echo "Debian GNU/Linux 12 (bookworm) terminal-vm tty1\n\n"
           . "  * Documentation:  type 'help' for available commands\n"
           . "Last login: $when from $ip\n"
           . rand(40, 90) . " packages can be updated. Run 'sudo apt-get update' to refresh.\n\n"
           . "Type 'help' to get started.";
        break;

    case 'echo':
        echo implode(' ', array_slice($parts, 1));
        break;

    case 'id':
        echo ($_SESSION['user'] === 'root')
            ? "uid=0(root) gid=0(root) groups=0(root)"
            : "uid=1000(guest) gid=1000(guest) groups=1000(guest),4(adm),27(sudo),100(users)";
        break;

    case 'hostname':
        echo "terminal-vm";
        break;

    case 'ifconfig':
    case 'ip':
        echo htmlspecialchars(
            "eth0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500\n"
          . "        inet 192.168.1.42  netmask 255.255.255.0  broadcast 192.168.1.255\n"
          . "        inet6 fe80::21a:2bff:fe3c:4d5e  prefixlen 64  scopeid 0x20<link>\n"
          . "        ether 00:1a:2b:3c:4d:5e  txqueuelen 1000  (Ethernet)\n"
          . "        RX packets 182934  bytes 231847284 (221.1 MiB)\n"
          . "        TX packets 98213  bytes 12834728 (12.2 MiB)\n\n"
          . "lo: flags=73<UP,LOOPBACK,RUNNING>  mtu 65536\n"
          . "        inet 127.0.0.1  netmask 255.0.0.0"
        );
        break;

    case 'env':
    case 'printenv':
        echo "USER=" . $_SESSION['user'] . "\n"
           . "HOME=/home/guest\nSHELL=/bin/bash\n"
           . "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
           . "LANG=en_US.UTF-8\nTERM=xterm-256color\n"
           . "PWD=" . cwd_path() . "\n"
           . "LOGNAME=" . $_SESSION['user'];
        break;

    case 'top':
    case 'htop':
        echo "top - " . date('H:i:s') . " up 142 days,  4:12,  1 user,  load average: 0.08, 0.03, 0.01\n"
           . "Tasks: 142 total,   1 running, 141 sleeping,   0 stopped,   0 zombie\n"
           . "%Cpu(s):  2.3 us,  0.7 sy,  0.0 ni, 96.8 id,  0.2 wa\n"
           . "MiB Mem :   3936.0 total,   1821.4 free,   1024.2 used,   1090.4 buff/cache\n\n"
           . "  PID USER      PR  NI    VIRT    RES  %CPU  %MEM     TIME+ COMMAND\n"
           . " 1542 guest     20   0   12840   4820   1.3   0.1   0:12.34 bash\n"
           . " 2213 root      20   0  918234  48210   0.7   1.2   1:42.11 python3\n"
           . "  842 root      20   0  168420  11204   0.3   0.3   0:58.02 systemd\n"
           . " 3102 www-data  20   0  294810  22840   0.3   0.6   0:04.19 apache2\n\n"
           . "(press q to quit — simulated)";
        break;

    case 'tree':
        echo ".\n" . rtrim(fs_tree($currentDir));
        break;

    case 'find':
        if ($arg === '') { echo "usage: find [name]"; break; }
        $hits = fs_find($currentDir, $arg);
        echo $hits
            ? implode("\n", array_map(function ($p) { return './' . $p; }, $hits))
            : "find: '" . $arg . "': no matches found";
        break;

    case 'cal':
        $first = (int) date('w', mktime(0, 0, 0, (int) date('n'), 1));
        $days  = (int) date('t');
        $hdr   = date('F Y');
        $pad   = (int) floor((20 - strlen($hdr)) / 2);
        $out   = str_repeat(' ', max(0, $pad)) . $hdr . "\n";
        $out  .= "Su Mo Tu We Th Fr Sa\n";
        $out  .= str_repeat('   ', $first);
        for ($d = 1; $d <= $days; $d++) {
            $out .= str_pad($d, 2, ' ', STR_PAD_LEFT) . ' ';
            if (($first + $d) % 7 === 0) $out .= "\n";
        }
        echo rtrim($out);
        break;

    case 'cowsay':
        $sp = strpos($cmdInput, ' ');
        $text = ($sp === false) ? 'Moooo!' : trim(substr($cmdInput, $sp + 1));
        if ($text === '') $text = 'Moooo!';
        if (strlen($text) > 60) $text = substr($text, 0, 60);
        $bar = str_repeat('_', strlen($text) + 2);
        $dsh = str_repeat('-', strlen($text) + 2);
        $cow = " " . $bar . "\n"
             . "< " . $text . " >\n"
             . " " . $dsh . "\n"
             . "        \\   ^__^\n"
             . "         \\  (oo)\\_______\n"
             . "            (__)\\       )\\/\\\n"
             . "                ||----w |\n"
             . "                ||     ||";
        echo htmlspecialchars($cow);
        break;

    case 'fortune':
        $fortunes = [
            "The best way to predict the future is to implement it.",
            "rm -rf is forever. So is regret.",
            "There are only 10 kinds of people: those who read binary and those who don't.",
            "A good programmer looks both ways before crossing a one-way street.",
            "Real programmers count from zero.",
            "To understand recursion, you must first understand recursion.",
            "It works on my machine.",
            "Weeks of coding can save you hours of planning.",
            "There is no place like 127.0.0.1.",
            "Keyboard not found. Press F1 to continue.",
            "The cake is a lie, but the segfault is real.",
            "sudo make me a sandwich.",
            "There are fingers here."
        ];
        echo htmlspecialchars($fortunes[array_rand($fortunes)]);
        break;

    case 'sl':
        echo "\n"
           ."                  C  O C@@O C O  C@O  CO    @@    O     @     O     @      O\n"
           ."              C@@@O\n"
           ."         C    O\n"
           ."       C@@@@O\n"
           ."\n"
           ."     0   0\n"
           ."     ++      +------ ____                 ____________________ ____________________\n"
           ."     ||      |+-+ |  |   \@@@@@@@@@@@     |  ___ ___ ___ ___ | |  ___ ___ ___ ___ |\n"
           ."   /---------|| | |  |    \@@@@@@@@@@@@@_ |  |_| |_| |_| |_| | |  |_| |_| |_| |_| |\n"
           ."  + ========  +-+ |  |                  | |__________________| |__________________|\n"
           ." _|--/~\------/~\-+  |__________________| |__________________| |__________________|\n"
           ."//// O========O_/       (O)       (O)        (O)        (O)       (O)        (O)\n"
           . "\n(did you mean 'ls'?  choo choo!)";
        break;

    case 'neofetch':
        $logo = [
            "        _____        ",
            "       /     \\       ",
            "      | () () |      ",
            "       \\  ^  /       ",
            "       |||||||       ",
            "       |||||||       ",
        ];
        $info = [
            $_SESSION['user'] . "@terminal-vm",
            "-----------------",
            "OS: Debian GNU/Linux 12 (bookworm) x86_64",
            "Host: terminal-vm 1.0",
            "Kernel: 5.15.0-101-generic",
            "Uptime: 142 days, 4 hours, 12 mins",
            "Packages: 1842 (dpkg)",
            "Shell: bash 5.2.15",
            "Resolution: 1920x1080",
            "Terminal: /dev/pts/0",
            "CPU: Intel Xeon (4) @ 2.40GHz",
            "Memory: 1024MiB / 3936MiB",
        ];
        $rows = max(count($logo), count($info));
        $out = '';
        for ($i = 0; $i < $rows; $i++) {
            $l = str_pad(isset($logo[$i]) ? $logo[$i] : '', 21);
            $r = isset($info[$i]) ? $info[$i] : '';
            $out .= $l . '  ' . $r . "\n";
        }
        echo rtrim($out);
        break;

    case '':
        break;

    default:
        echo "bash: $cmd: command not found";
        break;
}
