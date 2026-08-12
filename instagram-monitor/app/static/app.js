"use strict";
const $ = id => document.getElementById(id);
const esc = s => { const d=document.createElement("div"); d.textContent=s??""; return d.innerHTML; };
const fmt = n => { n=+n||0; return n>=1e6 ? (n/1e6).toFixed(1).replace(".",",")+" Mio."
  : n>=1e4 ? (n/1e3).toFixed(1).replace(".",",")+" Tsd." : n.toLocaleString("de-DE"); };

const fmtBytes = b => { b=+b||0;
  return b>=1e9 ? (b/1e9).toFixed(2).replace(".",",")+" GB"
    : b>=1e6 ? (b/1e6).toFixed(1).replace(".",",")+" MB"
    : b>=1e3 ? Math.round(b/1e3)+" KB" : b+" B"; };

/* ---------- Zurück-Knopf (Ansichts-Verlauf) ---------- */
let viewStack=[], replayingView=false;
function pushView(key, t){
  if(replayingView) return;
  const top=viewStack[viewStack.length-1];
  if(top && top.key===key){ top.t=t; return; }   // gleiche Ansicht → nur merken
  viewStack.push({key, t});
  if(viewStack.length>30) viewStack.shift();
  updateBackBtn();
}
function updateBackBtn(){ const b=$("back_btn"); if(b) b.style.display = viewStack.length>1 ? "" : "none"; }
function goBack(){
  if(viewStack.length<2) return;
  viewStack.pop();
  const top=viewStack[viewStack.length-1];
  replayingView=true;
  try{ top.t(); } finally { replayingView=false; }
  updateBackBtn();
}

/* ---------- Grundlagen ---------- */
async function api(path, opts){
  try{
    const res = await fetch(path, opts);
    let data = {}; try{ data = await res.json(); }catch(e){}
    if(typeof setOffline==="function") setOffline(false);  // Verbindung steht wieder
    return {status:res.status, data};
  }catch(e){
    if(typeof setOffline==="function") setOffline(true);
    return {status:0, data:{error:"Verbindung zum lokalen Server fehlgeschlagen."}};
  }
}
function toast(msg, kind){
  if(!msg) return;
  const t = document.createElement("div");
  t.className = "toast" + (kind?(" "+kind):"");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(()=>t.remove(), 4200);
}
// Skeleton-Platzhalter: graue „lädt“-Zeilen statt eines drehenden Kreisels –
// die Seite wirkt sofort fertig, die Inhalte trudeln nach.
function skeletonRows(n){
  let html="";
  for(let i=0;i<(n||8);i++){
    html+='<div class="skrow"><div class="sk skc"></div><div class="skl">'
      +'<div class="sk" style="height:12px;width:'+(40+i*7%45)+'%"></div>'
      +'<div class="sk" style="height:10px;width:'+(25+i*11%40)+'%"></div></div></div>';
  }
  return html;
}
/* ---------- Offline-Erkennung ---------- */
function setOffline(off){
  const w=$("warnbar"); if(!w) return;
  if(off){ w.textContent="⚠ Keine Verbindung zum Programm auf dem PC – läuft es noch? (Fenster offen lassen)";
    w.style.display="block"; w.dataset.off="1"; }
  else if(w.dataset.off==="1"){ w.dataset.off=""; w.style.display="none"; }
}
window.addEventListener("offline", ()=>setOffline(true));
window.addEventListener("online", ()=>{ setOffline(false); if(typeof refreshStatus==="function") refreshStatus(); });
/* ---------- Browser-Benachrichtigungen bei neuen Beiträgen ---------- */
let notifiedNew = {};
function enableNotifications(){
  if(!("Notification" in window)){ toast("Dieser Browser kann keine Benachrichtigungen zeigen.","err"); return; }
  Notification.requestPermission().then(p=>{
    try{ localStorage.setItem("igm_notify", p==="granted"?"1":"0"); }catch(e){}
    toast(p==="granted"?"Benachrichtigungen an – du wirst bei neuen Beiträgen informiert.":"Benachrichtigungen wurden nicht erlaubt.", p==="granted"?"ok":"err");
  });
}
function notifyEnabled(){ try{ return localStorage.getItem("igm_notify")==="1"; }catch(e){ return false; } }
function toggleNotify(){
  if($("opt_notify").checked) enableNotifications();
  else { try{ localStorage.setItem("igm_notify","0"); }catch(e){}
    toast("Benachrichtigungen aus.","ok"); }
}
function maybeNotifyNew(accounts){
  if(!notifyEnabled() || !("Notification" in window) || Notification.permission!=="granted") return;
  (accounts||[]).forEach(a=>{
    if(a.is_new && a.last_post_at && notifiedNew[a.username]!==a.last_post_at){
      notifiedNew[a.username]=a.last_post_at;
      try{ new Notification("Neuer Beitrag von @"+a.username,
        {body:(a.full_name||a.username)+" hat etwas Neues gepostet", icon:"/icon.svg", tag:a.username}); }catch(e){}
    }
  });
}
function timeAgo(iso){
  if(!iso) return "";
  const d = new Date(iso.replace(" ","T")+ (iso.length<=19?"Z":""));
  const s = (Date.now()-d.getTime())/1000;
  if(isNaN(s)) return "";
  if(s<60) return "gerade eben";
  if(s<3600) return "vor "+Math.floor(s/60)+" Min";
  if(s<86400) return "vor "+Math.floor(s/3600)+" Std";
  if(s<604800) return "vor "+Math.floor(s/86400)+" T";
  return d.toLocaleDateString("de-DE");
}

/* ---------- Theme ---------- */
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  $("theme_btn").textContent = t==="light" ? "☀️" : "🌙";
  try{ localStorage.setItem("igm_theme", t); }catch(e){}
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute("data-theme")==="light" ? "light":"dark";
  applyTheme(cur==="light"?"dark":"light");
}
applyTheme((()=>{ try{ return localStorage.getItem("igm_theme")||"dark"; }catch(e){ return "dark"; } })());

/* ---------- Gespeichert (lokal, ganze Beiträge) ---------- */
// Gefolgt-/Follower-Listen lokal merken: so sind sie in der Suche sofort
// da (ohne Instagram-Abruf) und überleben einen Neustart.
function saveFollowCache(kind, accts){ try{ localStorage.setItem("igm_list_"+kind, JSON.stringify(accts)); }catch(e){} }
function getFollowCache(kind){ try{ return JSON.parse(localStorage.getItem("igm_list_"+kind)||"[]"); }catch(e){ return []; } }
function getSaved(){ try{ return JSON.parse(localStorage.getItem("igm_saved")||"[]"); }catch(e){ return []; } }
function isSaved(sc){ return getSaved().some(p=>p.shortcode===sc); }
function toggleSavedPost(post){
  let s = getSaved();
  if(s.some(p=>p.shortcode===post.shortcode)) s = s.filter(p=>p.shortcode!==post.shortcode);
  else s.unshift(post);
  try{ localStorage.setItem("igm_saved", JSON.stringify(s.slice(0,300))); }catch(e){}
  return isSaved(post.shortcode);
}
/* Story-„gesehen"-Zustand */
function seenStories(){ try{ return JSON.parse(localStorage.getItem("igm_seen")||"[]"); }catch(e){ return []; } }
function markSeen(u){ const s=seenStories(); if(!s.includes(u)){ s.push(u);
  try{ localStorage.setItem("igm_seen", JSON.stringify(s.slice(-200))); }catch(e){} } }

/* @Erwähnungen und #Hashtags in der Bildunterschrift hervorheben */
function linkify(text){
  return esc(text).replace(/(^|\s)@([A-Za-z0-9._]{1,30})/g,
      '$1<a class="tag" onclick="openProfile(\'$2\')">@$2</a>')
    .replace(/(^|\s)#([\wäöüÄÖÜ]+)/g, '$1<span class="tag">#$2</span>');
}
function copyLink(url){
  try{ navigator.clipboard.writeText(url); toast("Link kopiert","ok"); }
  catch(e){ toast(url); }
}
async function shareOrCopy(p){
  const url = p.url || p;
  if(navigator.share){
    try{ await navigator.share({title:"Instagram", text:"@"+(p.username||""), url}); return; }
    catch(e){ /* abgebrochen -> kopieren */ }
  }
  copyLink(url);
}

/* ---------- Status / Anmeldung ---------- */
let loggedIn = false, currentUser = "";
// Warnleiste (Anmeldung abgelaufen / Drossel-Pause) aktualisieren.
function updateWarnbar(msg){
  const w=$("warnbar"); if(!w) return;
  if(msg){ w.textContent="⚠ "+msg; w.style.display="block"; }
  else w.style.display="none";
}
// Leichter Status-Check alle 5 Minuten: NUR lokale Abfrage, aktualisiert
// die Warnleiste – lädt nichts von Instagram und wechselt keine Ansicht.
async function pollStatusWarning(){
  const {data}=await api("/api/status");
  if(data && data.error===undefined) updateWarnbar(data.warning||"");
}
setInterval(pollStatusWarning, 5*60*1000);
async function refreshStatus(){
  if(!window._dlRestore){ window._dlRestore=true; restoreDlBar(); }
  const {data} = await api("/api/status");
  loggedIn = !!data.logged_in; currentUser = data.user || "";
  updateWarnbar(data.warning||"");
  $("whoami").innerHTML = loggedIn
    ? '<b onclick="showMe()" style="cursor:pointer">@'+esc(currentUser)+'</b>'
    : '<span onclick="scrollToLogin()" style="cursor:pointer">anmelden</span>';
  $("login_panel").style.display = loggedIn ? "none" : "block";
  $("manage_btn").style.display = loggedIn ? "flex" : "none";
  // Gut sichtbarer Abmelden-Knopf in der Kopfleiste (nur wenn angemeldet).
  if($("logout_btn")) $("logout_btn").style.display = loggedIn ? "flex" : "none";
  if(loggedIn){
    if(!window._autostart){
      window._autostart = true;
      // Einstellungen UND Sofort-Feed PARALLEL laden (statt nacheinander) –
      // der lokale Feed ist dadurch einen Roundtrip früher da.
      const [fs, cf] = await Promise.all([
        api("/api/feed/settings"),
        api("/api/feed/cached"),
      ]);
      if(fs.data && fs.data.profiles!==undefined){
        $("feed_profiles").value = String(fs.data.profiles);
        $("feed_per").value = String(fs.data.per);
      }
      // Sofort-Feed: Die zuletzt geladenen Beiträge liegen lokal auf der
      // Platte und erscheinen SOFORT – wie bei Instagram, ganz ohne
      // Instagram-Abruf. Erst ⟳ bzw. der nächste Feed-Lauf holt Neues.
      const items = (cf.data && cf.data.items) || [];
      if(items.length){
        feedCommitted = items;
        feedOffset = feedTotal = 0;   // kein automatisches Nachladen beim Scrollen
        showFeed();
        $("more").innerHTML = '<div class="hint">Sofort-Feed · Stand: '
          + (cf.data.saved_at ? timeAgo(cf.data.saved_at) : "unbekannt")
          + ' · ⟳ oben lädt Neues</div>';
      } else {
        showStart();   // noch nie einen Feed geladen → Dashboard
      }
    }
  }else{
    // Auch ohne Anmeldung: schwarzes Dashboard (das Anmelde-Feld steht
    // darüber; öffentliche Profile lassen sich per Namenseingabe laden).
    showStart();
  }
}
function showHomeIdle(){ showStart(); }
// Dashboard: schwarzer Startbildschirm – lädt NICHTS von Instagram.
// (Die kleine Statistik-Zeile liest nur die eigene Festplatte.)
function showStart(){
  cancelSelect(); pushView("start", showStart);
  viewMode="start"; setNav("nav_home");
  $("login_panel").style.display=loggedIn?"none":"block";
  if(typeof feedTimer!=="undefined" && feedTimer){ clearInterval(feedTimer); feedTimer=null; }
  $("pf_head").style.display="none"; $("subbar").innerHTML=""; $("more").innerHTML="";
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("content").className="";
  $("content").innerHTML='<div style="min-height:68vh;display:flex;flex-direction:column;'
    +'align-items:center;justify-content:center;text-align:center;gap:16px">'
    +'<div style="font-size:26px;font-weight:800;text-shadow:0 2px 10px #000">📷 Instagram Monitor</div>'
    +'<div class="hint" id="dash_stats" style="text-shadow:0 1px 6px #000">&nbsp;</div>'
    +'<div class="rowflex" style="justify-content:center;gap:8px;width:100%;max-width:420px">'
    +'<input id="dash_user" placeholder="Kontoname eingeben …" style="flex:1" autocomplete="off"'
    +' onkeydown="if(event.key===\'Enter\')openProfile(this.value)">'
    +'<button class="btn" onclick="openProfile($(\'dash_user\').value)">Profil laden</button>'
    +'</div>'
    +'<div class="rowflex" style="justify-content:center;gap:10px;flex-wrap:wrap">'
    +'<button class="btn ghost" onclick="showFollowing()">Gefolgt / Follower</button>'
    +'<button class="btn ghost" onclick="showSearch()">Suchen</button>'
    +'<button class="btn ghost" onclick="startFeed()">Feed</button>'
    +'<button class="btn ghost" onclick="showStats()">📊 Statistik</button>'
    +'<button class="btn ghost" onclick="showDownloads()">📥 Verlauf</button>'
    +'</div>'
    +'<div class="hint" style="text-shadow:0 1px 6px #000">Nichts wird automatisch von Instagram geladen – wähle selbst.</div>'
    +'</div>';
  api("/api/stats").then(({data})=>{
    const el=$("dash_stats");
    if(!el || viewMode!=="start" || !data || !data.accounts) return;
    el.textContent = data.accounts.length+" Konten · "+fmt(data.total_files)
      +" Dateien · "+fmtBytes(data.total_bytes);
  });
}
// Statistik: Liste aller heruntergeladenen Konten mit Dateizahl und Größe.
async function showStats(){
  cancelSelect(); pushView("stats", showStats);
  viewMode="stats"; setNav("nav_home");
  if(typeof feedTimer!=="undefined" && feedTimer){ clearInterval(feedTimer); feedTimer=null; }
  $("subbar").innerHTML=""; $("more").innerHTML="";
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("content").className="";
  $("pf_head").style.display="flex"; $("pf_head").className="pf-head";
  $("pf_head").innerHTML='<div class="ph">📊</div><div><div class="fn">Statistik</div>'
    +'<div class="un" id="st_sum">lädt …</div></div>';
  $("content").innerHTML='<div class="empty"><span class="spin"></span></div>';
  const {data}=await api("/api/stats");
  if(viewMode!=="stats") return;
  if(!data || data.error || !data.accounts){ renderEmpty((data&&data.error)||"Statistik nicht verfügbar."); return; }
  const accts=data.accounts;
  const sum=$("st_sum");
  const frei = (data.disk && data.disk.free)
    ? " · "+fmtBytes(data.disk.free)+" frei" : "";
  if(sum) sum.textContent=accts.length+" Konten · "+fmt(data.total_files)
    +" Dateien · "+fmtBytes(data.total_bytes)+" gesamt"+frei;
  // Werkzeugleiste: Sicherheitskopie + fehlgeschlagene Downloads.
  $("content").innerHTML='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip" onclick="doBackup()">🗄 Sicherheitskopie</button>'
    +'<button class="chip" onclick="showFailed()">⚠ Fehlgeschlagene Downloads</button>'
    +'</div>';
  if(accts.length===0){
    const e=document.createElement("div"); e.className="empty";
    e.textContent="Noch nichts heruntergeladen."; $("content").appendChild(e); return; }
  const maxB=accts[0].bytes||1;
  accts.forEach(a=>{
    const r=document.createElement("div"); r.className="acctrow";
    const pct=Math.max(2, Math.round(a.bytes/maxB*100));
    r.innerHTML='<div class="ph">📁</div><div style="flex:1;min-width:0">'
      +'<div class="fn">@'+esc(a.username)+'</div>'
      +'<div class="un">'+fmt(a.files)+' Dateien · '+fmtBytes(a.bytes)+'</div>'
      +'<div style="height:4px;border-radius:2px;background:var(--border);margin-top:5px">'
      +'<div style="height:4px;border-radius:2px;background:var(--accent);width:'+pct+'%"></div></div>'
      +'</div>';
    r.style.cursor="pointer"; r.title="Profil öffnen";
    r.onclick=()=>openProfile(a.username);
    $("content").appendChild(r);
  });
}
// Sicherheitskopie der Datenbank anlegen.
async function doBackup(){
  toast("Sicherheitskopie wird erstellt …");
  const {data}=await api("/api/backup",{method:"POST"});
  if(data && data.ok) toast("Kopie gespeichert: "+(data.path||""),"ok");
  else toast((data&&data.error)||"Backup fehlgeschlagen","err");
}
// Fehlgeschlagene / noch offene Downloads anzeigen + erneut versuchen.
let failedTimer=null;
async function showFailed(){
  cancelSelect(); pushView("failed", showFailed);
  viewMode="failed"; setNav("nav_profile"); $("subbar").innerHTML=""; $("more").innerHTML="";
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("content").className="";
  $("pf_head").style.display="flex"; $("pf_head").className="pf-head";
  $("pf_head").innerHTML='<div class="ph">⚠</div><div><div class="fn">Fehlgeschlagene Downloads</div><div class="un" id="fail_sum">lädt …</div></div>';
  $("content").innerHTML='<div class="empty"><span class="spin"></span></div>';
  const {data}=await api("/api/failed-downloads");
  if(viewMode!=="failed") return;
  if(data.error){ renderEmpty(data.error); return; }
  const items=data.items||[];
  const s=$("fail_sum"); if(s) s.textContent=items.length+" offen";
  if(items.length===0){ renderEmpty("Keine offenen Downloads – alles geladen. 🎉"); return; }
  $("content").innerHTML='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip" id="retry_btn" onclick="retryFailed()">↻ Alle erneut versuchen</button></div>';
  items.forEach(it=>{
    const r=document.createElement("div"); r.className="acctrow";
    const icon=/(Video|Reel)/.test(it.type)?'▶':(/Album/.test(it.type)?'▦':'🖼');
    r.innerHTML='<div class="ph">'+icon+'</div><div style="flex:1;min-width:0">'
      +'<div class="fn">@'+esc(it.username)+'</div>'
      +'<div class="un">'+esc(it.type||'')+' · '+(it.posted_at?timeAgo(it.posted_at):'')+'</div></div>';
    r.onclick=()=>window.open(it.url,"_blank","noopener");
    $("content").appendChild(r);
  });
}
async function retryFailed(){
  const {data}=await api("/api/retry-failed",{method:"POST"});
  if(data.error){ toast(data.error,"err"); return; }
  if(!data.total){ toast("Nichts zu wiederholen.","ok"); return; }
  toast("Wiederhole "+data.total+" Downloads …");
  showDlBar("Wiederhole Downloads …", 0, data.total);
  if(failedTimer) clearInterval(failedTimer);
  failedTimer=setInterval(async ()=>{
    const {data:st}=await api("/api/retry-failed/status");
    // Ausstieg bei Serverfehler – sonst läuft der Poller endlos weiter.
    if(!st || st.error){ clearInterval(failedTimer); failedTimer=null; hideDlBar(); return; }
    const b=$("retry_btn"); if(b) b.textContent="↻ "+st.done+"/"+st.total+" ("+st.downloaded+" ✓)";
    showDlBar(st.done+"/"+st.total+" · "+st.downloaded+" geladen", st.done, st.total);
    if(!st.running && st.finished){ clearInterval(failedTimer); failedTimer=null; hideDlBar();
      toast("Fertig: "+st.downloaded+" nachgeladen","ok"); if(viewMode==="failed") showFailed(); }
  },1000);
}
// Täglich wechselndes Hintergrundfoto aus den eigenen Downloads laden.
function bgEnabled(){ try{ return localStorage.getItem("igm_bg")!=="0"; }catch(e){ return true; } }
function toggleBg(){
  const on=$("opt_bg").checked;
  try{ localStorage.setItem("igm_bg", on?"1":"0"); }catch(e){}
  if(on){ initBackground(); toast("Tägliches Hintergrundfoto an","ok"); }
  else{ $("bg").style.backgroundImage=""; document.body.classList.remove("hasbg");
    toast("Hintergrundfoto aus","ok"); }
}
function initBackground(){
  if(!bgEnabled()){ document.body.classList.remove("hasbg"); return; }
  const day=new Date().toISOString().slice(0,10);
  const im=new Image();
  im.onload=()=>{ if(!bgEnabled()) return;
    $("bg").style.backgroundImage="url(/background?d="+day+")"; document.body.classList.add("hasbg"); };
  im.onerror=()=>{ document.body.classList.remove("hasbg"); };
  im.src="/background?d="+day;
}
function scrollToLogin(){ $("login_panel").scrollIntoView({behavior:"smooth"}); }

async function importSession(){
  const b=$("imp_btn"); b.disabled=true; b.innerHTML='<span class="spin"></span> Übernehme …';
  const {data}=await api("/api/import-session",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  b.disabled=false; b.textContent="🔓 Aus Browser übernehmen (empfohlen)";
  if(data.ok){ toast("Angemeldet als @"+data.user,"ok"); window._autostart=false; refreshStatus(); }
  else toast(data.error||"Übernahme fehlgeschlagen","err");
}
async function doLogin(){
  const b=$("lg_btn"); b.disabled=true; b.innerHTML='<span class="spin"></span> …';
  const body=JSON.stringify({username:$("lg_user").value,password:$("lg_pass").value,code:$("lg_code").value});
  const {data}=await api("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body});
  b.disabled=false; b.textContent="Anmelden";
  if(data.ok){ $("lg_pass").value=""; $("lg_code").value=""; $("code_row").style.display="none";
    toast("Angemeldet als @"+data.user,"ok"); window._autostart=false; refreshStatus(); }
  else if(data.needs_2fa){ $("code_row").style.display="flex"; toast("Bitte Zwei-Faktor-Code eingeben."); }
  else toast(data.error||"Anmeldung fehlgeschlagen","err");
}
async function doSessionId(){
  const b=$("sid_btn"); b.disabled=true; b.innerHTML='<span class="spin"></span> …';
  const {data}=await api("/api/login-sessionid",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({sessionid:$("lg_sid").value})});
  b.disabled=false; b.textContent="Anmelden";
  if(data.ok){ $("lg_sid").value=""; toast("Angemeldet als @"+data.user,"ok"); window._autostart=false; refreshStatus(); }
  else toast(data.error||"Anmeldung fehlgeschlagen","err");
}
async function logout(){ await api("/api/logout",{method:"POST"}); location.reload(); }

/* ---------- kleine Helfer ---------- */
// Entprellen: Filter-/Sucheingaben nicht bei JEDEM Tastendruck neu rendern.
function debounce(fn, ms){ let t=null; return function(...a){ clearTimeout(t); t=setTimeout(()=>fn.apply(this,a), ms); }; }
// Begrenzte Parallelität: mehrere Downloads gleichzeitig, aber gedeckelt
// (der Server serialisiert die Instagram-Zugriffe ohnehin; das hält nur die
// Oberfläche flott und pipelinet die Anfragen, ohne den Server zu überrennen).
async function runPool(items, worker, concurrency){
  const n = Math.max(1, Math.min(concurrency||3, items.length));
  let i = 0;
  async function next(){ while(i < items.length){ const k = i++; try{ await worker(items[k], k); }catch(e){} } }
  await Promise.all(Array.from({length:n}, next));
}
// Entprellte Fassung des Listen-Filters (als Funktionsdeklaration, damit sie
// aus Inline-Handlern im HTML erreichbar ist).
const _debListView = debounce(()=>applyListView(), 200);
function applyListViewDebounced(){ _debListView(); }

/* ---------- Feed ---------- */
let feedTimer=null, viewMode="feed";
let feedCommitted=[], feedOffset=0, feedTotal=1e9, feedLoading=false, feedObserver=null;
function showFeed(){
  cancelSelect(); pushView("feed", showFeed);
  if(loggedIn && feedCommitted.length){
    // bereits geladenen Feed sofort (aus dem Speicher) anzeigen
    viewMode="feed"; setNav("nav_home"); $("pf_head").style.display="none"; $("subbar").innerHTML="";
    $("stories_row").style.display="flex"; $("filterbar").style.display="flex";
    $("more").innerHTML = feedOffset>=feedTotal ? '<div class="hint">Ende des Feeds</div>' : "";
    renderFeed(feedCommitted); setupFeedObserver();
  } else startFeed();   // noch nichts geladen → Feed (neu) laden, auch beim Zurück
}

async function startFeed(){
  cancelSelect(); pushView("feed", showFeed); $("subbar").innerHTML="";
  viewMode="feed"; setNav("nav_home"); $("pf_head").style.display="none"; $("more").innerHTML="";
  if(loggedIn){ $("stories_row").style.display="flex"; $("filterbar").style.display="flex"; }
  if(!loggedIn){ $("filterbar").style.display="none";
    renderEmpty("Melde dich an, um den Feed deiner Abos zu sehen. Oder suche oben ein öffentliches Konto."); return; }
  if(followList.length===0) loadFollowing(true);   // Stories/Suche im Hintergrund füllen
  feedCommitted=[]; feedOffset=0; feedTotal=1e9; feedLoading=false;
  showSkeleton();
  await loadFeedPage(true);
  setupFeedObserver();
}

async function loadFeedPage(first){
  if(feedLoading) return;
  if(!first && feedOffset>=feedTotal) return;
  feedLoading=true;
  if(!first) $("more").innerHTML='<span class="spin"></span>';
  const {data}=await api("/api/feed/start",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({profiles:$("feed_profiles").value, per:$("feed_per").value, offset:feedOffset})});
  if(data.error){ feedLoading=false; $("more").innerHTML="";
    if(feedCommitted.length===0) renderEmpty(data.error); toast(data.error,"err"); return; }
  if(data.followees_total!==undefined) feedTotal=data.followees_total;
  if(data.total===0){ feedLoading=false; feedOffset=feedTotal;
    $("more").innerHTML = feedCommitted.length ? '<div class="hint">Ende des Feeds</div>' : "";
    if(feedCommitted.length===0) renderEmpty("Du folgst noch keinem Konto."); return; }
  await pollFeedPage();
  feedOffset = (data.next_offset!==undefined) ? data.next_offset : (feedOffset + data.total);
  feedLoading=false;
  $("more").innerHTML = feedOffset>=feedTotal ? '<div class="hint">Ende des Feeds</div>' : "";
}

function pollFeedPage(){
  return new Promise(resolve=>{
    let lastLen=-1;
    const t=setInterval(async ()=>{
      const {data:st}=await api("/api/feed/status");
      if(viewMode!=="feed"){ clearInterval(t); resolve(); return; }
      // Ausstieg bei Serverfehler/Abbruch – sonst würde der Poller ewig
      // weiterlaufen und feedLoading bliebe hängen.
      if(!st || st.error){ clearInterval(t); feedLoading=false; resolve(); return; }
      const items=st.items||[];
      // Nur neu aufbauen, wenn wirklich neue Beiträge dazugekommen sind –
      // nicht mehr bei jedem Sekunden-Tick (verhindert Flackern & CPU-Last).
      if(items.length!==lastLen){ lastLen=items.length; renderFeed(feedCommitted.concat(items)); }
      if(!st.running && st.finished){ clearInterval(t);
        feedCommitted=feedCommitted.concat(items); resolve(); }
    },1000);
  });
}

function setupFeedObserver(){
  if(feedObserver) feedObserver.disconnect();
  feedObserver=new IntersectionObserver(es=>{
    if(es[0].isIntersecting && viewMode==="feed" && !feedLoading && feedOffset<feedTotal){
      loadFeedPage(false);
    }
  },{rootMargin:"500px"});
  feedObserver.observe($("more"));
}
function showSkeleton(){
  $("content").innerHTML = '<div class="sk sk-post"></div><div class="sk sk-post"></div>';
}
function renderEmpty(msg){ $("content").innerHTML = '<div class="empty">'+esc(msg)+'</div>'; }

let feedPosts=[], currentFilter="all";
function matchFilter(p){
  if(currentFilter==="all") return true;
  if(currentFilter==="album") return p.is_album;
  if(currentFilter==="video") return p.is_video && !p.is_album;
  if(currentFilter==="photo") return !p.is_video && !p.is_album;
  return true;
}
function renderFeed(posts){
  feedPosts = posts;
  const c=$("content"); c.innerHTML="";
  const shown = posts.filter(matchFilter);
  if(posts.length===0){ showSkeleton(); return; }
  if(shown.length===0){ renderEmpty("Keine Beiträge für diesen Filter."); return; }
  for(const p of shown) c.appendChild(postCard(p));
  setupFeedVideos();
}

/* Stummes Autoplay des gerade sichtbaren Feed-Videos (bandbreitenschonend:
   es wird immer nur das aktuell sichtbare Video geladen/abgespielt). */
function autoplayOn(){ try{ return localStorage.getItem("igm_autoplay")!=="0"; }catch(e){ return true; } }
let feedVidObserver=null;
function setupFeedVideos(){
  if(feedVidObserver) feedVidObserver.disconnect();
  if(!autoplayOn()) return;   // Datensparmodus: kein Autoplay im Feed
  feedVidObserver=new IntersectionObserver(es=>{
    es.forEach(en=>{
      if(en.isIntersecting && en.intersectionRatio>=0.7) activateFeedVideo(en.target);
      else deactivateFeedVideo(en.target);
    });
  },{threshold:[0,0.7,1]});
  document.querySelectorAll("#content .post .media[data-v]").forEach(m=>feedVidObserver.observe(m));
}
function activateFeedVideo(m){
  let v=m.querySelector("video");
  if(!v){
    v=document.createElement("video");
    v.src="/media/"+m.dataset.v+"?inline=1";
    v.muted=true; v.loop=true; v.playsInline=true; v.preload="metadata";
    v.style.cssText="position:absolute;inset:0;width:100%;height:100%;object-fit:cover";
    m.appendChild(v);
    const img=m.querySelector("img"); if(img) img.style.visibility="hidden";
    const play=m.querySelector(".play"); if(play) play.style.display="none";
  }
  v.play().catch(()=>{});
}
function deactivateFeedVideo(m){
  const v=m.querySelector("video");
  if(v){ v.pause();
    // Quelle freigeben, damit nicht viele Videos gleichzeitig laden
    v.removeAttribute("src"); try{ v.load(); }catch(e){} v.remove();
    const img=m.querySelector("img"); if(img) img.style.visibility="";
    const play=m.querySelector(".play"); if(play) play.style.display="";
  }
}
function setFilter(f, btn){
  currentFilter=f;
  document.querySelectorAll("#filterbar .chip[data-f]").forEach(c=>c.classList.remove("on"));
  if(btn) btn.classList.add("on");
  renderFeed(feedPosts);
}
async function downloadFeedToPC(){
  const list=feedPosts.filter(matchFilter).filter(p=>!p.downloaded);
  if(list.length===0){ toast("Nichts Neues zum Laden.","ok"); return; }
  if(!confirm(list.length+" Beiträge aus dem Feed auf den PC laden?")) return;
  let n=0;
  await runPool(list, async (p)=>{
    await api("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({shortcode:p.shortcode, username:p.username})});
    toast("Lädt "+(++n)+"/"+list.length+" …");
  }, 3);
  toast("Feed-Download fertig ("+list.length+")","ok");
}
function setNav(id){ document.querySelectorAll(".bottomnav button").forEach(b=>b.classList.remove("on"));
  const b=$(id); if(b) b.classList.add("on"); }

/* Gespeicherte Beiträge + Sammlungen (lokal) */
function getColls(){ try{ return JSON.parse(localStorage.getItem("igm_colls")||"{}"); }catch(e){ return {}; } }
function setColls(o){ try{ localStorage.setItem("igm_colls", JSON.stringify(o)); }catch(e){} }
let currentColl="__all";
function showSaved(){
  cancelSelect(); pushView("saved", showSaved);
  viewMode="saved"; setNav("nav_saved");
  if(feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("filterbar").style.display="none"; $("stories_row").style.display="none";
  $("pf_head").style.display="flex"; $("pf_head").className="pf-head";
  $("more").innerHTML="";
  const saved=getSaved(), colls=getColls();
  // Sammlungs-Chips
  let chips='<div class="filterbar" style="padding:8px 2px">';
  chips+=`<button class="chip ${currentColl==='__all'?'on':''}" onclick="setColl('__all')">Alle (${saved.length})</button>`;
  for(const [name,scs] of Object.entries(colls))
    chips+=`<button class="chip ${currentColl===name?'on':''}" onclick="setColl('${esc(name).replace(/'/g,"\\'")}')">🔖 ${esc(name)} (${(scs||[]).length})</button>`;
  chips+='<button class="chip" onclick="newCollection()">+ Neu</button></div>';
  $("subbar").innerHTML=chips;
  // gefilterte Liste
  let list=saved;
  if(currentColl!=="__all"){ const set=new Set(colls[currentColl]||[]); list=saved.filter(p=>set.has(p.shortcode)); }
  $("pf_head").innerHTML='<div class="ph">🔖</div><div><div class="fn">Gespeichert</div>'
    +'<div class="un">'+list.length+' Beiträge'+(currentColl!=="__all"?' in „'+esc(currentColl)+'“':'')+' (lokal)</div></div>';
  if(list.length===0){ renderEmpty(currentColl==="__all"?"Noch nichts gemerkt. Tippe ★ oder doppeltippe ein Bild.":"Diese Sammlung ist leer."); return; }
  renderGrid(list);
}
function setColl(name){ currentColl=name; showSaved(); }
function newCollection(){
  const name=(prompt("Name der neuen Sammlung:")||"").trim();
  if(!name || name==="__all") return;
  const colls=getColls(); if(!colls[name]) colls[name]=[]; setColls(colls);
  currentColl=name; showSaved();
}
function askCollection(){
  const colls=getColls(); const names=Object.keys(colls);
  const hint = names.length ? (" Vorhanden: "+names.join(", ")) : "";
  const name=(prompt("In welche Sammlung? (Name eingeben – neu oder vorhanden)"+hint)||"").trim();
  return (!name || name==="__all") ? "" : name;
}
function addSelectedToCollection(){
  const scs=[...selectedGrid]; if(scs.length===0){ toast("Nichts ausgewählt.","err"); return; }
  const name=askCollection(); if(!name) return;
  const colls=getColls(); const set=new Set(colls[name]||[]); scs.forEach(s=>set.add(s));
  colls[name]=[...set]; setColls(colls);
  toast(scs.length+" zu „"+name+"“ hinzugefügt","ok"); cancelSelect(); showSaved();
}
function removeSelectedFromCollection(){
  const scs=new Set(selectedGrid); if(scs.size===0) return;
  const colls=getColls(); colls[currentColl]=(colls[currentColl]||[]).filter(s=>!scs.has(s)); setColls(colls);
  toast("Aus „"+currentColl+"“ entfernt","ok"); cancelSelect(); showSaved();
}

function avatarHTML(username, cls){
  const url = followAvatar[username];
  return url ? '<img class="'+cls+'" src="'+url+'" alt="">'
             : '<div class="ph '+cls+'">'+esc((username||"?")[0].toUpperCase())+'</div>';
}

function postCard(p){
  const el=document.createElement("article"); el.className="post";
  const saved=isSaved(p.shortcode);
  el.innerHTML = `
    <div class="head">
      ${avatarHTML(p.username,"")}
      <span class="u" style="cursor:pointer">@${esc(p.username)}</span>
      <span class="t" title="${esc((p.posted_at||'').replace('T',' '))}">${timeAgo(p.posted_at)}</span>
    </div>
    <div class="media">
      ${p.thumb ? `<img loading="lazy" src="${p.thumb}" alt="">` : `<div class="empty">keine Vorschau</div>`}
      ${p.is_video ? `<span class="play">▶</span>` : ""}
      ${p.is_album ? `<span class="multi">▦</span>` : ""}
      <span class="heart">♥</span>
    </div>
    <div class="actions">
      <button class="a-dev" title="Auf dieses Gerät">⤓</button>
      <button class="a-pc ${p.downloaded?'done':''}" title="Auf den PC">${p.downloaded?'✓':'⬇'}</button>
      <button class="a-copy" title="Link kopieren">🔗</button>
      <a href="${p.url}" target="_blank" rel="noopener" title="Auf Instagram öffnen">↗</a>
      <span class="sp"></span>
      <button class="a-save ${saved?'saved':''}" title="Merken">${saved?'★':'☆'}</button>
    </div>
    ${(p.likes||p.comments) ? `<div class="stats">${p.likes?('❤ '+fmt(p.likes)+' „Gefällt mir“'):''}${p.likes&&p.comments?' · ':''}${p.comments?('💬 '+fmt(p.comments)+' Kommentare'):''}</div>` : ""}
    ${p.caption ? `<div class="cap clamp"><span class="u">@${esc(p.username)}</span><span class="txt">${linkify(p.caption)}</span> <span class="more">… mehr</span></div>` : ""}
  `;
  const media=el.querySelector(".media");
  if(p.is_video) media.dataset.v=p.shortcode;
  media.onclick=()=>openLightbox(p);
  let lastTap=0;
  media.addEventListener("dblclick", ()=>likeAnim(el,p));
  media.addEventListener("touchend", ()=>{ const n=Date.now(); if(n-lastTap<300){ likeAnim(el,p); } lastTap=n; });
  el.querySelector(".u").onclick=()=>openProfile(p.username);
  el.querySelector(".a-dev").onclick=()=>deviceDownload(p.shortcode);
  el.querySelector(".a-copy").onclick=()=>shareOrCopy(p);
  el.querySelector(".a-pc").onclick=(e)=>pcDownload(p.shortcode, e.currentTarget);
  const sv=el.querySelector(".a-save");
  sv.onclick=()=>{ const s=toggleSavedPost(p); sv.textContent=s?'★':'☆'; sv.classList.toggle('saved',s);
    toast(s?"Gemerkt":"Entfernt","ok"); };
  const more=el.querySelector(".more");
  if(more) more.onclick=()=>el.querySelector(".cap").classList.toggle("clamp");
  return el;
}
function likeAnim(el, p){
  const h=el.querySelector(".heart"); if(h){ h.classList.remove("go"); void h.offsetWidth; h.classList.add("go"); }
  if(!isSaved(p.shortcode)){ toggleSavedPost(p);
    const sv=el.querySelector(".a-save"); if(sv){ sv.textContent='★'; sv.classList.add('saved'); } toast("Gemerkt","ok"); }
}
function deviceDownload(sc){ window.location.href = "/media/"+sc; }
async function pcDownload(sc, btn){
  const b=btn||document.querySelector(".a-pc");
  if(!b||b.classList.contains("done")) return;
  b.disabled=true; b.innerHTML='<span class="spin"></span>';
  const {data}=await api("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({shortcode:sc})});
  b.disabled=false;
  if(data.ok){ b.textContent="✓"; b.classList.add("done"); toast("Auf PC gespeichert","ok"); }
  else { b.textContent="⬇"; toast(data.error||"Download fehlgeschlagen","err"); }
}

/* ---------- Lightbox ---------- */
let lbItems=[], lbIndex=0, lbPost=null;
async function openLightbox(p){
  lbPost=p; lbIndex=0;
  if(p.is_album){
    const {data}=await api("/api/album/"+p.shortcode);
    lbItems = (data.items||[]).map(it=>({media:it.media, download:it.download, is_video:it.is_video}));
    if(lbItems.length===0) lbItems=[{media:"/media/"+p.shortcode+"?inline=1", download:"/media/"+p.shortcode, is_video:p.is_video}];
  }else{
    lbItems=[{media:"/media/"+p.shortcode+"?inline=1", download:"/media/"+p.shortcode, is_video:p.is_video}];
  }
  $("lb_cap").innerHTML = '<b>@'+esc(p.username)+'</b> '+esc(p.caption||"");
  $("lb").classList.add("open");
  drawLightbox();
}
function drawLightbox(){
  const it=lbItems[lbIndex];
  $("lb_stage").innerHTML = it.is_video
    ? `<video src="${it.media}" controls autoplay playsinline></video>`
    : `<img src="${it.media}" alt="">`;
  const multi = lbItems.length>1;
  $("lb_l").style.display = multi ? "block":"none";
  $("lb_r").style.display = multi ? "block":"none";
  $("lb_dots").innerHTML = multi ? lbItems.map((_,i)=>'<i class="'+(i===lbIndex?'on':'')+'"></i>').join("") : "";
  $("lb_dl").innerHTML = `<a href="${it.download}" title="Auf dieses Gerät">⤓ Gerät</a>`
    + `<button onclick="pcDownloadFromLb()" title="Auf den PC">⬇ PC</button>`
    + `<button onclick="toggleComments()" title="Kommentare">💬</button>`;
}
function lbNav(d){ lbIndex=(lbIndex+d+lbItems.length)%lbItems.length; drawLightbox(); }
function closeLightbox(){ $("lb").classList.remove("open"); $("lb_stage").innerHTML="";
  $("lb_cmts").classList.remove("open"); $("lb_cmts").innerHTML=""; }
async function toggleComments(){
  const sheet=$("lb_cmts");
  if(sheet.classList.contains("open")){ sheet.classList.remove("open"); return; }
  sheet.innerHTML='<div class="empty"><span class="spin"></span></div>'; sheet.classList.add("open");
  const {data}=await api("/api/comments/"+lbPost.shortcode);
  if(!$("lb").classList.contains("open")) return;
  if(data.error){ sheet.innerHTML='<h4>Kommentare <button onclick="toggleComments()">✕</button></h4><div class="hint">'+esc(data.error)+'</div>'; return; }
  const cs=data.comments||[];
  let html='<h4>Kommentare ('+cs.length+') <button onclick="toggleComments()">✕</button></h4>';
  if(cs.length===0) html+='<div class="hint">Keine Kommentare.</div>';
  for(const c of cs){
    html+='<div class="cmt"><div><span class="cu">@'+esc(c.username)+'</span>'+linkify(c.text)
      +'<div class="cl">'+timeAgo(c.posted_at)+(c.likes?(' · '+fmt(c.likes)+' ❤'):'')+'</div></div></div>';
  }
  sheet.innerHTML=html;
}
function pcDownloadFromLb(){ if(lbPost) pcDownload(lbPost.shortcode); }

/* ---------- Stories ---------- */
function renderStoriesRow(){
  const row=$("stories_row");
  if(!loggedIn || followList.length===0){ row.style.display="none"; return; }
  row.style.display="flex"; row.innerHTML="";
  const seen=seenStories();
  for(const a of followList.slice(0,30)){
    const d=document.createElement("div"); d.className="story";
    const ring = seen.includes(a.username) ? "ring seen" : "ring";
    d.innerHTML = `<div class="${ring}">${a.avatar?`<img src="${a.avatar}" alt="">`:`<div class="ph">@</div>`}</div>`
      + `<span>${esc(a.username)}</span>`;
    d.onclick=()=>openStory(a.username);
    row.appendChild(d);
  }
}
let svItems=[], svIndex=0, svTimer=null;
async function openStory(username){
  toast("Lade Stories von @"+username+" …");
  const {data}=await api("/api/stories?username="+encodeURIComponent(username));
  if(data.error){ toast(data.error,"err"); return; }
  if(!data.items || data.items.length===0){ toast("Keine aktuellen Stories.","ok"); return; }
  svItems=data.items; svIndex=0; svUser=username;
  markSeen(username); renderStoriesRow();
  $("sv").classList.add("open");
  drawStory();
}
let svUser="";
function drawStory(){
  clearTimeout(svTimer);
  const it=svItems[svIndex];
  $("sv_stage").innerHTML = it.is_video
    ? `<video src="${it.media}" controls autoplay playsinline onended="storyNav(1)"></video>`
    : `<img src="${it.media}" alt="">`;
  $("sv_bar").innerHTML = svItems.map((_,i)=>`<i><b style="width:${i<svIndex?100:0}%"></b></i>`).join("");
  $("sv_dl").innerHTML = `<b style="color:#fff;align-self:center">@${esc(svUser)}</b>`
    + `<a href="${it.download}">⤓ Gerät</a>`;
  if(!it.is_video){
    const bar=$("sv_bar").children[svIndex].firstChild;
    bar.style.transition="width 4s linear"; requestAnimationFrame(()=>bar.style.width="100%");
    svTimer=setTimeout(()=>storyNav(1), 4200);
  }
}
function storyNav(d){
  svIndex+=d;
  if(svIndex<0) svIndex=0;
  else if(svIndex>=svItems.length){ closeStory(); return; }
  else drawStory();
}
function closeStory(){ clearTimeout(svTimer); $("sv").classList.remove("open"); $("sv_stage").innerHTML=""; }

/* ---------- Reels-Vollbild-Feed ---------- */
let reelsObserver=null;
async function openReels(){
  setNav("nav_reels");
  let vids=(feedPosts||[]).filter(p=>p.is_video);
  if(vids.length===0 && loggedIn){
    toast("Sammle Reels aus deinen Abos …");
    await startFeed();                       // Feed still aufbauen …
    vids=(feedPosts||[]).filter(p=>p.is_video);
  }
  if(vids.length===0){ toast("Keine Videos/Reels gefunden – im Feed weiter scrollen.","ok"); return; }
  const track=$("reels_track"); track.innerHTML="";
  for(const p of vids){
    const slide=document.createElement("div"); slide.className="reel";
    slide.innerHTML=`
      <video data-src="/media/${p.shortcode}?inline=1" loop muted playsinline preload="none"></video>
      <div class="muted">🔇 tippen für Ton</div>
      <div class="info"><span class="u">@${esc(p.username)}</span><div class="cp">${esc(p.caption||"")}</div></div>
      <div class="side">
        <div>❤<small>${fmt(p.likes||0)}</small></div>
        <div>💬<small>${fmt(p.comments||0)}</small></div>
        <a href="/media/${p.shortcode}" title="Auf Gerät">⤓</a>
        <button class="rpc" title="Auf PC">⬇</button>
      </div>`;
    const v=slide.querySelector("video");
    slide.querySelector(".muted").onclick=()=>{ v.muted=!v.muted; slide.querySelector(".muted").textContent=v.muted?"🔇 tippen für Ton":"🔊 Ton an"; if(!v.muted) v.play(); };
    v.onclick=()=>{ v.paused?v.play():v.pause(); };
    slide.querySelector(".u").onclick=()=>{ closeReels(); openProfile(p.username); };
    slide.querySelector(".rpc").onclick=(e)=>pcDownload(p.shortcode, e.currentTarget);
    track.appendChild(slide);
  }
  $("reels").classList.add("open");
  if(reelsObserver) reelsObserver.disconnect();
  reelsObserver=new IntersectionObserver(es=>{
    es.forEach(en=>{ const v=en.target.querySelector("video");
      if(!v) return;
      if(en.isIntersecting){
        // Videoquelle erst beim Sichtbarwerden setzen (lazy) – nicht alle
        // Reels auf einmal laden.
        if(!v.src && v.dataset.src){ v.src=v.dataset.src; }
        v.play().catch(()=>{});
      } else { v.pause(); }
    });
  },{threshold:0.6});
  track.querySelectorAll(".reel").forEach(r=>reelsObserver.observe(r));
  // erstes Video starten (Quelle setzen, falls der Observer noch nicht feuerte)
  const first=track.querySelector("video");
  if(first){ if(!first.src && first.dataset.src){ first.src=first.dataset.src; } first.play().catch(()=>{}); }
}
function closeReels(){
  $("reels").classList.remove("open");
  if(reelsObserver) reelsObserver.disconnect();
  $("reels_track").querySelectorAll("video").forEach(v=>v.pause());
  $("reels_track").innerHTML="";
}

/* ---------- Pull-to-Refresh (Handy) ---------- */
let ptrStart=null;
window.addEventListener("touchstart", e=>{ if(window.scrollY<=0 && viewMode==="feed") ptrStart=e.touches[0].clientY; else ptrStart=null; }, {passive:true});
window.addEventListener("touchmove", e=>{
  if(ptrStart===null) return;
  const dy=e.touches[0].clientY-ptrStart;
  if(dy>60){ $("ptr").classList.add("show"); } else { $("ptr").classList.remove("show"); }
}, {passive:true});
window.addEventListener("touchend", e=>{
  if(ptrStart!==null && $("ptr").classList.contains("show")){
    $("ptr").classList.remove("show"); toast("Aktualisiere …"); startFeed();
  }
  ptrStart=null;
}, {passive:true});

/* ---------- Profil-Ansicht (Raster) ---------- */
let profileName="", profileCount=24;
async function openProfile(input, count){
  const name=(input||"").trim().replace(/^@/,""); if(!name) return;
  cancelSelect();
  pushView("profile:"+name.toLowerCase(), ()=>openProfile(name, count));
  profileName=name; profileCount=count||24;
  // Gesehen-Marke setzen: Die NEU-Markierung in der Gefolgt-/Follower-
  // Liste erlischt für dieses Konto (rein lokal, kein Instagram-Abruf).
  api("/api/seen",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username:name})});
  const isOwn = currentUser && name.toLowerCase()===currentUser.toLowerCase();
  viewMode="profile"; setNav(isOwn?"nav_profile":"nav_search"); $("subbar").innerHTML="";
  if(feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  const acc = followList.find(a=>a.username.toLowerCase()===name.toLowerCase());
  $("pf_head").style.display="flex"; $("pf_head").className="pf-head";
  $("pf_head").innerHTML = (acc&&acc.avatar?`<img src="${acc.avatar}">`:`<div class="ph">@</div>`)
    + `<div><div class="fn">@${esc(name)}</div><div class="un">lädt …</div></div>`;
  $("content").innerHTML='<div class="empty"><span class="spin" style="border-top-color:var(--accent)"></span></div>';
  $("more").innerHTML="";
  api(isOwn ? "/api/me" : "/api/profile/"+encodeURIComponent(name)).then(({data})=>{
    if(data && !data.error && viewMode==="profile" && profileName===name) renderProfileHeader(data);
  });
  const {data}=await api("/api/posts?username="+encodeURIComponent(name)+"&count="+profileCount);
  if(viewMode!=="profile" || profileName!==name) return;
  if(data.error){ renderEmpty(data.error); toast(data.error,"err"); return; }
  const posts=data.posts||[];
  if(posts.length===0){ renderEmpty("Keine Beiträge gefunden."); return; }
  renderGrid(posts);
  $("more").innerHTML = posts.length>=profileCount
    ? '<button class="btn ghost" onclick="openProfile(profileName, profileCount+24)">Mehr laden</button>' : "";
}
function renderProfileHeader(info){
  const isOwn = currentUser && info.username.toLowerCase()===currentUser.toLowerCase();
  $("pf_head").innerHTML = `
    ${info.avatar?`<img src="${info.avatar}">`:`<div class="ph">@</div>`}
    <div style="flex:1;min-width:0">
      <div class="fn">${esc(info.full_name||info.username)}${info.is_verified?'<span class="verified">✔</span>':''}</div>
      <div class="un">@${esc(info.username)}${info.is_private?' · privat':''}</div>
      <div class="pf-stats">
        <div class="s"><b>${fmt(info.posts)}</b>Beiträge</div>
        <div class="s ${isOwn?'link':''}" id="pf_followers"><b>${fmt(info.followers)}</b>Follower</div>
        <div class="s ${isOwn?'link':''}" id="pf_following"><b>${fmt(info.following)}</b>Gefolgt</div>
      </div>
      ${info.biography?`<div class="pf-bio">${linkify(info.biography)}</div>`:''}
      <div class="pf-actions" style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
        <button class="btn ghost small" id="pf_dl_fotos">⬇ Fotos</button>
        <button class="btn ghost small" id="pf_dl_videos">⬇ Videos</button>
        <button class="btn ghost small" id="pf_dl_story">⬇ Story</button>
        <button class="btn ghost small" id="pf_dl_high">⬇ Highlights</button>
        <button class="btn ghost small" id="pf_dl_all" title="Alles: Beiträge, Videos, Stories & Highlights">⬇ Alles</button>
      </div>
      <div class="pf-files" id="pf_files">📥 lädt Zähler …</div>
    </div>`;
  const u=info.username;
  $("pf_dl_fotos").onclick=()=>downloadAccountType(u,"photos");
  $("pf_dl_videos").onclick=()=>downloadAccountType(u,"videos");
  $("pf_dl_story").onclick=()=>downloadStoryKind(u,"stories");
  $("pf_dl_high").onclick=()=>downloadStoryKind(u,"highlights");
  $("pf_dl_all").onclick=()=>downloadAccount(u);
  loadAccountFiles(u);
  if(isOwn){ const f1=$("pf_followers"), f2=$("pf_following");
    if(f1) f1.onclick=showFollowers; if(f2) f2.onclick=showFollowing; }
  else pushRecent({username:info.username, full_name:info.full_name, avatar:info.avatar});
}
function showMe(){ if(!loggedIn){ toast("Bitte zuerst anmelden."); scrollToLogin(); return; } openProfile(currentUser); }

/* ---------- Suche & Entdecken ---------- */
const EXPLORE=["reisen","natur","food","kunst","musik","sport","fashion","auto"];
function showSearch(){
  cancelSelect(); pushView("search", showSearch); $("subbar").innerHTML="";
  viewMode="search"; setNav("nav_search");
  $("login_panel").style.display=loggedIn?"none":"block";
  if(feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("pf_head").style.display="none"; $("more").innerHTML="";
  // Bei Instagram gesucht wird NUR auf Knopfdruck (🔍 oder Enter).
  // Beim Tippen wird lediglich die lokale Gefolgt-/Follower-Liste
  // gefiltert – das ist kein Instagram-Abruf.
  $("content").innerHTML = '<div class="searchbox" style="display:flex;gap:8px;align-items:center">'
    +'<input id="sq" placeholder="Konten oder #Hashtag suchen …" autocomplete="off" style="flex:1">'
    +'<button class="btn" onclick="doSearch($(\'sq\').value)">🔍 Suchen</button>'
    +'</div><div id="slocal"></div><div id="sresults"></div>';
  renderSearchStart();
  renderLocalAccounts("");
  const inp=$("sq"); inp.focus();
  inp.onkeydown=(e)=>{ if(e.key==='Enter') doSearch(inp.value); };
  inp.oninput=debounce(()=>renderLocalAccounts(inp.value), 200);
}
// Gefolgt/Follower direkt in der Suche – aus dem lokalen Speicher, mit
// Mehrfachauswahl und Sammel-Download (wie in der Kontenliste).
let searchKind="following";
function renderLocalAccounts(q){
  const box=$("slocal"); if(!box) return;
  const nF=getFollowCache("following").length, nW=getFollowCache("followers").length;
  const accts=getFollowCache(searchKind);
  q=(q||"").trim().toLowerCase().replace(/^@/,"");
  const hits=(q && q[0]!=="#")
    ? accts.filter(a=>((a.username||"")+" "+(a.full_name||"")).toLowerCase().includes(q))
    : accts;
  let html='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip '+(searchKind==="following"?"on":"")+'" onclick="setSearchKind(\'following\')">Gefolgt'+(nF?" ("+nF+")":"")+'</button>'
    +'<button class="chip '+(searchKind==="followers"?"on":"")+'" onclick="setSearchKind(\'followers\')">Follower'+(nW?" ("+nW+")":"")+'</button>';
  if(accts.length){
    html+='<button class="chip" onclick="selectAllAccts(true)">Alle</button>'
      +'<button class="chip" onclick="selectAllAccts(false)">Keine</button>'
      +'<span class="chip" style="cursor:default">je Konto: <select id="acct_count" style="background:transparent;border:0;color:inherit"><option>3</option><option selected>12</option><option>24</option><option value="30">alle (max 30)</option></select></span>'
      +'<button class="chip" id="acct_dl" onclick="downloadFollowSelected()">⬇ Herunterladen ('+followSel.size+')</button>'
      +'<button class="chip" title="Liste neu von Instagram laden" onclick="loadListIntoSearch(true)">↻</button>';
  } else if(loggedIn){
    html+='<button class="chip" onclick="loadListIntoSearch()">Liste laden</button>';
  }
  html+='</div><div id="slocal_rows"></div>';
  box.innerHTML=html;
  const rows=$("slocal_rows");
  if(accts.length && hits.length===0){
    rows.innerHTML='<div class="hint" style="margin:4px">Kein Treffer in deiner Liste – 🔍 sucht bei Instagram.</div>';
  }
  hits.slice(0,400).forEach(a=>rows.appendChild(accountRow(a,true)));
  rows.querySelectorAll(".acctrow").forEach(r=>{ const cb=r.querySelector(".acb");
    if(cb && followSel.has(r.dataset.u)) cb.checked=true; });
}
function setSearchKind(k){ if(searchKind!==k){ searchKind=k; followSel.clear(); }
  renderLocalAccounts($("sq")?$("sq").value:""); }
async function loadListIntoSearch(fresh){
  if(!loggedIn){ toast("Bitte zuerst anmelden."); scrollToLogin(); return; }
  const rows=$("slocal_rows"); if(rows) rows.innerHTML='<div class="empty"><span class="spin"></span></div>';
  // ↻ = frisch von Instagram (fresh); „Liste laden“ nutzt den schnellen Cache.
  const base=searchKind==="followers"?"/api/followers":"/api/following";
  const {data}=await api(base+(fresh?"?fresh=1":""));
  if(data.error){ toast(data.error,"err"); renderLocalAccounts($("sq")?$("sq").value:""); return; }
  const accts=data.accounts||[];
  saveFollowCache(searchKind, accts);
  if(searchKind==="following") followList=accts;
  renderLocalAccounts($("sq")?$("sq").value:"");
}
let exploreCache=null;
function renderSearchStart(){
  const box=$("sresults"); if(!box) return;
  const rec=recentViewed();
  let html='<div class="filterbar" style="padding-top:8px">'
    + EXPLORE.map(t=>`<button class="chip" onclick="searchTag('${t}')">#${t}</button>`).join("")+'</div>';
  if(rec.length){ html+='<div class="hint" style="margin:12px 4px 4px">Zuletzt angesehen</div><div id="recent"></div>'; }
  html+='<div class="hint" style="margin:14px 4px 4px" id="explore_title">Entdecken</div><div id="explore_grid"></div>';
  box.innerHTML=html;
  if(rec.length){ const rc=$("recent"); rec.forEach(a=>rc.appendChild(accountRow(a))); }
  // Entdecken lädt NICHT automatisch – nur auf Klick.
  if(exploreCache){ renderExplore(exploreCache); }
  else if($("explore_title")) $("explore_title").innerHTML=
    'Entdecken <button class="chip" onclick="loadExplore()">Laden</button>';
}
function searchTag(t){ const inp=$("sq"); if(inp) inp.value="#"+t; doSearch("#"+t); }
async function loadExplore(){
  const g=$("explore_grid"); if(!g) return;
  const tag=EXPLORE[Math.floor(Math.random()*EXPLORE.length)];
  if($("explore_title")) $("explore_title").innerHTML='Entdecken · #'+tag+' <span class="spin"></span>';
  const {data}=await api("/api/hashtag/"+encodeURIComponent(tag)+"?count=18");
  if(!$("explore_grid")) return;
  if(data.error || !(data.posts||[]).length){
    if($("explore_title")) $("explore_title").textContent="Entdecken (aktuell nicht verfügbar)";
    return;
  }
  exploreCache={tag, posts:data.posts};
  renderExplore(exploreCache);
}
function renderExplore(c){
  if($("explore_title")) $("explore_title").innerHTML='Entdecken · #'+esc(c.tag)+' <button class="chip" onclick="loadExplore()">↻</button>';
  if($("explore_grid")) renderGrid(c.posts, $("explore_grid"));
}
async function doSearch(q){
  q=(q||"").trim();
  if(!$("sresults")) return;
  if(q[0]==="#"){ return doHashtag(q.slice(1)); }
  if(q.length<2){ renderSearchStart(); return; }
  $("sresults").innerHTML='<div class="empty"><span class="spin"></span></div>';
  const {data}=await api("/api/search?q="+encodeURIComponent(q));
  if(!$("sresults")) return;
  if(data.error){ $("sresults").innerHTML='<div class="hint">'+esc(data.error)+'</div>'; return; }
  const rs=data.results||[];
  if(rs.length===0){ $("sresults").innerHTML='<div class="hint">Keine Treffer. Für Hashtags „#" davor setzen.</div>'; return; }
  $("sresults").innerHTML=""; rs.forEach(a=>$("sresults").appendChild(accountRow(a)));
}
async function doHashtag(tag){
  tag=(tag||"").trim().replace(/^#/,""); if(tag.length<2){ $("sresults").innerHTML='<div class="hint">Hashtag zu kurz.</div>'; return; }
  $("sresults").innerHTML='<div class="empty"><span class="spin"></span></div>';
  const {data}=await api("/api/hashtag/"+encodeURIComponent(tag)+"?count=24");
  if(!$("sresults")) return;
  if(data.error){ $("sresults").innerHTML='<div class="hint">'+esc(data.error)+'</div>'; return; }
  const posts=data.posts||[];
  if(posts.length===0){ $("sresults").innerHTML='<div class="hint">Nichts zu #'+esc(tag)+' gefunden.</div>'; return; }
  $("sresults").innerHTML='<div class="hint" style="margin:4px">Neueste Beiträge zu #'+esc(tag)+'</div><div id="hgrid"></div>';
  renderGrid(posts, $("hgrid"));
}

/* zuletzt angesehene Profile (lokal) */
function recentViewed(){ try{ return JSON.parse(localStorage.getItem("igm_recent")||"[]"); }catch(e){ return []; } }
function pushRecent(a){
  if(!a || !a.username) return;
  let r=recentViewed().filter(x=>x.username!==a.username);
  r.unshift({username:a.username, full_name:a.full_name||"", avatar:a.avatar||""});
  try{ localStorage.setItem("igm_recent", JSON.stringify(r.slice(0,12))); }catch(e){}
}
function accountRow(a, selectable){
  const r=document.createElement("div"); r.className="acctrow"; r.dataset.u=a.username;
  r.dataset.g=a.group||"";
  const cb = selectable ? '<input type="checkbox" class="acb" style="width:18px;height:18px;flex:0 0 auto">' : "";
  // Optional: „Zuletzt gepostet“-Zeile (kommt aus der lokalen Datenbank)
  const lp = a._lp!==undefined
    ? '<div class="un lastp">'+(a._new?'<b style="color:var(--ok)">● NEU</b> · ':'')
      +(a._lp?('🕐 zuletzt gepostet: '+timeAgo(a._lp)):'🕐 noch keine Beiträge bekannt – „Neue prüfen“')+'</div>'
    : '';
  const ring = a.has_story ? ' class="storyring" title="hat gerade eine Story"' : '';
  const grp = a.group ? '<span class="grpchip">🏷 '+esc(a.group)+'</span>' : '';
  // In der Suche (a.following ist dort gesetzt): „+ Folgen"-Umschalter, der
  // das Konto zur lokalen Liste hinzufügt/entfernt (kein echter IG-Follow).
  const canFollow = a.following!==undefined;
  const folBtn = canFollow
    ? '<button class="favbtn folbtn'+(a.following?' on':'')+'" title="'
      +(a.following?'In deiner Liste – tippen zum Entfernen':'Zu deiner Liste hinzufügen')
      +'">'+(a.following?'✓ Gefolgt':'+ Folgen')+'</button>'
    : '';
  // ⭐ Anpinnen: in Gefolgt-/Follower-Listen UND in Suchtreffern (beide
  // liefern das Feld favorite). 🏷 Gruppe nur in den Listen.
  const favBtn = a.favorite!==undefined
    ? '<button class="favbtn" title="Anpinnen (immer oben)">'+(a.favorite?'⭐':'☆')+'</button>'
    : '';
  const grpBtn = (a.favorite!==undefined && !canFollow)
    ? '<button class="favbtn grpbtn" title="Gruppe zuweisen">🏷</button>'
    : '';
  const extras = folBtn + favBtn + grpBtn;
  r.innerHTML = cb + (a.avatar?`<img loading="lazy" decoding="async" src="${a.avatar}"${ring}>`:`<div class="ph${a.has_story?' storyring':''}">@</div>`)
    + `<div style="flex:1;min-width:0"><div class="fn">${esc(a.full_name||a.username)}${a.is_verified?'<span class="verified">✔</span>':''}</div><div class="un">@${esc(a.username)}${grp}</div>${lp}</div>`
    + extras;
  const fav=r.querySelector(".favbtn:not(.grpbtn)");
  if(fav) fav.onclick=async (e)=>{
    e.stopPropagation();
    const on = fav.textContent!=='⭐';
    fav.textContent = on?'⭐':'☆'; a.favorite=on;
    await api("/api/favorite",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:a.username, on})});
  };
  const gb=r.querySelector(".grpbtn");
  if(gb) gb.onclick=async (e)=>{
    e.stopPropagation();
    const g=prompt("Gruppe für @"+a.username+" (leer lassen = Gruppe entfernen):", a.group||"");
    if(g===null) return;
    const {data}=await api("/api/group",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:a.username, group:g})});
    if(data && data.ok) toast(g.trim()?("@"+a.username+" → Gruppe „"+g.trim()+"“"):"Gruppe entfernt","ok");
  };
  const flb=r.querySelector(".folbtn");
  if(flb) flb.onclick=async (e)=>{
    e.stopPropagation();
    const add=!a.following;
    a.following=add;
    flb.textContent=add?'✓ Gefolgt':'+ Folgen';
    flb.classList.toggle("on", add);
    flb.title=add?'In deiner Liste – tippen zum Entfernen':'Zu deiner Liste hinzufügen';
    const ep=add?"/api/following/add":"/api/following/remove";
    const {data}=await api(ep,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:a.username, full_name:a.full_name||""})});
    if(data && data.error){                      // Fehler → optische Rücknahme
      a.following=!add; flb.textContent=a.following?'✓ Gefolgt':'+ Folgen';
      flb.classList.toggle("on", a.following); toast(data.error,"err"); return;
    }
    // Lokalen Listen-Speicher mitpflegen, damit „Gefolgt (N)" sofort stimmt.
    if(add){ const c=getFollowCache("following");
      if(!c.some(x=>x.username===a.username)){
        c.unshift({username:a.username, full_name:a.full_name||"", avatar:a.avatar||""});
        saveFollowCache("following", c); } }
    else saveFollowCache("following",
      getFollowCache("following").filter(x=>x.username!==a.username));
    toast(add?("@"+a.username+" zu deiner Liste hinzugefügt")
            :("@"+a.username+" aus deiner Liste entfernt"),"ok");
  };
  if(selectable){
    const cbEl=r.querySelector(".acb");
    cbEl.onclick=(e)=>e.stopPropagation();
    cbEl.onchange=()=>{ if(cbEl.checked) followSel.add(a.username); else followSel.delete(a.username); updateAcctBar(); };
    r.onclick=(e)=>{ if(e.target!==cbEl) openProfile(a.username); };
  } else {
    r.onclick=()=>openProfile(a.username);
  }
  return r;
}
let followSel=new Set(), acctTimer=null;
function selectAllAccts(on){
  // WIRKLICH alle Konten der Liste erfassen – nicht nur die gerade
  // angezeigten Zeilen (bei langen Listen werden anfangs nur 200 gerendert).
  if(on && typeof listAccts!=="undefined" && listAccts.length){
    listAccts.forEach(a=>followSel.add(a.username));
  } else if(!on){
    followSel.clear();
  }
  document.querySelectorAll("#content .acctrow").forEach(r=>{
    const cb=r.querySelector(".acb"); if(!cb) return; cb.checked=on;
    if(on) followSel.add(r.dataset.u);
  });
  updateAcctBar();
}
function updateAcctBar(){ const b=$("acct_dl"); if(b) b.textContent="⬇ Herunterladen ("+followSel.size+")"; }
async function downloadFollowSelected(){
  const usernames=[...followSel];
  if(usernames.length===0){ toast("Bitte Konten auswählen.","err"); return; }
  const count=($("acct_count")&&$("acct_count").value)||12;
  const types=($("acct_types")&&$("acct_types").value)||"all";
  const was={all:"Beiträge",photos:"Fotos",videos:"Videos/Reels",stories:"Stories",highlights:"Highlights",everything:"Beiträge + Stories & Highlights",complete:"WIRKLICH ALLES (alle Beiträge + Stories + Highlights)"}[types]||"Beiträge";
  if(!confirm("Von "+usernames.length+" Konten "+(types==="stories"?"die aktuellen Stories":("je "+count+" "+was))+" auf den PC laden?")) return;
  const {data}=await api("/api/download-following",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames, count, types})});
  if(data.error){ toast(data.error,"err"); return; }
  toast("Download gestartet …");
  showDlBar("Download läuft …", 0, usernames.length);
  if(acctTimer) clearInterval(acctTimer);
  acctTimer=setInterval(async ()=>{
    const {data:st}=await api("/api/download-following/status");
    if(!st || st.error){ clearInterval(acctTimer); acctTimer=null; hideDlBar(); return; }
    if($("acct_dl")) $("acct_dl").textContent=st.done+"/"+st.total+" · "+st.downloaded+" geladen";
    showDlBar((st.current?("@"+st.current+" · "):"")+st.done+"/"+st.total+" Konten · "
      +st.downloaded+" Beiträge geladen", st.done, st.total);
    if(!st.running && st.finished){ clearInterval(acctTimer); acctTimer=null;
      hideDlBar(); toast("Fertig: "+st.downloaded+" Beiträge geladen","ok"); updateAcctBar(); }
  },1000);
}
/* ---------- Download-Fortschrittsbalken (oben) ---------- */
function showDlBar(text, done, total){
  const bar=$("dlbar"); if(!bar) return;
  bar.style.display="block";
  $("dlbar_text").textContent=text||"Download …";
  const pct = total>0 ? Math.round(done/total*100) : 0;
  $("dlbar_fill").style.width=pct+"%";
}
function hideDlBar(){ const b=$("dlbar"); if(b) b.style.display="none"; }
async function cancelBulkAll(){
  await api("/api/download-following/cancel",{method:"POST"});
  hideDlBar(); toast("Download abgebrochen.");
}
// Gemeinsamer Poller für den oberen Balken (Konto-Download & Co.).
let dlBarTimer=null, dlBarErrors=0;
function startDlBarPolling(){
  if(dlBarTimer) clearInterval(dlBarTimer);
  dlBarErrors=0;
  dlBarTimer=setInterval(async ()=>{
    const {data:st}=await api("/api/download-following/status");
    // WICHTIG: Ein einzelner Fehl-Abruf heißt NICHT „kein Download" – der
    // Server ist beim Herunterladen oft kurz beschäftigt. Erst nach vielen
    // Fehlern in Folge aufgeben; der Download selbst läuft ja weiter.
    if(!st || st.error || st.running===undefined){
      if(++dlBarErrors >= 8){ clearInterval(dlBarTimer); dlBarTimer=null; }
      return;                       // Balken NICHT verstecken
    }
    dlBarErrors=0;
    const active=(st.items||[]).find(it=>it.state==="active");
    const who = active?("@"+active.name+" · "):(st.current?("@"+st.current+" · "):"");
    showDlBar(who+st.done+"/"+st.total+" Konten · "+st.downloaded+" Beiträge geladen",
              st.done, st.total);
    if(!st.running && st.finished){ clearInterval(dlBarTimer); dlBarTimer=null;
      hideDlBar(); toast("Fertig: "+st.downloaded+" Beiträge geladen","ok"); }
  },1000);
}
// Beim Öffnen der App prüfen, ob im Hintergrund schon ein Download läuft –
// dann den antippbaren Balken sofort wieder anzeigen (er ging z. B. durch
// Neuladen der Seite verloren, obwohl der Download weiterlief).
async function restoreDlBar(){
  const {data:st}=await api("/api/download-following/status");
  if(st && !st.error && st.running){
    showDlBar((st.current?("@"+st.current+" · "):"")+st.done+"/"+st.total
      +" Konten · "+st.downloaded+" Beiträge geladen", st.done, st.total);
    startDlBarPolling();
  }
}
// „⬇ Konto laden": alles von EINEM Konto (Beiträge + Stories + Highlights).
async function downloadAccount(username){
  username=(username||"").trim().replace(/^@/,""); if(!username) return;
  if(!confirm("Alles von @"+username+" laden (Beiträge, Videos, Stories & Highlights)?\n"
    +"Läuft im Hintergrund weiter – Fortschritt oben antippbar.")) return;
  const {data}=await api("/api/download-following",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames:[username], count:50, types:"everything"})});
  if(data.error){ toast(data.error,"err"); return; }
  toast("Download von @"+username+" gestartet …","ok");
  showDlBar("@"+username+" wird geladen …", 0, 1);
  startDlBarPolling();
}
// Nur EIN Medientyp eines Kontos (Fotos bzw. Videos) laden.
async function downloadAccountType(username, types){
  username=(username||"").trim().replace(/^@/,""); if(!username) return;
  const label={photos:"Fotos", videos:"Videos/Reels"}[types]||types;
  if(!confirm("„"+label+"“ von @"+username+" laden?\nLäuft im Hintergrund weiter.")) return;
  const {data}=await api("/api/download-following",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames:[username], count:50, types})});
  if(data.error){ toast(data.error,"err"); return; }
  toast(label+" von @"+username+" – Download gestartet …","ok");
  showDlBar("@"+username+" · "+label+" …", 0, 1);
  startDlBarPolling();
}
// Story bzw. Highlights eines Kontos laden (eigener Endpunkt).
async function downloadStoryKind(username, kind){
  username=(username||"").trim().replace(/^@/,""); if(!username) return;
  const label=kind==="highlights"?"Highlights":"Story";
  if(!confirm(label+" von @"+username+" laden?\nLäuft im Hintergrund weiter.")) return;
  const {data}=await api("/api/download-stories",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username, kind})});
  if(data.error){ toast(data.error,"err"); return; }
  if(data.total===0){ toast("Keine "+label+" vorhanden.","ok"); return; }
  toast(label+" von @"+username+" – Download gestartet …","ok");
  showDlBar("@"+username+" · "+label+" …", 0, 1);
  startDlBarPolling();
}
// Gelbe Zeile im Profil: wie viele Dateien je Typ schon lokal liegen.
async function loadAccountFiles(username){
  const box=$("pf_files"); if(!box) return;
  const {data}=await api("/api/account-files/"+encodeURIComponent(username));
  if(!$("pf_files")) return;
  if(!data || data.error){ box.textContent=""; return; }
  const c=data.counts||{};
  const parts=[];
  if(c.Fotos)      parts.push(c.Fotos+" Fotos");
  if(c.Videos)     parts.push(c.Videos+" Videos");
  if(c.Story)      parts.push(c.Story+" Story");
  if(c.Highlights) parts.push(c.Highlights+" Highlights");
  box.textContent = data.total
    ? ("📥 Geladen: "+parts.join(" · ")+"  ("+fmtBytes(data.bytes)+")")
    : "📥 Von diesem Konto ist noch nichts geladen.";
}
// „⬇⬇ ALLE Konten komplett": geht jedes Abo-Konto durch und lädt WIRKLICH
// alles (alle Beiträge, Videos, Stories, Highlights). Bereits erledigte
// Konten werden übersprungen, damit ein zweiter Lauf nur den Rest holt.
async function downloadAllComplete(){
  if(!confirm("Wirklich ALLES von ALLEN Konten laden?\n\n"
    +"• alle Beiträge, Videos, Stories und Highlights\n"
    +"• bereits komplett geladene Konten werden übersprungen\n"
    +"• das dauert viele Stunden und läuft im Hintergrund weiter\n"
    +"• Fortschritt: oben auf den Balken tippen (dort auch „Abbrechen“)")) return;
  const {data}=await api("/api/download-all-complete",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({})});
  if(data.error){ toast(data.error,"err"); return; }
  if(!data.total){ toast(data.message||"Alle Konten sind bereits komplett geladen.","ok"); return; }
  toast("Komplett-Download gestartet: "+data.total+" Konten"
    +(data.skipped?(" ("+data.skipped+" schon erledigt)"):""),"ok");
  showDlBar("Komplett-Download läuft …", 0, data.total);
  startDlBarPolling();
  showDownloadsLive();
}
/* ---------- „Erledigt": komplett geladene Konten ---------- */
async function showCompleted(){
  cancelSelect(); pushView("completed", showCompleted);
  viewMode="completed"; setNav("");
  if(typeof feedTimer!=="undefined" && feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("subbar").innerHTML=""; $("more").innerHTML="";
  $("pf_head").style.display="block"; $("pf_head").className="";
  $("pf_head").innerHTML='<div style="padding:8px 4px">'
    +'<div class="fn" style="font-weight:700;font-size:17px">✅ Erledigt</div>'
    +'<div class="un" style="color:var(--muted);font-size:13px">Konten, von denen ALLES geladen wurde</div></div>';
  $("content").innerHTML=skeletonRows(5);
  const {data}=await api("/api/accounts-complete");
  if(viewMode!=="completed") return;
  if(!data || data.error){ renderEmpty((data&&data.error)||"Konnte nicht geladen werden."); return; }
  const accts=data.accounts||[];
  if(!accts.length){
    $("content").innerHTML='<div class="empty">Noch kein Konto komplett geladen.<br>'
      +'<span class="hint">Starte „⬇⬇ ALLE Konten komplett“ in der Gefolgt-Liste – '
      +'jedes fertige Konto erscheint dann hier.</span></div>';
    return;
  }
  let html='<div class="hint" style="margin:6px 2px">'+accts.length+' von '
    +(data.following_total||accts.length)+' Konten komplett geladen</div>';
  accts.forEach(a=>{
    html+='<div class="acctrow" data-u="'+esc(a.username)+'" style="cursor:pointer">'
      +'<div class="ph">✅</div><div style="flex:1;min-width:0">'
      +'<div class="fn">@'+esc(a.username)+'</div>'
      +'<div class="un">'+a.files+' Dateien · '+timeAgo(a.at)+'</div></div>'
      +'<button class="favbtn" title="Erledigt-Vermerk entfernen (wird dann neu geladen)">↻</button></div>';
  });
  $("content").innerHTML=html;
  document.querySelectorAll("#content .acctrow").forEach(r=>{
    const u=r.dataset.u;
    r.onclick=(e)=>{ if(!e.target.classList.contains("favbtn")) openProfile(u); };
    const b=r.querySelector(".favbtn");
    if(b) b.onclick=async (e)=>{
      e.stopPropagation();
      await api("/api/accounts-complete/reset",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({username:u})});
      toast("@"+u+" wird beim nächsten Komplett-Lauf erneut geladen.","ok");
      showCompleted();
    };
  });
}
/* ---------- Live-Ansicht: „Was wird gerade geladen?" ---------- */
// Der obere Fortschrittsbalken ist antippbar und öffnet diese Ansicht: sie
// zeigt je Konto, ob es fertig (✅), gerade dran (spinner) oder in der
// Warteschlange (⏳) ist – so sieht man, WAS im Hintergrund geladen wird.
let liveTimer=null;
function showDownloadsLive(){
  cancelSelect(); pushView("dllive", showDownloadsLive);
  viewMode="dllive"; setNav("");
  if(typeof feedTimer!=="undefined" && feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("subbar").innerHTML=""; $("more").innerHTML="";
  $("pf_head").style.display="block"; $("pf_head").className="";
  $("pf_head").innerHTML='<div style="padding:8px 4px">'
    +'<div class="fn" style="font-weight:700;font-size:17px">⬇ Aktuelle Downloads</div>'
    +'<div class="un" style="color:var(--muted);font-size:13px">Was gerade im Hintergrund geladen wird</div></div>';
  $("content").innerHTML=skeletonRows(4);
  let liveErrors=0;
  const tick=async ()=>{
    if(viewMode!=="dllive"){ if(liveTimer){clearInterval(liveTimer);liveTimer=null;} return; }
    const {data:st}=await api("/api/download-following/status");
    if(viewMode!=="dllive"){ if(liveTimer){clearInterval(liveTimer);liveTimer=null;} return; }
    // Kurzer Schluckauf (Server lädt gerade eine große Datei) ist NICHT
    // „nichts geladen": bisherige Anzeige stehen lassen, weiter versuchen.
    if(!st || st.error || st.running===undefined){
      if(++liveErrors >= 8){ $("content").innerHTML=
        '<div class="empty">Keine Antwort vom Server.<br><span class="hint">Die App kurz schließen und neu öffnen.</span></div>'; }
      return;
    }
    liveErrors=0;
    renderDownloadsLive(st);
    if(!st.running && st.finished && liveTimer){ clearInterval(liveTimer); liveTimer=null; }
  };
  tick();
  if(liveTimer) clearInterval(liveTimer);
  liveTimer=setInterval(tick, 1000);
}
function renderDownloadsLive(st){
  const items=st.items||[];
  if(!st.running && !items.length){
    $("content").innerHTML='<div class="empty">Gerade wird nichts geladen.<br>'
      +'<span class="hint">Starte einen Download über ein Profil („⬇ Konto laden") '
      +'oder die Gefolgt-Liste.</span></div>';
    renderResumeBox(); return;
  }
  const done=items.filter(it=>it.state==="done");
  const active=items.find(it=>it.state==="active");
  const pending=items.filter(it=>it.state==="pending");
  let html='<div class="hint" style="margin:6px 2px">'
    +(st.running?('Lädt … '+st.done+'/'+st.total+' Konten · '+st.downloaded+' Dateien geladen')
                :('Fertig · '+st.downloaded+' Dateien geladen'))
    +(st.running?' <button class="btn ghost small" onclick="cancelBulkAll()" style="margin-left:8px">Abbrechen</button>':'')
    +'</div>';
  // ---- Bereich 1: WIRD JETZT BEARBEITET ----
  if(st.running){
    html+='<div style="margin:10px 2px 4px;font-weight:700">🔄 Wird jetzt bearbeitet</div>';
    if(active){
      html+='<div class="acctrow" style="cursor:pointer;border:1px solid var(--accent);border-radius:12px" onclick="openProfile(\''+esc(active.name)+'\')">'
        +'<img loading="lazy" src="/avatar/'+encodeURIComponent(active.name)+'" onerror="this.outerHTML=\'<div class=ph>@</div>\'">'
        +'<div style="flex:1;min-width:0"><div class="fn">@'+esc(active.name)+'</div>'
        +'<div class="un">lädt gerade …'+(active.n?(' '+active.n+' Dateien in diesem Lauf'):'')+'</div>'
        +'<div class="un" id="live_files">📥 prüfe, was schon da ist …</div></div>'
        +'<span class="spin" style="border-top-color:var(--accent)"></span></div>';
      loadLiveFiles(active.name);
    } else {
      const nxt=pending.length?('gleich weiter mit @'+esc(pending[0].name)):'gleich fertig …';
      html+='<div class="hint" style="margin:2px 2px 6px">kurze Pause zwischen den Konten – '+nxt+'</div>';
    }
  }
  // ---- Bereich 2: ALS NÄCHSTES ----
  if(pending.length){
    html+='<div style="margin:12px 2px 4px;font-weight:700">⏭ Als Nächstes ('+pending.length+')</div>';
    pending.slice(0,10).forEach(it=>{
      html+='<div class="acctrow" style="cursor:default"><div class="ph">⏳</div>'
        +'<div style="flex:1;min-width:0"><div class="fn">@'+esc(it.name)+'</div>'
        +'<div class="un">wartet …</div></div></div>';
    });
    if(pending.length>10) html+='<div class="hint" style="margin:2px 2px">… und '+(pending.length-10)+' weitere</div>';
  }
  // ---- Bereich 3: FERTIG in diesem Lauf ----
  if(done.length){
    html+='<div style="margin:12px 2px 4px;font-weight:700">✅ Fertig in diesem Lauf ('+done.length+')</div>';
    done.slice(-5).reverse().forEach(it=>{
      html+='<div class="acctrow" style="cursor:pointer" onclick="openProfile(\''+esc(it.name)+'\')">'
        +'<div class="ph">✅</div><div style="flex:1;min-width:0"><div class="fn">@'+esc(it.name)+'</div>'
        +'<div class="un">'+it.n+' Dateien geladen</div></div></div>';
    });
    if(done.length>5) html+='<div class="hint" style="margin:2px 2px">Alle fertigen Konten: Knopf „✅ Erledigt" in der Gefolgt-Liste</div>';
  }
  $("content").innerHTML=html;
  if(!st.running) renderResumeBox();
}
// Zeile „was ist von diesem Konto schon auf dem Gerät" im Aktiv-Kasten –
// höchstens alle 10 s je Konto neu geholt (der Server zählt echte Dateien).
let _liveFilesUser="", _liveFilesAt=0, _liveFilesText="";
async function loadLiveFiles(username){
  const now=Date.now();
  if(username===_liveFilesUser && now-_liveFilesAt<10000){
    const el=$("live_files"); if(el && _liveFilesText) el.textContent=_liveFilesText;
    return;
  }
  _liveFilesUser=username; _liveFilesAt=now;
  const {data}=await api("/api/account-files/"+encodeURIComponent(username));
  const el=$("live_files"); if(!el) return;
  if(!data || data.error){ el.textContent=""; return; }
  const c=data.counts||{}; const parts=[];
  if(c.Fotos) parts.push(c.Fotos+" Fotos"); if(c.Videos) parts.push(c.Videos+" Videos");
  if(c.Story) parts.push(c.Story+" Story"); if(c.Highlights) parts.push(c.Highlights+" Highlights");
  _liveFilesText = parts.length ? ("📥 Schon auf dem Gerät: "+parts.join(" · ")) : "📥 Noch nichts von diesem Konto auf dem Gerät.";
  el.textContent=_liveFilesText;
}
// „Weitermachen": zeigt einen abgebrochenen Auftrag und bietet an, dort
// fortzusetzen, wo er aufgehört hat (statt bei 600 Konten von vorn).
async function renderResumeBox(){
  const {data}=await api("/api/download-following/resume-info");
  const c=$("content"); if(!c || !data || !data.available) return;
  const box=document.createElement("div");
  box.style.cssText="margin:12px 4px;padding:12px;border:1px solid var(--border);border-radius:12px";
  box.innerHTML='<div style="font-weight:700;margin-bottom:4px">⏸ Unterbrochener Download</div>'
    +'<div class="hint" style="margin-bottom:8px">'+data.done+' von '+data.total
    +' Konten erledigt · <b>'+data.remaining+' offen</b></div>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap">'
    +'<button class="btn" id="rs_go">▶ Weitermachen (ab Konto '+(data.done+1)+')</button>'
    +'<button class="btn ghost" id="rs_new">↺ Von vorne</button></div>';
  c.appendChild(box);
  $("rs_go").onclick=async ()=>{
    const {data:r}=await api("/api/download-following/resume",{method:"POST"});
    if(r.error){ toast(r.error,"err"); return; }
    toast("Weiter ab Konto "+(data.done+1)+" – "+r.total+" offen","ok");
    showDlBar("Weitermachen …", 0, r.total); startDlBarPolling(); showDownloadsLive();
  };
  $("rs_new").onclick=()=>{
    toast("Für „von vorne\" die Konten in der Gefolgt-Liste erneut auswählen und ⬇ starten.");
    showFollowing();
  };
}

/* ---------- Download-Verlauf ---------- */
// „🔎 Neue prüfen“: geht die angezeigten Konten nacheinander durch (nur auf
// Knopfdruck!) und aktualisiert je Zeile „zuletzt gepostet“ + NEU-Punkt.
let chkTimer=null;
async function checkNewPosts(){
  const names=[...document.querySelectorAll("#content .acctrow")]
    .map(r=>r.dataset.u).filter(Boolean);
  if(!names.length){ toast("Keine Konten in der Liste.","err"); return; }
  const {data}=await api("/api/check-new",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames:names})});
  if(data.error){ toast(data.error,"err"); return; }
  toast("Prüfe "+data.total+" Konten – die Zeiten erscheinen nach und nach …");
  if(chkTimer) clearInterval(chkTimer);
  chkTimer=setInterval(async ()=>{
    const {data:st}=await api("/api/check-new/status");
    if(!st || st.error){ clearInterval(chkTimer); chkTimer=null;
      const cb=$("chk_btn"); if(cb) cb.textContent="🔎 Neue prüfen"; return; }
    const b=$("chk_btn");
    if(b) b.textContent = st.running ? ("🔎 "+st.done+"/"+st.total+" …") : "🔎 Neue prüfen";
    Object.entries(st.results||{}).forEach(([u,info])=>{
      const row=document.querySelector('#content .acctrow[data-u="'+(window.CSS&&CSS.escape?CSS.escape(u):u)+'"]');
      if(!row) return;
      let lpEl=row.querySelector(".lastp");
      if(!lpEl){ const mid=row.querySelector(".fn") && row.querySelector(".fn").parentElement;
        if(!mid) return;
        lpEl=document.createElement("div"); lpEl.className="un lastp"; mid.appendChild(lpEl); }
      lpEl.innerHTML=(info.new?'<b style="color:var(--ok)">● NEU</b> · ':'')
        + (info.error ? esc(info.error)
           : (info.last ? ('🕐 zuletzt gepostet: '+timeAgo(info.last)) : '🕐 keine Beiträge gefunden'));
    });
    if(!st.running && st.finished){
      clearInterval(chkTimer); chkTimer=null;
      const n=Object.values(st.results||{}).filter(i=>i.new).length;
      toast(n ? ("● "+n+" Konten haben etwas Neues gepostet!") : "Keine neuen Beiträge seit der letzten Prüfung.","ok");
    }
  },1000);
}
async function showDownloads(){
  cancelSelect(); pushView("downloads", showDownloads); $("subbar").innerHTML="";
  viewMode="downloads"; setNav("nav_profile"); $("manage_panel").style.display="none";
  if(feedTimer){clearInterval(feedTimer); feedTimer=null;}
  $("stories_row").style.display="none"; $("filterbar").style.display="none"; $("more").innerHTML="";
  $("pf_head").style.display="flex"; $("pf_head").className="pf-head";
  $("pf_head").innerHTML='<div class="ph">📥</div><div><div class="fn">Download-Verlauf</div><div class="un">lädt …</div></div>';
  $("content").className=""; $("content").innerHTML='<div class="empty"><span class="spin"></span></div>';
  const {data}=await api("/api/downloads");
  if(viewMode!=="downloads") return;
  if(data.error){ renderEmpty(data.error); return; }
  const items=data.items||[];
  const un=$("pf_head").querySelector(".un"); if(un) un.textContent=items.length+" Downloads";
  if(items.length===0){ renderEmpty("Noch nichts heruntergeladen."); return; }
  $("content").innerHTML="";
  items.forEach(it=>{
    const r=document.createElement("div"); r.className="acctrow";
    const icon=/(Video|Reel)/.test(it.type)?'▶':(/Album/.test(it.type)?'▦':'🖼');
    r.innerHTML='<div class="ph">'+icon+'</div><div style="flex:1;min-width:0">'
      +'<div class="fn">@'+esc(it.username)+'</div>'
      +'<div class="un">'+esc(it.type||'')+' · '+timeAgo(it.downloaded_at)+'</div></div>';
    r.onclick=()=>window.open(it.url,"_blank","noopener");
    $("content").appendChild(r);
  });
}

/* ---------- Follower / Gefolgt ---------- */
// Beide Listen kommen aus /api/overview – d. h. aus der LOKALEN Datenbank
// des Servers: beim Öffnen sofort da, ohne einen einzigen Instagram-Abruf.
// Aufgefrischt wird im Hintergrund (automatisch stündlich bzw. per Knopf).
function showFollowing(){ showAccountList("following","Gefolgt", true); }
function showFollowers(){ showAccountList("followers","Follower", true); }
let listTimer=null, listEtag="", listKind="";
function stopListTimer(){ if(listTimer){ clearInterval(listTimer); listTimer=null; } }
async function showAccountList(kind,title,selectable){
  cancelSelect(); pushView("list:"+title, ()=>showAccountList(kind,title,selectable));
  followSel.clear(); listShown=200;
  viewMode="list"; listKind=kind; setNav("nav_profile");
  if(feedTimer){clearInterval(feedTimer); feedTimer=null;}
  stopListTimer();
  $("stories_row").style.display="none"; $("filterbar").style.display="none"; $("more").innerHTML="";
  $("pf_head").style.display="none";
  // Umschalter Gefolgt/Follower + Auswahl-/Download-Aktionen
  let sub='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip '+(title==='Gefolgt'?'on':'')+'" onclick="showFollowing()">Gefolgt</button>'
    +'<button class="chip '+(title==='Follower'?'on':'')+'" onclick="showFollowers()">Follower</button>'
    +'<button class="chip" title="Wer folgt dir nicht zurück / wem folgst du nicht zurück" onclick="showFollowCompare()">🔁 Rückfolge</button>'
    +'<button class="chip" title="Neu gefolgt / entfolgt im Zeitverlauf" onclick="showListHistory()">🕘 Verlauf</button>';
  if(selectable){
    sub+='<button class="chip" onclick="selectAllAccts(true)">Alle</button>'
      +'<button class="chip" onclick="selectAllAccts(false)">Keine</button>'
      +'<span class="chip" style="cursor:default">je Konto: <select id="acct_count" style="background:transparent;border:0;color:inherit"><option>3</option><option selected>12</option><option>24</option><option value="30">alle (max 30)</option></select></span>'
      +'<span class="chip" style="cursor:default" title="Was soll heruntergeladen werden?">Was: <select id="acct_types" style="background:transparent;border:0;color:inherit">'
      +'<option value="all">Alles</option>'
      +'<option value="photos">nur Fotos</option>'
      +'<option value="videos">nur Videos+Reels</option>'
      +'<option value="stories">nur Stories</option>'
      +'<option value="highlights">nur Highlights</option>'
      +'<option value="everything">Alles + Stories &amp; Highlights</option>'
      +'<option value="complete">KOMPLETT (wirklich alles)</option>'
      +'</select></span>'
      +'<button class="chip" id="acct_dl" onclick="downloadFollowSelected()">⬇ Herunterladen (0)</button>'
      +'<button class="chip" id="all_new" title="Alle Konten dieser Liste: nur fehlende Beiträge laden" onclick="downloadAllNew()">⬇ Alles Neue</button>'
      +'<button class="chip" id="all_complete" title="Jedes Konto WIRKLICH komplett laden: alle Beiträge, Videos, Stories und Highlights" onclick="downloadAllComplete()">⬇⬇ ALLE Konten komplett</button>'
      +'<button class="chip" onclick="showCompleted()" title="Welche Konten sind schon komplett geladen?">✅ Erledigt</button>'
      +'<button class="chip" id="chk_btn" onclick="checkNewPosts()">🔎 Neue prüfen</button>'
      +'<button class="chip" title="Als CSV-Datei (Excel) speichern" onclick="exportListCsv()">⬇ CSV</button>';
  }
  sub+='</div>'
    // Zweite Reihe: Sofort-Filter (rein lokal) + Sortierung.
    +'<div class="filterbar" style="padding:0 2px 8px">'
    +'<input id="list_filter" placeholder="🔍 in dieser Liste filtern …" autocomplete="off" '
    +'oninput="applyListViewDebounced()" style="flex:1;min-width:140px;padding:7px 12px;border-radius:10px">'
    +'<span class="chip" style="cursor:default">Sortieren: '
    +'<select id="list_sort" onchange="applyListView()" style="background:transparent;border:0;color:inherit">'
    +'<option value="recent">letzter Beitrag</option>'
    +'<option value="new">NEU zuerst</option>'
    +'<option value="name">Name (A–Z)</option>'
    +'<option value="user">@Benutzername</option>'
    +'<option value="original">Instagram-Reihenfolge</option>'
    +'</select></span></div>';
  $("subbar").innerHTML=sub;
  // Skeleton statt Kreisel: die Seite wirkt sofort fertig.
  $("content").className=""; $("content").innerHTML=skeletonRows(8);
  const {data}=await api("/api/overview");
  if(viewMode!=="list"||listKind!==kind) return;
  if(data.error){ renderEmpty(data.error); toast(data.error,"err"); return; }
  renderAccountList(data, kind, title, selectable);
  // Erste Befüllung: Liste wurde noch nie geholt → einmalig anstoßen.
  const block=data[kind]||{};
  if(!(block.accounts||[]).length && !block.fetched_at && data.logged_in
     && !(data.refresh&&data.refresh.running)){
    refreshSocial(true);
  }
  startListPolling(kind, title, selectable);
}
let listAccts=[], listMeta={}, listShown=200;
function renderAccountList(data, kind, title, selectable){
  listEtag=data.version||"";
  const block=data[kind]||{};
  const accts=(block.accounts||[]).slice();
  saveFollowCache(kind, accts);
  accts.forEach(a=>{ a._lp=a.last_post_at||""; a._new=!!a.is_new; });
  listAccts=accts;
  listMeta={kind, title, selectable, refresh:data.refresh||{},
            fetched_at:block.fetched_at||"", logged_in:!!data.logged_in};
  applyListView();
}
// Filtert + sortiert die geladene Liste rein lokal und rendert sie. Wird
// bei jedem Tippen im Filter und bei jeder Sortier-Änderung aufgerufen –
// ohne erneuten Server-Abruf.
function applyListView(){
  const c=$("content"); if(!c) return;
  const {title, selectable, refresh:st, fetched_at, logged_in}=listMeta;
  const q=($("list_filter")&&$("list_filter").value||"").trim().toLowerCase();
  const sortMode=($("list_sort")&&$("list_sort").value)||"recent";
  let rows=listAccts.slice();
  if(q) rows=rows.filter(a=>((a.username||"")+" "+(a.full_name||"")+" "+(a.group||"")).toLowerCase().includes(q));
  const cmp={
    recent:(x,y)=>(y._lp||"").localeCompare(x._lp||""),
    new:(x,y)=>(y._new?1:0)-(x._new?1:0) || (y._lp||"").localeCompare(x._lp||""),
    name:(x,y)=>(x.full_name||x.username).localeCompare(y.full_name||y.username,"de"),
    user:(x,y)=>(x.username||"").localeCompare(y.username||""),
    original:(x,y)=>0,
  }[sortMode]||((x,y)=>0);
  // Favoriten (⭐) immer zuerst, dann nach gewählter Sortierung.
  rows.sort((x,y)=>((y.favorite?1:0)-(x.favorite?1:0)) || cmp(x,y));

  c.innerHTML="";
  const info=document.createElement("div"); info.className="hint"; info.style.margin="4px";
  const stand=fetched_at ? ("Stand: "+timeAgo(fetched_at)) : "noch nie von Instagram geholt";
  const zaehler = q ? (rows.length+" von "+listAccts.length+" Konten")
                    : (listAccts.length+" Konten");
  info.innerHTML=esc(title)+": "+zaehler+" · "+esc(stand)+" · "
    +(st.running
      ? '<span class="spin" style="width:12px;height:12px;border-width:2px;display:inline-block;vertical-align:middle"></span> wird gerade aktualisiert …'
      : '<a style="cursor:pointer;text-decoration:underline" onclick="refreshSocial()">🔄 Jetzt aktualisieren</a>');
  c.appendChild(info);
  if(st.last_error && !st.running){
    const w=document.createElement("div"); w.className="hint"; w.style.margin="4px";
    w.textContent="⚠ Letzte Aktualisierung: "+st.last_error; c.appendChild(w);
  }
  if(listAccts.length===0){
    const e=document.createElement("div"); e.className="empty";
    e.textContent = logged_in
      ? "Die Liste wird gerade zum ersten Mal geladen – sie erscheint hier automatisch."
      : "Bitte zuerst anmelden – danach sind Gefolgt & Follower hier sofort da.";
    c.appendChild(e); return;
  }
  if(rows.length===0){
    const e=document.createElement("div"); e.className="empty";
    e.textContent="Kein Treffer für „"+q+"“."; c.appendChild(e); return;
  }
  // Handy: zweispaltiges Kachel-Raster (via CSS-Klasse).
  c.className = "acctgrid";
  // Fenster-Rendering: bei sehr großen Listen (z. B. tausende Follower)
  // zunächst nur die ersten N Zeilen bauen – „Mehr anzeigen“ lädt weiter.
  // Das hält das Öffnen auch bei riesigen Listen sofort flüssig.
  const shown = Math.min(rows.length, listShown);
  for(let i=0;i<shown;i++){
    const a=rows[i];
    const row=accountRow(a, selectable);
    if(selectable && followSel.has(a.username)){
      const cb=row.querySelector(".acb"); if(cb) cb.checked=true;
    }
    c.appendChild(row);
  }
  if(rows.length>shown){
    const more=document.createElement("button");
    more.className="btn ghost"; more.style.margin="10px auto"; more.style.display="block";
    more.textContent="Mehr anzeigen ("+(rows.length-shown)+" weitere)";
    more.onclick=()=>{ listShown+=200; applyListView(); };
    c.appendChild(more);
  }
  updateAcctBar();
  maybeNotifyNew(listAccts);
}
// „Rückfolge“: Vergleicht Gefolgt- und Follower-Liste (beide aus dem
// lokalen Cache) und zeigt, wer nicht zurückfolgt bzw. wem du nicht folgst.
async function showFollowCompare(){
  cancelSelect(); pushView("compare", showFollowCompare);
  viewMode="list"; listKind="compare"; setNav("nav_profile"); stopListTimer();
  $("stories_row").style.display="none"; $("pf_head").style.display="none"; $("more").innerHTML="";
  $("subbar").innerHTML='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip" onclick="showFollowing()">← zurück zur Liste</button></div>';
  $("content").className=""; $("content").innerHTML=skeletonRows(6);
  const {data}=await api("/api/overview");
  if(viewMode!=="list"||listKind!=="compare") return;
  if(data.error){ renderEmpty(data.error); return; }
  const following=(data.following.accounts||[]), followers=(data.followers.accounts||[]);
  if(!following.length && !followers.length){ renderEmpty("Noch keine Listen geladen – bitte zuerst „Gefolgt“ öffnen."); return; }
  const setFwr=new Set(followers.map(a=>a.username));
  const setFwg=new Set(following.map(a=>a.username));
  const notBack=following.filter(a=>!setFwr.has(a.username));    // folgt dir nicht zurück
  const notFollowing=followers.filter(a=>!setFwg.has(a.username)); // du folgst nicht zurück
  const c=$("content"); c.innerHTML="";
  const section=(titel, arr, hinweis)=>{
    const h=document.createElement("div"); h.className="hint"; h.style.margin="10px 4px 4px";
    h.innerHTML="<b>"+esc(titel)+" ("+arr.length+")</b> · "+esc(hinweis); c.appendChild(h);
    if(!arr.length){ const e=document.createElement("div"); e.className="hint"; e.style.margin="4px";
      e.textContent="— niemand —"; c.appendChild(e); return; }
    arr.forEach(a=>c.appendChild(accountRow(a,false)));
  };
  section("Folgt dir nicht zurück", notBack, "du folgst – zurück kommt nichts");
  section("Du folgst nicht zurück", notFollowing, "folgen dir – du ihnen (noch) nicht");
}
// „Verlauf“: Wer ist seit einer früheren Auffrischung neu dazugekommen
// oder entfolgt (aus den gespeicherten Snapshots der Listen).
async function showListHistory(){
  cancelSelect(); pushView("listhistory", showListHistory);
  viewMode="list"; listKind="history"; setNav("nav_profile"); stopListTimer();
  $("stories_row").style.display="none"; $("pf_head").style.display="none"; $("more").innerHTML="";
  $("subbar").innerHTML='<div class="filterbar" style="padding:8px 2px">'
    +'<button class="chip" onclick="showFollowing()">← zurück zur Liste</button></div>';
  $("content").className=""; $("content").innerHTML=skeletonRows(6);
  const {data}=await api("/api/social/events");
  if(viewMode!=="list"||listKind!=="history") return;
  if(data.error){ renderEmpty(data.error); return; }
  const ev=data.events||[];
  const c=$("content"); c.innerHTML="";
  const info=document.createElement("div"); info.className="hint"; info.style.margin="4px";
  info.textContent="Änderungen an deinen Listen (aus den automatischen Auffrischungen):";
  c.appendChild(info);
  if(!ev.length){ const e=document.createElement("div"); e.className="empty";
    e.textContent="Noch kein Verlauf – der entsteht ab der zweiten Auffrischung."; c.appendChild(e); return; }
  ev.forEach(e=>{
    const r=document.createElement("div"); r.className="acctrow";
    const neu=e.event==="added";
    const wo=e.kind==="following"?"Gefolgt":"Follower";
    r.innerHTML='<div class="ph">'+(neu?'➕':'➖')+'</div><div style="flex:1;min-width:0">'
      +'<div class="fn">@'+esc(e.username)+'</div>'
      +'<div class="un"><b style="color:'+(neu?'var(--ok)':'#e0666f')+'">'
      +(neu?'neu in ':'weg aus ')+wo+'</b> · '+timeAgo(e.happened_at)+'</div></div>';
    r.onclick=()=>openProfile(e.username);
    c.appendChild(r);
  });
}
// CSV-Export der aktuell geladenen Liste (Excel-freundlich, mit BOM).
function exportListCsv(){
  if(!listAccts.length){ toast("Nichts zu exportieren.","err"); return; }
  const head=["username","full_name","gruppe","favorit","letzter_beitrag","neu","hat_story"];
  const line=a=>[a.username, a.full_name||"", a.group||"", a.favorite?"ja":"",
    a.last_post_at||"", a._new?"ja":"", a.has_story?"ja":""]
    .map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(";");
  const csv="﻿"+head.join(";")+"\n"+listAccts.map(line).join("\n");
  const blob=new Blob([csv],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url; a.download=(listMeta.kind||"liste")+".csv"; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast("CSV gespeichert ("+listAccts.length+" Konten)","ok");
}
async function refreshSocial(silent){
  const {status,data}=await api("/api/social/refresh",{method:"POST"});
  if(data&&data.error&&status!==409){ toast(data.error,"err"); return; }
  if(!silent) toast("Listen werden im Hintergrund neu von Instagram geholt …");
}
// Leichtes Polling, solange die Liste offen ist: fragt nur die lokale
// Datenbank-Version ab (?since=…) und rendert NUR bei Änderungen neu.
function startListPolling(kind, title, selectable){
  stopListTimer();
  listTimer=setInterval(async ()=>{
    if(viewMode!=="list"||listKind!==kind){ stopListTimer(); return; }
    const {data}=await api("/api/overview?since="+encodeURIComponent(listEtag));
    if(viewMode!=="list"||listKind!==kind){ stopListTimer(); return; }
    if(!data||data.error||data.unchanged) return;
    renderAccountList(data, kind, title, selectable);
  }, 5000);
}
let gridPosts=[], selectMode=false, selectedGrid=new Set(), gridEl=null;
// „Nur nicht geladene": blendet im Raster alles aus, was schon gespeichert ist.
let gridOnlyNew=false, gridAllPosts=[];
function toggleOnlyNew(){
  gridOnlyNew=!gridOnlyNew;
  window._gridToggleRender=true;
  try{
    renderGrid(gridOnlyNew ? gridAllPosts.filter(p=>!p.downloaded) : gridAllPosts);
  } finally { window._gridToggleRender=false; }
}
function renderGrid(posts, container){
  const isMain=!container;
  const c=container||$("content"); if(isMain) c.className="";
  if(isMain && !window._gridToggleRender){ gridAllPosts=posts; gridOnlyNew=false; }
  if(isMain){ gridPosts=posts; selectMode=false; selectedGrid.clear(); updateSelbar(); }
  const g=document.createElement("div"); g.className="grid";
  for(const p of posts){
    const cell=document.createElement("div"); cell.className="cell"; cell.dataset.sc=p.shortcode;
    cell.innerHTML = (p.thumb?`<img loading="lazy" src="${p.thumb}">`:"")
      + (p.is_video?`<span class="ic">▶</span>`:(p.is_album?`<span class="ic">▦</span>`:""))
      + `<span class="dlst ${p.downloaded?'g':'r'}" title="${p.downloaded?'heruntergeladen':'nicht heruntergeladen'}">${p.downloaded?'✓':''}</span>`;
    cell.onclick=()=>{ if(isMain && selectMode) toggleCell(cell,p); else openLightbox(p); };
    g.appendChild(cell);
  }
  c.innerHTML="";
  if(isMain){
    gridEl=g;
    const tools=document.createElement("div"); tools.className="gridtools";
    const nNew=posts.filter(p=>!p.downloaded).length;
    tools.innerHTML='<button class="btn ghost small" onclick="enterSelect()">☑ Auswählen</button>'
      +'<button class="btn ghost small" id="onlynew_btn" onclick="toggleOnlyNew()">'
      +(gridOnlyNew?'Alle anzeigen':'🔴 Nur nicht geladene ('+nNew+')')+'</button>';
    c.appendChild(tools);
  }
  c.appendChild(g);
}
function enterSelect(){ if(!gridEl) return; selectMode=true; selectedGrid.clear();
  gridEl.classList.add("selmode"); updateSelbar(); }
function cancelSelect(){ selectMode=false; selectedGrid.clear();
  if(gridEl){ gridEl.classList.remove("selmode");
    gridEl.querySelectorAll(".cell.sel").forEach(x=>x.classList.remove("sel")); }
  updateSelbar(); }
function toggleCell(cell,p){
  if(selectedGrid.has(p.shortcode)){ selectedGrid.delete(p.shortcode); cell.classList.remove("sel"); }
  else { selectedGrid.add(p.shortcode); cell.classList.add("sel"); }
  updateSelbar();
}
function updateSelbar(){
  const bar=$("selbar");
  if(!selectMode){ bar.classList.remove("show"); return; }
  bar.classList.add("show"); $("selcount").textContent=selectedGrid.size+" ausgewählt";
  let a='<button class="btn" onclick="downloadSelected()">⬇ Auf PC</button>';
  if(viewMode==="saved"){
    a+='<button class="btn ghost" onclick="addSelectedToCollection()">🔖 Sammlung</button>';
    if(currentColl!=="__all") a+='<button class="btn ghost" onclick="removeSelectedFromCollection()">Aus Sammlung</button>';
  }
  a+='<button class="btn ghost" onclick="cancelSelect()">Abbrechen</button>';
  $("selactions").innerHTML=a;
}
async function downloadSelected(){
  const scs=[...selectedGrid];
  if(scs.length===0){ toast("Nichts ausgewählt.","err"); return; }
  toast("Lade "+scs.length+" auf den PC …");
  let ok=0;
  await runPool(scs, async (sc)=>{
    const {data}=await api("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({shortcode:sc})});
    if(data && data.ok) ok++;
  }, 3);
  toast("Fertig: "+ok+"/"+scs.length+" gespeichert","ok");
  cancelSelect();
}

/* ---------- Abo-Liste / Verwalten ---------- */
let followList=[], followAvatar={};
function toggleManage(){ const m=$("manage_panel"); m.style.display = m.style.display==="none"?"block":"none";
  if(m.style.display==="block"){ m.scrollIntoView({behavior:"smooth"}); loadSchedule(); renderSaveTools(); } }

// „Direkt speichern": Laptop-Lesezeichen (Bookmarklet) + Hinweis zum
// Handy-Teilen-Ziel. Das Lesezeichen zeigt auf DIESEN Server (location.origin)
// und schickt den aktuellen Instagram-Beitrag an /save.
function bookmarkletCode(){
  return "javascript:(function(){var u=location.href;"
    + "if(!/instagram\\.com\\/(p|reel|reels|tv)\\//.test(u)){"
    + "alert('Bitte auf einer Instagram-Beitrags- oder Reel-Seite klicken.');return;}"
    + "window.open('" + location.origin
    + "/save?u='+encodeURIComponent(u),'igmsave','width=460,height=300');})();";
}
function renderSaveTools(){
  const body=$("save_tools_body"); if(!body) return;
  body.innerHTML="";
  const a=document.createElement("a");
  a.textContent="⤓ Auf Instagram speichern";
  a.className="btn";
  a.style.cssText="display:inline-block;text-decoration:none";
  a.setAttribute("href", bookmarkletCode());
  // Direktklick im eigenen Fenster ergibt keinen Sinn – nur ziehen.
  a.onclick=(e)=>{ e.preventDefault();
    toast("Diesen Knopf in die Lesezeichenleiste ZIEHEN, dann auf einer Instagram-Beitragsseite anklicken.","ok"); };
  body.appendChild(a);
  const tip=document.createElement("div");
  tip.className="hint"; tip.style.marginTop="8px";
  tip.innerHTML="<b>Laptop:</b> den Knopf oben in die <b>Lesezeichenleiste ziehen</b> "
    + "(mit Strg+Shift+B einblenden). Dann auf instagram.com bei einem Beitrag/Reel "
    + "anklicken → landet sofort im passenden <b>Fotos</b>-/<b>Videos</b>-Ordner.<br><br>"
    + "<b>Handy/Tablet:</b> in der Instagram-App auf <b>Teilen → Instagram Monitor</b> "
    + "(sobald diese Seite als App installiert ist).";
  body.appendChild(tip);
}

async function loadFollowing(silent){
  if(!loggedIn){ if(!silent) toast("Bitte zuerst anmelden.","err"); return; }
  const b=$("fl_btn"); if(!silent){ b.disabled=true; b.innerHTML='<span class="spin"></span> …'; }
  // Knopfdruck (nicht still) = bewusst frisch von Instagram holen, damit
  // neu gefolgte Konten sofort auftauchen. Hintergrund-Aufruf nutzt Cache.
  const {data}=await api("/api/following"+(silent?"":"?fresh=1"));
  if(!silent){ b.disabled=false; b.textContent="Abo-Liste laden"; }
  if(data.error){ if(!silent) toast(data.error,"err"); return; }
  followList=data.accounts||[]; followAvatar={};
  saveFollowCache("following", followList);
  const dl=$("follwd"); dl.innerHTML="";
  for(const a of followList){ if(a.avatar) followAvatar[a.username]=a.avatar;
    const o=document.createElement("option"); o.value=a.username; dl.appendChild(o); }
  renderStoriesRow();
  const list=$("fl_list"); list.innerHTML="";
  for(const a of followList){
    const r=document.createElement("label"); r.className="rowflex"; r.style.padding="6px 4px";
    r.dataset.u=a.username; r.dataset.s=((a.full_name||"")+" "+a.username).toLowerCase();
    r.innerHTML=`<input type="checkbox" checked style="width:16px;height:16px">
      ${a.avatar?`<img src="${a.avatar}" style="width:32px;height:32px;border-radius:50%">`:`<div class="ph" style="width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--bg2)">@</div>`}
      <span style="flex:1"><b>${esc(a.full_name||a.username)}</b> <span class="hint" style="margin:0">@${esc(a.username)}</span></span>`;
    list.appendChild(r);
  }
}
function selectedFollow(){ return [...document.querySelectorAll("#fl_list label")]
  .filter(r=>r.querySelector("input").checked && r.style.display!=="none").map(r=>r.dataset.u); }
function selectAllFollow(on){ document.querySelectorAll("#fl_list label").forEach(r=>{
  if(r.style.display!=="none") r.querySelector("input").checked=on; }); }
function filterFollow(){ const q=$("fl_filter").value.toLowerCase();
  document.querySelectorAll("#fl_list label").forEach(r=>{ r.style.display=(!q||r.dataset.s.includes(q))?"flex":"none"; }); }

let bulkTimer=null;
async function startBulk(){
  const usernames=selectedFollow();
  if(usernames.length===0){ toast("Bitte Konten auswählen.","err"); return; }
  const since=$("bulk_since").value;
  if(!confirm("Beiträge von "+usernames.length+" Konten"+(since?(" ab "+since):"")+" auf den PC laden?")) return;
  const {data}=await api("/api/download-following",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames, count:$("bulk_count").value, since})});
  if(data.error){ toast(data.error,"err"); return; }
  $("bulk_btn").disabled=true; $("bulk_cancel").style.display="inline-block";
  if(bulkTimer) clearInterval(bulkTimer);
  bulkTimer=setInterval(pollBulk,1000); pollBulk();
}
async function pollBulk(){
  const {data:st}=await api("/api/download-following/status");
  if(!st || st.error){ if(bulkTimer){ clearInterval(bulkTimer); bulkTimer=null; }
    $("bulk_btn").disabled=false; $("bulk_cancel").style.display="none"; return; }
  $("bulk_text").textContent = `${st.done}/${st.total} Konten · ${st.downloaded} geladen`+(st.current&&st.running?` · @${st.current}`:"");
  if(!st.running && st.finished){ clearInterval(bulkTimer); bulkTimer=null;
    $("bulk_btn").disabled=false; $("bulk_cancel").style.display="none";
    toast("Fertig: "+st.downloaded+" Beiträge geladen","ok"); }
}
async function cancelBulk(){ await api("/api/download-following/cancel",{method:"POST"}); }

/* ---------- Automatischer Zeitplan ---------- */
function addSchedTime(val){
  const wrap=$("sch_times");
  const box=document.createElement("span"); box.className="rowflex"; box.style.gap="2px";
  box.innerHTML=`<input type="time" value="${val||'12:00'}" style="padding:6px 8px">`
    +`<button class="btn ghost small" title="entfernen" onclick="this.parentNode.remove()">✕</button>`;
  wrap.appendChild(box);
}
async function loadSchedule(){
  const {data}=await api("/api/schedule");
  if(data && data.enabled!==undefined){
    $("sch_enabled").checked=!!data.enabled;
    $("sch_stories").checked=!!data.stories;
    $("sch_per").value=String(data.per||3);
    $("sch_times").innerHTML="";
    (data.times&&data.times.length?data.times:["12:00","00:00"]).forEach(addSchedTime);
    $("sch_info").textContent = data.last_run ? ("Zuletzt automatisch: "+data.last_run) : "";
  }
  $("opt_autoplay").checked=autoplayOn();
  $("opt_bg").checked=bgEnabled();
  const st=await api("/api/settings");
  if(st.data){ $("opt_captions").checked=!!st.data.save_captions;
    $("opt_social").checked = st.data.social_auto_refresh!==false;
    if(st.data.social_refresh_minutes) $("opt_social_min").value=String(st.data.social_refresh_minutes);
    $("opt_quiet").checked = !!st.data.quiet_enabled;
    if(st.data.quiet_from) $("opt_quiet_from").value=st.data.quiet_from;
    if(st.data.quiet_until) $("opt_quiet_until").value=st.data.quiet_until;
    if($("opt_notify")) $("opt_notify").checked = notifyEnabled()
      && ("Notification" in window) && Notification.permission==="granted";
    $("dl_dir").textContent = "Ordner: "+(st.data.download_dir||"");
    if($("set_dldir") && !$("set_dldir").value) $("set_dldir").value=st.data.download_dir||""; }
}
async function saveSchedule(){
  const times=[...document.querySelectorAll("#sch_times input[type=time]")].map(i=>i.value).filter(Boolean);
  const {data}=await api("/api/schedule",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({enabled:$("sch_enabled").checked, stories:$("sch_stories").checked, times, per:$("sch_per").value})});
  if(data.ok){
    $("sch_times").innerHTML=""; (data.times||[]).forEach(addSchedTime);
    toast(data.enabled ? ("Zeitplan aktiv: "+(data.times||[]).join(", ")) : "Zeitplan gespeichert (aus)","ok");
  }else toast(data.error||"Speichern fehlgeschlagen","err");
}
async function saveDownloadDir(){
  const v=$("set_dldir").value.trim();
  if(!v){ toast("Bitte einen Ordner-Pfad eingeben.","err"); return; }
  const {data}=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({download_dir:v})});
  if(data.ok){ toast("Download-Ordner geändert","ok");
    $("dl_dir").textContent="Ordner: "+(data.download_dir||v);
    $("set_dldir").value=data.download_dir||v; }
  else toast(data.error||"Ordner nicht nutzbar","err");
}
async function saveAppSettings(){
  const {data}=await api("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({save_captions:$("opt_captions").checked,
      social_auto_refresh:$("opt_social").checked,
      social_refresh_minutes:+($("opt_social_min").value||60),
      quiet_enabled:$("opt_quiet").checked,
      quiet_from:$("opt_quiet_from").value||"23:00",
      quiet_until:$("opt_quiet_until").value||"07:00"})});
  if(data.ok) toast("Einstellungen gespeichert","ok");
}
function toggleAutoplay(){
  try{ localStorage.setItem("igm_autoplay", $("opt_autoplay").checked?"1":"0"); }catch(e){}
  toast($("opt_autoplay").checked?"Video-Autoplay an":"Datensparmodus (kein Autoplay)","ok");
  if(viewMode==="feed") renderFeed(feedPosts);
}
// Gefolgt-/Follower-Liste aus einer hochgeladenen Datei übernehmen.
// kind "zip": Instagrams komplettes Export-ZIP → beide Listen auf einmal.
async function importList(kind){
  const inp=$("imp_file");
  if(!inp || !inp.files || !inp.files.length){ toast("Bitte zuerst eine Datei auswählen.","err"); return; }
  const info=$("imp_info");
  if(info) info.textContent="Datei wird gelesen …";
  const fd=new FormData();
  fd.append("file", inp.files[0]);
  if(kind!=="zip") fd.append("kind", kind);
  // Kein Content-Type setzen – der Browser ergänzt die nötige Grenze selbst.
  let data={};
  try{
    const res=await fetch("/api/import-list",{method:"POST",body:fd});
    try{ data=await res.json(); }catch(e){}
  }catch(e){ data={error:"Upload fehlgeschlagen – läuft der Server noch?"}; }
  if(data.error){ if(info) info.textContent=""; toast(data.error,"err"); return; }
  inp.value="";
  if(data.zip){
    if(info) info.textContent="✓ Komplett übernommen: "+data.following+" Gefolgt · "+data.followers+" Follower.";
    toast("Import: "+data.following+" Gefolgt · "+data.followers+" Follower","ok");
    showFollowing();
    return;
  }
  const wo = kind==="following" ? "Gefolgt" : "Follower";
  if(info) info.textContent="✓ "+data.count+" Konten als „"+wo+"“ übernommen.";
  toast(data.count+" Konten importiert ("+wo+")","ok");
  // Direkt anzeigen, damit du das Ergebnis siehst.
  if(kind==="following") showFollowing(); else showFollowers();
}
// „⬇ Alles Neue": lädt von ALLEN Konten der aktuellen Liste die neuesten
// Beiträge – bereits Heruntergeladenes überspringt der Server automatisch
// (Datenbank-Abgleich), es kommt also nur Fehlendes dazu.
async function downloadAllNew(){
  const names=(listAccts||[]).map(a=>a.username).filter(Boolean);
  if(!names.length){ toast("Die Liste ist leer.","err"); return; }
  const count=($("acct_count")&&$("acct_count").value)||12;
  const types=($("acct_types")&&$("acct_types").value)||"all";
  const was={all:"neueste Beiträge",photos:"neueste Fotos",videos:"neueste Videos/Reels",stories:"aktuelle Stories",everything:"neueste Beiträge + Stories & Highlights"}[types]||"neueste Beiträge";
  if(!confirm("Von ALLEN "+names.length+" Konten "+(types==="stories"?was:("je "+count+" "+was))+" auf den PC laden?\nBereits Heruntergeladenes wird übersprungen.")) return;
  const {data}=await api("/api/download-following",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({usernames:names, count, types})});
  if(data.error){ toast(data.error,"err"); return; }
  toast("Download läuft – nur Neues wird gespeichert …");
  if(acctTimer) clearInterval(acctTimer);
  acctTimer=setInterval(async ()=>{
    const {data:st}=await api("/api/download-following/status");
    if(!st || st.error){ clearInterval(acctTimer); acctTimer=null;
      const nb=$("all_new"); if(nb) nb.textContent="⬇ Alles Neue"; return; }
    const b=$("all_new");
    if(b && st.running) b.textContent="⬇ "+st.done+"/"+st.total+" · "+st.downloaded+" neu";
    if(!st.running && st.finished){ clearInterval(acctTimer); acctTimer=null;
      if(b) b.textContent="⬇ Alles Neue";
      toast("Fertig: "+st.downloaded+" neue Dateien gespeichert","ok"); }
  },1000);
}
async function doLogout(){ if(confirm("Wirklich abmelden?")){ await api("/api/logout",{method:"POST"}); location.reload(); } }

/* ---------- X-Videos (nur lokale Browser-Sitzung auf dem PC) ---------- */
let xResults=[], xSelected=new Set(), xPollTimer=null, xLastNotice="", xAvailable=true;
// Anmelde-Token gegen CSRF: liefert der lokale Server nur an diesen PC.
let xCsrfToken="";

function showXVideos(){
  cancelSelect(); pushView("x", showXVideos);
  viewMode="x"; setNav("nav_x");
  if(feedTimer){ clearInterval(feedTimer); feedTimer=null; }
  $("manage_panel").style.display="none"; $("login_panel").style.display="none";
  $("stories_row").style.display="none"; $("filterbar").style.display="none";
  $("pf_head").style.display="none"; $("subbar").innerHTML=""; $("more").innerHTML="";
  $("content").className="";
  $("content").innerHTML=`
    <section class="x-page">
      <div class="x-hero">
        <div class="x-mark">𝕏</div>
        <div><h1>X Videos</h1><div class="hint">Ohne X API – lokale Browser-Sitzung</div></div>
        <span class="x-status" id="x_session_status">Status wird geladen …</span>
      </div>
      <div class="panel x-login-box">
        <div class="x-login-head">
          <b>Bei X anmelden</b>
          <div class="hint">Die Anmeldung läuft im sichtbaren Browser auf diesem PC. Dein Passwort wird nur ins X-Formular eingetragen – es wird nicht gespeichert und nicht protokolliert. 2FA, CAPTCHA und Sicherheitsabfragen beantwortest du dort selbst.</div>
        </div>
        <form class="x-login-form" id="x_login_form" autocomplete="off" onsubmit="event.preventDefault();xSubmitLogin()">
          <label><span>E-Mail oder Benutzername</span>
            <input id="x_identifier" type="text" maxlength="254" autocomplete="off" spellcheck="false" placeholder="name@example.com"></label>
          <label><span>@Benutzername (optional)</span>
            <input id="x_username" type="text" maxlength="16" autocomplete="off" spellcheck="false" placeholder="@meinname"></label>
          <label><span>Passwort</span>
            <input id="x_password" type="password" maxlength="256" autocomplete="new-password" data-1p-ignore data-lpignore="true"></label>
          <div class="x-login-actions">
            <button class="btn" type="submit" id="x_login_submit">Bei X anmelden</button>
            <button class="btn ghost" type="button" id="x_login_btn" onclick="xOpenLogin()">X-Browser öffnen</button>
          </div>
        </form>
        <div class="hint x-login-note" id="x_login_note" style="display:none"></div>
      </div>
      <div class="panel x-search-box">
        <label class="x-query"><span>Hashtag oder Suchbegriff</span><input id="x_query" maxlength="120" placeholder="#fussball oder eine Phrase" onkeydown="if(event.key==='Enter')xStartSearch()"></label>
        <label><span>Anzahl</span><input id="x_limit" type="number" min="1" max="50" value="10"></label>
        <label><span>Sortierung</span><select id="x_sort"><option value="latest">Neueste</option><option value="top">Top</option></select></label>
        <label class="x-check"><input id="x_no_reposts" type="checkbox"> Reposts ausschließen</label>
        <div class="x-search-actions">
          <button class="btn" id="x_search_btn" onclick="xStartSearch()">Videos suchen</button>
          <button class="btn ghost" id="x_search_cancel" onclick="xCancelSearch()" style="display:none">Abbrechen</button>
        </div>
      </div>
      <div class="x-warning" id="x_warning" style="display:none"></div>
      <div class="x-progress" id="x_progress" style="display:none"><div class="x-progress-head"><span id="x_progress_text"></span><button class="btn ghost small" id="x_download_cancel" onclick="xCancelDownload()" style="display:none">Download abbrechen</button></div><div class="x-progress-track"><i id="x_progress_fill"></i></div></div>
      <div class="x-toolbar" id="x_toolbar" style="display:none">
        <span id="x_selected_count">0 ausgewählt</span>
        <button class="btn ghost small" onclick="xSelectAll(true)">Alle auswählen</button>
        <button class="btn ghost small" onclick="xSelectAll(false)">Keine</button>
        <button class="btn small" onclick="xDownloadSelected()">Ausgewählte herunterladen</button>
        <button class="btn ghost small" onclick="xDownloadAllNew()">Alle neuen herunterladen</button>
      </div>
      <div id="x_results"><div class="empty">Es wurde noch keine X-Suche gestartet.</div></div>
      <details class="panel x-recent"><summary>Letzte X-Downloads</summary><div id="x_recent" class="hint">Noch keine X-Downloads gespeichert.</div></details>
      <div class="hint x-legal">Lade nur öffentlich zugängliche Inhalte herunter, für die du die erforderlichen Rechte besitzt, und beachte die X-Nutzungsbedingungen. Die App umgeht keine Zugriffs- oder Schutzmechanismen.</div>
    </section>`;
  xLoadCsrf();
  xLoadStatus();
}

/* Holt das Anmelde-Token. Vom Handy/Tablet antwortet der Server mit 403 –
   dann bleibt das Passwortfeld ausgeblendet. */
async function xLoadCsrf(){
  const {status,data}=await api("/api/x/csrf");
  xCsrfToken=(status===200 && data && data.token) ? data.token : "";
  xSetLoginMode(!!xCsrfToken);
  return xCsrfToken;
}

function xSetLoginMode(isLocal){
  const form=$("x_login_form"), note=$("x_login_note");
  if(!form||!note) return;
  form.style.display=isLocal?"":"none";
  note.style.display=isLocal?"none":"block";
  if(!isLocal) note.textContent="Die X-Anmeldung mit Passwort ist nur direkt am PC möglich. Über das Netzwerk (Handy/Tablet) lehnt der Server sie ab.";
}

async function xLoadStatus(){
  const {data,status}=await api("/api/x/status");
  if(viewMode!=="x") return;
  if(status===0 || !data){ xSetWarning("Lokaler X-Status nicht erreichbar."); return; }
  xAvailable=!!data.available;
  const label=$("x_session_status"), login=$("x_login_btn"), submit=$("x_login_submit");
  if(data.local===false) xSetLoginMode(false);
  if(!data.available){
    label.textContent="Nicht verfügbar"; label.classList.add("bad");
    login.disabled=true; $("x_search_btn").disabled=true;
    if(submit) submit.disabled=true;
    xSetWarning(data.warning||data.error||"X-Suche ist auf diesem Gerät nicht verfügbar.");
  }else{
    if(submit) submit.disabled=false;
    label.classList.toggle("ok",!!data.session_confirmed);
    label.textContent=data.session_confirmed ? "Sitzung bereit" : (data.browser_open ? "Browser geöffnet" : "Sitzung nicht geöffnet");
    xSetWarning(data.warning||(!data.ffmpeg ? "FFmpeg wurde nicht gefunden. Die App nutzt nach Möglichkeit eine einzelne MP4-Variante; getrennte beste Video-/Audiospuren können FFmpeg benötigen." : ""));
  }
  if(data.search && data.search.items && data.search.items.length){
    xResults=data.search.items; xRenderResults();
  }
  xRenderRecent(data.recent_downloads||[]);
  xApplyJobs(data.search||{}, data.download||{});
  if((data.search&&data.search.running)||(data.download&&data.download.running)) xSchedulePoll();
}

function xSetWarning(message){
  const box=$("x_warning"); if(!box) return;
  box.textContent=message||""; box.style.display=message?"block":"none";
}

async function xOpenLogin(){
  const btn=$("x_login_btn"); btn.disabled=true; btn.textContent="Browser wird geöffnet …";
  const {data}=await api("/api/x/login/open",{method:"POST"});
  btn.disabled=false; btn.textContent="X-Browser öffnen";
  if(data.error){ toast(data.error,"err"); xSetWarning(data.error); return; }
  toast("X-Browser geöffnet. Du kannst dich dort auch direkt anmelden.","ok");
  const label=$("x_session_status"); if(label) label.textContent="Browser geöffnet";
}

/* Direkte Anmeldung: Die Zugangsdaten gehen nur an den lokalen Server, der
   sie ins sichtbare X-Fenster eintippt. Das Passwortfeld wird sofort danach
   geleert – gespeichert wird es nirgends. */
async function xSubmitLogin(){
  const idField=$("x_identifier"), userField=$("x_username"), pwField=$("x_password");
  if(!idField||!pwField) return;
  const identifier=(idField.value||"").trim();
  const username=(userField&&userField.value||"").trim();
  const password=pwField.value||"";
  if(!identifier){ toast("Bitte E-Mail-Adresse oder Benutzernamen eingeben.","err"); idField.focus(); return; }
  if(!password){ toast("Bitte das X-Passwort eingeben.","err"); pwField.focus(); return; }
  const btn=$("x_login_submit"); btn.disabled=true; btn.textContent="Anmeldung läuft …";
  let result={status:0,data:{}};
  try{
    if(!xCsrfToken) await xLoadCsrf();
    if(!xCsrfToken){ toast("Anmeldung nur direkt am PC möglich.","err"); return; }
    result=await api("/api/x/login",{method:"POST",
      headers:{"Content-Type":"application/json","X-CSRF-Token":xCsrfToken},
      body:JSON.stringify({identifier:identifier,username:username,password:password})});
  } finally {
    pwField.value="";                       // Passwort nicht im Formular liegen lassen
    btn.disabled=false; btn.textContent="Bei X anmelden";
  }
  const data=result.data||{};
  if(result.status===403){ xCsrfToken=""; xSetLoginMode(false); }
  if(data.error){ toast(data.error,"err"); xSetWarning(data.error); return; }
  xSetWarning("");
  toast(data.message||"Anmeldung im X-Fenster fortsetzen.", data.state==="signed_in"?"ok":"");
  xLoadStatus();
}

async function xStartSearch(){
  const payload={query:$("x_query").value,limit:$("x_limit").value,
    sort:$("x_sort").value,exclude_reposts:$("x_no_reposts").checked};
  const btn=$("x_search_btn"); btn.disabled=true;
  const {data}=await api("/api/x/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  btn.disabled=false;
  if(data.error){ toast(data.error,"err"); xSetWarning(data.error); return; }
  xResults=[]; xSelected.clear(); xRenderResults(); xLastNotice=""; xSchedulePoll(100);
}

async function xCancelSearch(){
  await api("/api/x/search/cancel",{method:"POST"});
  const b=$("x_search_cancel"); if(b) b.disabled=true;
}

async function xCancelDownload(){
  await api("/api/x/download/cancel",{method:"POST"});
  const b=$("x_download_cancel"); if(b) b.disabled=true;
}

function xSchedulePoll(delay){
  if(xPollTimer) clearTimeout(xPollTimer);
  xPollTimer=setTimeout(xPollJobs,delay===undefined?800:delay);
}

async function xPollJobs(){
  xPollTimer=null;
  if(viewMode!=="x") return;
  const [search,download]=await Promise.all([
    api("/api/x/search/status"), api("/api/x/download/status")
  ]);
  if(viewMode!=="x") return;
  const s=search.data||{}, d=download.data||{};
  if(s.items && (s.items.length || s.finished)){ xResults=s.items; xRenderResults(); }
  if(d.items){
    const completed=new Set(d.items.filter(i=>i.state==="downloaded"||i.state==="skipped").map(i=>i.post_id));
    if(completed.size){ xResults.forEach(p=>{ if(completed.has(p.post_id)) p.downloaded=true; }); xRenderResults(); }
  }
  xApplyJobs(s,d);
  const notice=(s.error||d.error||((s.finished&&!s.running)?s.message:"")||((d.finished&&!d.running)?d.message:""));
  if(notice && notice!==xLastNotice){ xLastNotice=notice; toast(notice,(s.error||d.error)?"err":"ok"); }
  if(s.running||d.running) xSchedulePoll();
}

function xApplyJobs(search,download){
  const searching=!!search.running, downloading=!!download.running;
  const sb=$("x_search_btn"), sc=$("x_search_cancel"), dc=$("x_download_cancel");
  if(sb) sb.disabled=!xAvailable||searching||downloading;
  if(sc){ sc.style.display=searching?"":"none"; sc.disabled=false; }
  if(dc){ dc.style.display=downloading?"":"none"; dc.disabled=false; }
  const box=$("x_progress"), text=$("x_progress_text"), fill=$("x_progress_fill");
  if(!box) return;
  if(searching){
    box.style.display="block"; text.textContent=search.message||("Suche: "+(search.found||0)+" gefunden");
    fill.style.width=Math.min(100,((search.found||0)/Math.max(1,search.total||1))*100)+"%";
  }else if(downloading){
    box.style.display="block"; text.textContent=download.message||("Download "+(download.done||0)+"/"+(download.total||0));
    fill.style.width=Math.min(100,((download.done||0)/Math.max(1,download.total||1))*100)+"%";
  }else if(search.error||download.error){
    box.style.display="block"; text.textContent=search.error||download.error; fill.style.width="0";
  }else{ box.style.display="none"; }
}

function xRenderResults(){
  const box=$("x_results"), toolbar=$("x_toolbar"); if(!box||!toolbar) return;
  if(!xResults.length){ toolbar.style.display="none"; box.innerHTML='<div class="empty">Noch keine Videobeiträge gefunden.</div>'; return; }
  toolbar.style.display="flex"; xUpdateSelected(); box.innerHTML="";
  const grid=document.createElement("div"); grid.className="x-grid";
  xResults.forEach(p=>{
    const card=document.createElement("article"); card.className="x-card"+(p.downloaded?" downloaded":"");
    const date=p.published_at?new Date(p.published_at).toLocaleString("de-DE"):"";
    card.innerHTML=`
      <label class="x-select"><input type="checkbox" ${xSelected.has(p.post_id)?"checked":""} aria-label="Beitrag auswählen"><span></span></label>
      <div class="x-preview">${p.preview_url?`<img loading="lazy" referrerpolicy="no-referrer" src="${esc(p.preview_url)}" alt="Vorschau">`:'<div class="empty">Keine Vorschau</div>'}<b>▶</b>${p.downloaded?'<i>✓ Gespeichert</i>':''}</div>
      <div class="x-card-body"><div class="x-author"><strong>${esc(p.author_name||p.username||"X")}</strong><span>@${esc(p.username||"")}</span></div>
      ${date?`<time>${esc(date)}</time>`:""}<p>${esc(p.caption||"Kein Textauszug verfügbar.")}</p>
      <div class="x-card-actions"><a class="btn ghost small" href="${esc(p.post_url)}" target="_blank" rel="noopener noreferrer">Auf X öffnen</a><button class="btn small" ${p.downloaded?"disabled":""}>${p.downloaded?"Gespeichert":"Herunterladen"}</button></div></div>`;
    card.querySelector('.x-select input').onchange=e=>{ if(e.target.checked)xSelected.add(p.post_id);else xSelected.delete(p.post_id);xUpdateSelected(); };
    card.querySelector('.x-card-actions button').onclick=()=>xDownloadOne(p.post_id);
    grid.appendChild(card);
  });
  box.appendChild(grid);
}

function xUpdateSelected(){ const el=$("x_selected_count"); if(el) el.textContent=xSelected.size+" ausgewählt"; }
function xRenderRecent(items){
  const box=$("x_recent"); if(!box) return;
  if(!items.length){ box.textContent="Noch keine X-Downloads gespeichert."; return; }
  box.innerHTML=items.map(item=>`<div class="x-recent-row"><span>@${esc(item.username||"")} · ${esc(item.search_query||"")}</span><a href="${esc(item.post_url)}" target="_blank" rel="noopener noreferrer">${timeAgo(item.downloaded_at)||"Auf X öffnen"} ↗</a></div>`).join("");
}
function xSelectAll(on){ xSelected.clear(); if(on)xResults.forEach(p=>{if(!p.downloaded)xSelected.add(p.post_id);}); xRenderResults(); }
function xDownloadOne(postId){ xStartDownload("/api/x/download",{post_id:postId}); }
function xDownloadSelected(){
  if(!xSelected.size){ toast("Bitte mindestens einen Beitrag auswählen.","err"); return; }
  xStartDownload("/api/x/download-selected",{post_ids:[...xSelected]});
}
function xDownloadAllNew(){ xStartDownload("/api/x/download-selected",{all_new:true}); }
async function xStartDownload(endpoint,payload){
  const {data}=await api(endpoint,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
  if(data.error){ toast(data.error,"err"); return; }
  xLastNotice=""; toast("X-Download gestartet …"); xSchedulePoll(100);
}

/* ---------- Tastatur / Scroll ---------- */
document.addEventListener("keydown", e=>{
  const tag=(e.target.tagName||"").toLowerCase();
  if($("lb").classList.contains("open")){
    if(e.key==="Escape") closeLightbox();
    if(e.key==="ArrowLeft") lbNav(-1);
    if(e.key==="ArrowRight") lbNav(1);
  }else if($("sv").classList.contains("open")){
    if(e.key==="Escape") closeStory();
    if(e.key==="ArrowLeft") storyNav(-1);
    if(e.key==="ArrowRight") storyNav(1);
  }else if(e.key==="Escape"){
    $("help").classList.remove("open");
    if($("manage_panel").style.display!=="none") $("manage_panel").style.display="none";
  }else if(e.key==="?" && tag!=="input" && tag!=="textarea"){
    $("help").classList.toggle("open");
  }else if(e.key==="/" && tag!=="input" && tag!=="textarea"){
    // „/“ springt in die Suche (wie bei vielen Web-Apps).
    e.preventDefault();
    const box=$("list_filter")||$("sq")||$("q_user");
    if(box){ box.focus(); box.select&&box.select(); }
  }
});
window.addEventListener("scroll", ()=>{ $("top").style.display = window.scrollY>600?"flex":"none"; });

// KEIN automatisches Aktualisieren im Hintergrund: Die Abo-Liste wird nur
// geladen, wenn du sie selbst öffnest (Gefolgt/Follower bzw. ⚙️ → Liste).
// Neue Follows erscheinen beim nächsten manuellen Öffnen der Liste.

// Als App installierbar machen (Service Worker; scheitert still auf http-LAN).
if("serviceWorker" in navigator){
  window.addEventListener("load", ()=>{ navigator.serviceWorker.register("/sw.js").catch(()=>{}); });
}

initBackground();
// Sofort einen neutralen Ladehinweis zeigen, BEVOR das Netzwerk antwortet –
// kein sichtbarer Blank-Screen mehr beim Öffnen.
if($("content")) $("content").innerHTML='<div class="empty"><span class="spin"></span></div>';
refreshStatus();
