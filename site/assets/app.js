(function(){
  "use strict";

  // --- v0rt's own console easter egg (this site collects these, after all) ---
  try {
    var _b = "background:#00ff66;color:#040804;font-weight:bold;padding:2px 8px;border-radius:3px";
    var _g = "color:#00ff66;font-family:monospace";
    console.log("%c v0rt %c you opened the console on a site that collects console easter eggs.", _b, _g);
    console.log("%cnicely done — you're exactly the kind of human we built this for.\nfound something delightful in the wild? the web is still handmade\nif you know where to look. keep digging.  <3\n\nthere's more where this came from: view-source, the response headers,\n/humans.txt, and a /robots.txt with opinions.", _g);
  } catch (e) {}

  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // --- boot sequence (once per session, skippable) ---
  var boot = document.getElementById('boot'), log = document.getElementById('bootlog');
  function endBoot(){ if(!boot) return; boot.classList.add('gone');
    setTimeout(function(){ if(boot && boot.parentNode) boot.parentNode.removeChild(boot); }, 550); }
  if (boot && !(reduce || sessionStorage.getItem('chunt_booted'))) {
    sessionStorage.setItem('chunt_booted','1');
    boot.style.display = 'flex';
    var total = (document.getElementById('grid') || {}).childElementCount || 0;
    var lines = ["> INITIALIZING UPLINK ...",
                 "> BYPASSING ICE .............. [ OK ]",
                 "> ACCESSING MAINFRAME ........ [ OK ]",
                 "> DECRYPTING finds.log ....... [ OK ]",
                 "> LOOT RECOVERED: " + total + " ENTRIES",
                 "> CONNECTION ESTABLISHED_"];
    var i=0, buf="";
    (function type(){
      if(i>=lines.length){ setTimeout(endBoot, 550); return; }
      buf += lines[i] + "\n"; log.textContent = buf; i++; setTimeout(type, 250);
    })();
    boot.addEventListener('click', endBoot);
    window.addEventListener('keydown', endBoot, {once:true});
  }

  // --- likes (any page that has .like buttons: gallery, find, archive) ---
  function likedSet(){
    try { return new Set(JSON.parse(localStorage.getItem('chunt_liked') || '[]')); }
    catch(e){ return new Set(); }
  }
  function saveLiked(s){
    try { localStorage.setItem('chunt_liked', JSON.stringify(Array.from(s))); } catch(e){}
  }
  var liked = likedSet();
  [].slice.call(document.querySelectorAll('.like')).forEach(function(btn){
    if (liked.has(btn.dataset.id)) btn.classList.add('liked');
  });
  document.addEventListener('click', function(ev){
    var btn = ev.target.closest ? ev.target.closest('.like') : null;
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    var id = btn.dataset.id, lc = btn.querySelector('.lc');
    fetch('/vote.php', {
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'id=' + encodeURIComponent(id)
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d && typeof d.count === 'number') {
        if (lc) lc.textContent = d.count;
        var card = btn.closest('.card'); if (card) card.dataset.likes = d.count;
        if (d.liked) { btn.classList.add('liked'); liked.add(id); }
        else { btn.classList.remove('liked'); liked.delete(id); }
        saveLiked(liked);
      }
    }).catch(function(){}).then(function(){ btn.disabled = false; });
  });

  // --- copy a permalink to a single find (any page) ---
  document.addEventListener('click', function(ev){
    var pb = ev.target.closest ? ev.target.closest('.permalink') : null;
    if (!pb) return;
    var url = location.origin + '/find/' + pb.dataset.id;
    var flash = function(){
      var orig = pb.textContent;
      pb.textContent = 'copied!'; pb.classList.add('copied');
      setTimeout(function(){ pb.textContent = orig; pb.classList.remove('copied'); }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(flash, function(){ window.prompt('copy this link:', url); });
    } else { window.prompt('copy this link:', url); }
  });

  // --- filter + sort + "load more" pagination (the gallery only; /archive
  // and /find have a #grid but no filter chips, so this stays off there) ---
  var grid = document.getElementById('grid');
  var chips = grid ? [].slice.call(document.querySelectorAll('.chip')) : [];
  if (grid && chips.length) {
    var cards = [].slice.call(grid.querySelectorAll('.card'));
    var sorts = [].slice.call(document.querySelectorAll('.sortbtn'));
    var showing = document.getElementById('showing');
    var moreBtn = document.getElementById('more');
    var PAGE = 48, shown = PAGE;
    var curFilter = 'all', curSort = 'score';
    var cmp = function(a, b){
      var ta=a.dataset.ts, tb=b.dataset.ts;
      if (curSort === 'score')  return (+b.dataset.score - +a.dataset.score) || (tb<ta?-1:tb>ta?1:0);
      if (curSort === 'likes')  return (+b.dataset.likes - +a.dataset.likes) || (+b.dataset.score - +a.dataset.score);
      if (curSort === 'newest') return tb<ta?-1:tb>ta?1:0;
      return ta<tb?-1:ta>tb?1:0;  // oldest
    };
    var apply = function(){
      var arr = cards.slice().sort(cmp);
      var matched = 0, visible = 0, frag = document.createDocumentFragment();
      arr.forEach(function(c){
        var inFilter = (curFilter==='all' || c.dataset.kind===curFilter);
        if (inFilter && matched < shown) { c.style.display = ''; visible++; }
        else { c.style.display = 'none'; }
        if (inFilter) matched++;
        frag.appendChild(c);
      });
      grid.appendChild(frag);
      if (showing) showing.textContent = 'showing ' + visible + (matched > visible ? ' of ' + matched : '');
      if (moreBtn) {
        var rest = matched - visible;
        moreBtn.hidden = rest <= 0;
        if (rest > 0) moreBtn.textContent = 'load more (' + rest + ')';
      }
    };
    chips.forEach(function(ch){ ch.addEventListener('click', function(){
      chips.forEach(function(x){ x.classList.remove('active'); }); ch.classList.add('active');
      curFilter = ch.dataset.filter; shown = PAGE; apply();
    }); });
    sorts.forEach(function(b){ b.addEventListener('click', function(){
      sorts.forEach(function(x){ x.classList.remove('active'); }); b.classList.add('active');
      curSort = b.dataset.sort; shown = PAGE; apply();
    }); });
    if (moreBtn) moreBtn.addEventListener('click', function(){ shown += PAGE; apply(); });
    apply();
  }
})();

// --- the "close"-button egg: clicking the red dot doesn't close v0rt, it
// powers the screen off (CRT style) and boots a very real-looking terminal ---
(function(){
  var red   = document.querySelector('.titlebar .dot.r');
  var crt   = document.getElementById('crt-off');
  var shell = document.getElementById('fakebash');
  if (!red || !crt || !shell) return;
  var frame = shell.querySelector('iframe');
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  var exitBtn = shell.querySelector('.exit');
  function close(){ shell.classList.remove('on'); }
  function boot(){
    if (!frame.src) {
      frame.src = '/terminal/';                  // lazy-load on first open
      // iframe keydowns don't reach this document, so wire our keys inside it.
      frame.addEventListener('load', function(){
        try {
          frame.contentDocument.addEventListener('keydown', function(e){
            if (e.key === 'Escape') { close(); return; }
            if (e.key === 'Enter') {
              var idoc = frame.contentDocument;
              var ci = idoc.getElementById('cmd');
              if (ci && ci.value.trim().toLowerCase() === 'exit') {
                // let the terminal handle `exit` while root (drops to guest);
                // a guest `exit` leaves the terminal and returns to v0rt home.
                var pr = idoc.getElementById('prompt');
                var isRoot = pr && /(^|\s)root@/.test(pr.innerText || '');
                if (!isRoot) {
                  e.preventDefault(); e.stopImmediatePropagation();
                  ci.value = '';
                  window.location.href = '/';
                }
              }
            }
          }, true);   // capture, to beat the terminal's own Enter handler
        } catch (e) {}
      });
    }
    shell.classList.add('on');
    crt.classList.remove('on');
  }
  red.addEventListener('click', function(){
    if (reduce) { boot(); return; }             // no flourish if motion-reduced
    crt.classList.remove('on'); void crt.offsetWidth; crt.classList.add('on'); // (re)play the power-off
    setTimeout(boot, 560);                       // ~matches the .55s animation
  });
  if (exitBtn) exitBtn.addEventListener('click', close);
  // Esc powers v0rt back on (this document; the iframe gets its own above).
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && shell.classList.contains('on')) close();
  });
})();
