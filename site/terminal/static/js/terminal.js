    (function() {
        const input = document.getElementById('cmd');
        const history = document.getElementById('history');
        const promptLabel = document.getElementById('prompt');
        const terminalContainer = document.getElementById('terminal-container');

        if (!input || !history || !promptLabel || !terminalContainer) return;

        let commandHistory = [];
        let historyIndex = -1;

        // Escape user text before it goes into innerHTML (no self-XSS from a
        // typed command). Server responses are already escaped server-side.
        function esc(s){ return String(s).replace(/[&<>"']/g, function(c){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }

        // Known commands, for Tab-completing the first word.
        const COMMANDS = ['ls','cd','pwd','cat','tree','find','touch','mkdir','rm','grep','echo','whoami','id','hostname','uname','uptime','date','cal','ps','top','df','free','env','neofetch','ping','ifconfig','netstat','nmap','scan','ssh','host','weather','cowsay','fortune','sl','calc','man','su','sudo','exit','clear','history','help'];

        // Commands are sent base64-encoded so a host WAF (ModSecurity) doesn't
        // mistake "mkdir"/"rm"/"cat /etc/passwd"/etc. for a real injection attack
        // and block the request. The server decodes ?c= (falls back to ?cmd=).
        function enc(s){ return encodeURIComponent(btoa(unescape(encodeURIComponent(s)))); }

        input.addEventListener('keydown', function(e) {
            // 1. TAB COMPLETION
            if (e.key === 'Tab') {
                e.preventDefault();
                const words = input.value.split(' ');
                const partial = words.pop();

                if (words.length === 0) {           // completing the command itself
                    const m = COMMANDS.find(function(c){ return c.indexOf(partial) === 0; });
                    if (m) input.value = m;
                    return;
                }

                fetch('./terminal.php?c=' + enc('ls'))
                    .then(res => res.text())
                    .then(data => {
                        const temp = document.createElement('div');
                        temp.innerHTML = data;
                        const files = Array.from(temp.querySelectorAll('span')).map(s => s.innerText.replace('/', ''));
                        const match = files.find(f => f.startsWith(partial));
                        if (match) {
                            words.push(match);
                            input.value = words.join(' ');
                        }
                    });
            }

            // 2. ARROW LOGIC
            if (e.key === 'ArrowUp') {
                if (historyIndex < commandHistory.length - 1) {
                    historyIndex++;
                    input.value = commandHistory[commandHistory.length - 1 - historyIndex];
                }
            } else if (e.key === 'ArrowDown') {
                if (historyIndex > 0) {
                    historyIndex--;
                    input.value = commandHistory[commandHistory.length - 1 - historyIndex];
                } else {
                    historyIndex = -1;
                    input.value = '';
                }
            }

            // 3. ENTER KEY
            if (e.key === 'Enter') {
                const command = input.value;
                const currentPrompt = promptLabel.innerText;
                
                if (command.trim() !== "") {
                    commandHistory.push(command);
                }
                historyIndex = -1;

                // FIXED: Using pure classes instead of dynamic style tags
                const isCurrentlyRoot = promptLabel.classList.contains('user-root');
                const promptClass = isCurrentlyRoot ? 'user-root' : 'user-guest';

                history.innerHTML += '<div class="history-item"><span class="' + promptClass + '">' + esc(currentPrompt) + '</span><span class="history-command">' + esc(command) + '</span></div>';
                
                input.value = '';

                // `history` lists the real client-side command history.
                if (command.trim().toLowerCase() === 'history') {
                    var hy = commandHistory.map(function(c, i){ return '  ' + (i + 1) + '  ' + esc(c); }).join('\n');
                    history.innerHTML += '<div class="output output-text">' + (hy || 'No history yet.') + '</div>';
                    updatePrompt();
                    terminalContainer.scrollTop = terminalContainer.scrollHeight;
                    return;
                }

                fetch('./terminal.php?c=' + enc(command))
                    .then(res => res.text())
                    .then(data => {
                        processResponse(data);
                    });
            }
        });

        function processResponse(data) {
            const cleanData = data.trim();

            if (cleanData.indexOf("OPEN_URL ") === 0) {
                var openUrl = cleanData.slice(9).trim();
                history.innerHTML += '<div class="output output-text">Opening ' + esc(openUrl) + ' …</div>';
                window.open(openUrl, '_blank', 'noopener');
                updatePrompt();
                terminalContainer.scrollTop = terminalContainer.scrollHeight;
                return;
            }

            if (cleanData.includes("SUDO_PASS_REQUIRED")) {
                const parts = cleanData.split("SUDO_PASS_REQUIRED");
                const lecture = parts[0].trim();
                const originalCmd = parts[1].trim();

                if (lecture !== "") {
                    history.innerHTML += '<div class="output output-text">' + lecture + '</div>';
                }

                let pass = prompt("[sudo] password for guest:");
                fetch('./terminal.php?c=' + enc('sudo_auth ' + pass + ' ' + originalCmd))
                    .then(res => res.text())
                    .then(authData => {
                        if (authData.trim() !== "") {
                            history.innerHTML += '<div class="output output-text">' + authData.trim() + '</div>';
                        }
                        updatePrompt();
                    });
                return;
            }

            if (cleanData === "SYSTEM_DESTROYED") {
                document.body.innerHTML = '';
                document.body.style.backgroundColor = '#000'; // Note: If the server blocks this background color, use document.body.className = 'dark-theme'
                setTimeout(() => {
                    // FIXED: Stripped inline style tag, replaced with purely class layout definitions
                    document.body.innerHTML = '<div class="output output-white panic-container">FATAL: Kernel panic - not syncing: Attempted to kill init!<br>System halted.</div>';
                }, 1500);
                return;
            }

            if (cleanData === "AUTH_REQUIRED") {
                let pass = prompt("Password for root:");
                fetch('./terminal.php?c=' + enc('auth_root ' + pass))
                    .then(res => res.text())
                    .then(authData => {
                        history.innerHTML += '<div class="output output-text">' + authData.trim() + '</div>';
                        updatePrompt();
                    });
                return;
            }

            if (cleanData === "COMMAND_CLEAR") {
                history.innerHTML = '<div class="output output-text">Terminal cleared.</div>';
            } else if (cleanData !== "") {
                history.innerHTML += '<div class="output output-text">' + cleanData + '</div>';
            }

            updatePrompt();
            terminalContainer.scrollTop = terminalContainer.scrollHeight;
        }

        function updatePrompt() {
            Promise.all([
                fetch('./terminal.php?c=' + enc('whoami')).then(res => res.text()),
                fetch('./terminal.php?c=' + enc('pwd')).then(res => res.text())
            ]).then(([user, pwd]) => {
                const u = user.trim();
                const isRoot = (u === 'root');
                const home = isRoot ? '/root' : '/home/' + u;
                let p = pwd.trim();
                if (p === home) p = '~';
                else if (p.indexOf(home + '/') === 0) p = '~' + p.slice(home.length);
                promptLabel.innerText = u + '@system:' + p + (isRoot ? '#' : '$') + ' ';
                
                // Safely toggles classes instead of trying to pass dynamic style text strings
                if (isRoot) {
                    promptLabel.classList.remove('user-guest');
                    promptLabel.classList.add('user-root');
                } else {
                    promptLabel.classList.remove('user-root');
                    promptLabel.classList.add('user-guest');
                }
            });
        }

        window.addEventListener('load', function() {
            updatePrompt();
            input.focus();
            // Replace the plain welcome line with a proper MOTD / last-login banner.
            fetch('./terminal.php?c=' + enc('motd'))
                .then(function(r){ return r.text(); })
                .then(function(t){ if (t && t.trim()) history.innerHTML = '<div class="output output-text">' + t + '</div>'; })
                .catch(function(){});
        });
    })();
