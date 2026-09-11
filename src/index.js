const JSON_HEADERS = { "content-type": "application/json; charset=UTF-8", "cache-control": "no-store" };
const MAX_COMMENT_LENGTH = 500;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function extractCookie(setCookie, name) {
  if (!setCookie) return null;
  const re = new RegExp('(?:^|[,;]\\s*)' + name + '="?([^";,]+)"?', 'i');
  const m = setCookie.match(re);
  return m ? m[1] : null;
}

function base64UrlBytes(value) {
  let s = value.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const raw = atob(s);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function tokenFromSessionId(sessionId) {
  try {
    const clean = String(sessionId).replace(/^"|"$/g, "");
    let first = clean.split(":")[0];
    const compressed = first.startsWith(".");
    if (compressed) first = first.slice(1);
    let bytes = base64UrlBytes(first);
    if (compressed) {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
      bytes = new Uint8Array(await new Response(stream).arrayBuffer());
    }
    const data = JSON.parse(new TextDecoder().decode(bytes));
    return data && data.token ? String(data.token) : null;
  } catch {
    return null;
  }
}

function scratchHeaders(account, referer) {
  return {
    "accept": "application/json",
    "content-type": "application/json",
    "x-csrftoken": "a",
    "x-requested-with": "XMLHttpRequest",
    "x-token": account.token,
    "referer": referer,
    "cookie": `scratchsessionsid=${account.sessionId}; scratchcsrftoken=a; scratchlanguage=en;`,
  };
}

async function scratchLogin(username, password) {
  const response = await fetch("https://scratch.mit.edu/login/", {
    method: "POST",
    headers: {
      "accept": "application/json",
      "content-type": "application/json",
      "x-csrftoken": "a",
      "x-requested-with": "XMLHttpRequest",
      "referer": "https://scratch.mit.edu/",
      "origin": "https://scratch.mit.edu",
      "cookie": "scratchcsrftoken=a; scratchlanguage=en;",
    },
    body: JSON.stringify({ username, password, useMessages: true }),
    redirect: "manual",
  });

  const text = await response.text();
  let payload = null;
  try { payload = JSON.parse(text); } catch {}

  const sessionId = extractCookie(response.headers.get("set-cookie"), "scratchsessionsid");
  let token = null;
  if (Array.isArray(payload) && payload[0] && payload[0].token) token = String(payload[0].token);
  else if (payload && payload.token) token = String(payload.token);
  if (!token && sessionId) token = await tokenFromSessionId(sessionId);

  if (!response.ok || !sessionId || !token) {
    const detail = payload && (payload.msg || payload.message) ? (payload.msg || payload.message) : "ログイン情報を確認してください。";
    throw new Error(detail);
  }

  let canonical = username;
  try {
    const sessionResp = await fetch("https://scratch.mit.edu/session/", {
      method: "POST",
      headers: scratchHeaders({ sessionId, token }, "https://scratch.mit.edu/"),
      body: "{}",
    });
    if (sessionResp.ok) {
      const info = await sessionResp.json();
      if (info && info.user && info.user.username) canonical = info.user.username;
    }
  } catch {}

  return { username: canonical, sessionId, token };
}

function normalizeTarget(type, raw) {
  const value = String(raw || "").trim();
  if (!value) throw new Error("投稿先を入力してください。");

  if (type === "project") {
    const m = value.match(/(?:projects\/)?(\d+)/i);
    if (!m) throw new Error("Project ID またはURLを入力してください。");
    return m[1];
  }
  if (type === "studio") {
    const m = value.match(/(?:studios\/)?(\d+)/i);
    if (!m) throw new Error("Studio ID またはURLを入力してください。");
    return m[1];
  }
  if (type === "profile") {
    const m = value.match(/(?:users\/)?([A-Za-z0-9_-]{1,30})/i);
    if (!m) throw new Error("Scratchユーザー名 またはプロフィールURLを入力してください。");
    return m[1];
  }
  throw new Error("投稿先の種類が不正です。");
}

async function postComment(account, type, rawTarget, content) {
  const target = normalizeTarget(type, rawTarget);
  const body = JSON.stringify({ commentee_id: "", content, parent_id: "" });
  let endpoint;
  let referer;

  if (type === "project") {
    endpoint = `https://api.scratch.mit.edu/proxy/comments/project/${target}/`;
    referer = `https://scratch.mit.edu/projects/${target}/`;
  } else if (type === "studio") {
    endpoint = `https://api.scratch.mit.edu/proxy/comments/studio/${target}/`;
    referer = `https://scratch.mit.edu/studios/${target}/comments/`;
  } else {
    endpoint = `https://scratch.mit.edu/site-api/comments/user/${encodeURIComponent(target)}/add/`;
    referer = `https://scratch.mit.edu/users/${encodeURIComponent(target)}/`;
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: scratchHeaders(account, referer),
    body,
  });
  const text = await response.text();
  let payload = null;
  try { payload = JSON.parse(text); } catch {}

  if (!response.ok) {
    throw new Error(`Scratch API: HTTP ${response.status}`);
  }
  if (payload && payload.id) return { id: payload.id, target };
  if (type === "profile" && payload && !payload.error) return { id: payload.id || null, target };
  if (payload && (payload.error || payload.errors || payload.message)) {
    throw new Error(JSON.stringify(payload.error || payload.errors || payload.message));
  }
  return { id: null, target };
}

function page() {
  return `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scratch Comment Posting Tool</title>
<style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f5f6f7}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:900px;margin:0 auto;padding:28px 18px 60px}
header{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:22px;border-bottom:1px solid #d9dde3;padding-bottom:16px}
h1{font-size:25px;margin:0}header p{margin:6px 0 0;color:#666;font-size:14px}.status{font-size:13px;color:#555}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.panel{background:#fff;border:1px solid #d7dce2;border-radius:6px;padding:18px}
h2{font-size:17px;margin:0 0 14px}.row{display:grid;gap:7px;margin-bottom:12px}label{font-size:13px;font-weight:650}
input,select,textarea,button{font:inherit}input,select,textarea{width:100%;border:1px solid #8b929a;border-radius:3px;padding:9px 10px;background:#fff}
textarea{min-height:112px;resize:vertical}.buttons{display:flex;gap:8px;flex-wrap:wrap}button{border:1px solid #737a82;border-radius:3px;padding:8px 12px;background:#fff;cursor:pointer}button.primary{background:#4c97ff;border-color:#3887e8;color:#fff;font-weight:650}button.danger{color:#b42318}button:disabled{opacity:.55;cursor:default}
.note{font-size:12px;color:#666;line-height:1.6;margin-top:10px}.accounts{display:grid;gap:7px}.account{display:flex;justify-content:space-between;align-items:center;border:1px solid #dfe3e8;padding:8px 9px;border-radius:3px;gap:10px}.account.active{border-color:#4c97ff;background:#f3f8ff}.account button{padding:4px 7px;font-size:12px}.name{font-weight:650;overflow-wrap:anywhere}.small{font-size:12px;color:#666}
#message{min-height:22px;margin:12px 0 0;font-size:13px}.ok{color:#16794a}.err{color:#b42318}.history{grid-column:1/-1}.history-list{font-size:13px;display:grid;gap:7px}.history-item{border-top:1px solid #e4e7eb;padding-top:8px}.muted{color:#777}
@media(max-width:720px){.grid{grid-template-columns:1fr}header{align-items:start;flex-direction:column}.history{grid-column:auto}}
</style>
</head>
<body>
<div class="wrap">
<header><div><h1>Scratch Comment Posting Tool</h1><p>Cloudflare Worker Web版 / 複数ログイン対応</p></div><div class="status" id="accountCount">0 accounts</div></header>
<div class="grid">
<section class="panel">
<h2>アカウント追加</h2>
<form id="loginForm">
<div class="row"><label for="username">Scratchユーザー名</label><input id="username" autocomplete="username" required></div>
<div class="row"><label for="password">パスワード</label><input id="password" type="password" autocomplete="current-password" required></div>
<div class="buttons"><button class="primary" id="loginBtn" type="submit">ログインして追加</button></div>
</form>
<p class="note">パスワードは保存しません。ログイン後のScratchセッションはこのタブの sessionStorage にだけ保持され、タブを閉じると消えます。</p>
</section>
<section class="panel">
<h2>ログイン中アカウント</h2>
<div id="accounts" class="accounts"><div class="muted">まだありません。</div></div>
</section>
<section class="panel">
<h2>コメント投稿</h2>
<form id="postForm">
<div class="row"><label for="activeAccount">使用アカウント</label><select id="activeAccount" required></select></div>
<div class="row"><label for="targetType">投稿先</label><select id="targetType"><option value="project">Project</option><option value="studio">Studio</option><option value="profile">Profile</option></select></div>
<div class="row"><label for="target">ID / ユーザー名 / URL</label><input id="target" required placeholder="例: 123456789 または Scratch URL"></div>
<div class="row"><label for="content">コメント</label><textarea id="content" maxlength="500" required></textarea><div class="small"><span id="count">0</span>/500</div></div>
<div class="buttons"><button class="primary" id="postBtn" type="submit">1件投稿</button></div>
</form>
<div id="message"></div>
<p class="note">一度の操作で1件だけ送信します。同じ画面から複数アカウントを切り替えて使えます。</p>
</section>
<section class="panel">
<h2>状態</h2>
<p class="small">Worker: <strong>online</strong></p>
<p class="small">ログイン情報保存: <strong>タブ中のみ</strong></p>
<p class="small">一斉送信: <strong>なし</strong></p>
<p class="small">Python CLI: <strong>main_multi.py に残存</strong></p>
</section>
<section class="panel history"><h2>このタブの投稿履歴</h2><div id="history" class="history-list"><div class="muted">まだありません。</div></div></section>
</div>
</div>
<script>
(function(){
  var STORE='scratch-comment-web-accounts-v1';
  var HIST='scratch-comment-web-history-v1';
  var accounts=[];
  try{accounts=JSON.parse(sessionStorage.getItem(STORE)||'[]');if(!Array.isArray(accounts))accounts=[];}catch(e){accounts=[];}
  var history=[];
  try{history=JSON.parse(sessionStorage.getItem(HIST)||'[]');if(!Array.isArray(history))history=[];}catch(e){history=[];}
  var lastPostAt=0;
  var accountsBox=document.getElementById('accounts');
  var active=document.getElementById('activeAccount');
  var accountCount=document.getElementById('accountCount');
  var message=document.getElementById('message');
  var historyBox=document.getElementById('history');
  var content=document.getElementById('content');
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function save(){sessionStorage.setItem(STORE,JSON.stringify(accounts));}
  function saveHistory(){sessionStorage.setItem(HIST,JSON.stringify(history.slice(-30)));}
  function render(){
    accountCount.textContent=String(accounts.length)+' account'+(accounts.length===1?'':'s');
    accountsBox.innerHTML=''; active.innerHTML='';
    if(!accounts.length){accountsBox.innerHTML='<div class="muted">まだありません。</div>';active.innerHTML='<option value="">先にログインしてください</option>';}
    accounts.forEach(function(a,i){
      var div=document.createElement('div');div.className='account';
      div.innerHTML='<div><div class="name">@'+esc(a.username)+'</div><div class="small">Scratch session</div></div><div class="buttons"><button type="button" data-use="'+i+'">使用</button><button class="danger" type="button" data-del="'+i+'">削除</button></div>';
      accountsBox.appendChild(div);
      var opt=document.createElement('option');opt.value=String(i);opt.textContent='@'+a.username;active.appendChild(opt);
    });
    renderHistory();
  }
  function renderHistory(){
    if(!history.length){historyBox.innerHTML='<div class="muted">まだありません。</div>';return;}
    historyBox.innerHTML=history.slice().reverse().map(function(h){return '<div class="history-item"><strong>@'+esc(h.account)+'</strong> → '+esc(h.type)+' '+esc(h.target)+'<br><span class="muted">'+esc(h.time)+' / '+esc(h.content)+'</span></div>';}).join('');
  }
  accountsBox.addEventListener('click',function(e){var t=e.target;if(!(t instanceof HTMLElement))return;if(t.dataset.use!==undefined){active.value=t.dataset.use;document.getElementById('postForm').scrollIntoView({behavior:'smooth'});}if(t.dataset.del!==undefined){accounts.splice(Number(t.dataset.del),1);save();render();}});
  document.getElementById('loginForm').addEventListener('submit',async function(e){e.preventDefault();var btn=document.getElementById('loginBtn');btn.disabled=true;message.className='';message.textContent='ログイン中...';try{var username=document.getElementById('username').value.trim();var password=document.getElementById('password').value;var r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:username,password:password})});var data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'ログイン失敗');var idx=accounts.findIndex(function(a){return a.username.toLowerCase()===data.account.username.toLowerCase();});if(idx>=0)accounts[idx]=data.account;else accounts.push(data.account);save();document.getElementById('password').value='';render();active.value=String(idx>=0?idx:accounts.length-1);message.className='ok';message.textContent='ログイン成功: @'+data.account.username;}catch(err){message.className='err';message.textContent='ログイン失敗: '+(err&&err.message?err.message:String(err));}finally{btn.disabled=false;}});
  document.getElementById('postForm').addEventListener('submit',async function(e){e.preventDefault();var idx=Number(active.value);if(!Number.isInteger(idx)||!accounts[idx]){message.className='err';message.textContent='先に使用アカウントを選んでください。';return;}var now=Date.now();if(now-lastPostAt<15000){message.className='err';message.textContent='連続送信を避けるため、前回の操作から15秒あけてください。';return;}var btn=document.getElementById('postBtn');btn.disabled=true;message.className='';message.textContent='投稿中...';try{var type=document.getElementById('targetType').value;var target=document.getElementById('target').value.trim();var text=content.value.trim();var r=await fetch('/api/post',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({account:accounts[idx],type:type,target:target,content:text})});var data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'投稿失敗');lastPostAt=Date.now();history.push({account:accounts[idx].username,type:type,target:data.target||target,content:text,time:new Date().toLocaleString()});saveHistory();renderHistory();message.className='ok';message.textContent='投稿成功'+(data.commentId?' / コメントID: '+data.commentId:'');}catch(err){message.className='err';message.textContent='投稿失敗: '+(err&&err.message?err.message:String(err));}finally{btn.disabled=false;}});
  content.addEventListener('input',function(){document.getElementById('count').textContent=String(content.value.length);});
  render();
})();
</script>
</body>
</html>`;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "scratch-comment-posting-tool", runtime: "cloudflare-workers", web: true });
    }

    if (request.method === "POST" && url.pathname === "/api/login") {
      try {
        const body = await request.json();
        const username = String(body.username || "").trim();
        const password = String(body.password || "");
        if (!username || !password) return json({ ok: false, error: "ユーザー名とパスワードを入力してください。" }, 400);
        if (!/^[A-Za-z0-9_-]{1,30}$/.test(username)) return json({ ok: false, error: "Scratchユーザー名の形式が不正です。" }, 400);
        const account = await scratchLogin(username, password);
        return json({ ok: true, account });
      } catch (error) {
        return json({ ok: false, error: error instanceof Error ? error.message : String(error) }, 401);
      }
    }

    if (request.method === "POST" && url.pathname === "/api/post") {
      try {
        const body = await request.json();
        const account = body.account || {};
        const content = String(body.content || "").trim();
        if (!account.username || !account.sessionId || !account.token) return json({ ok: false, error: "ログインセッションがありません。" }, 401);
        if (!content) return json({ ok: false, error: "コメントが空です。" }, 400);
        if (content.length > MAX_COMMENT_LENGTH) return json({ ok: false, error: `コメントは${MAX_COMMENT_LENGTH}文字以内です。` }, 400);
        if (!["project", "studio", "profile"].includes(body.type)) return json({ ok: false, error: "投稿先の種類が不正です。" }, 400);
        const result = await postComment(account, body.type, body.target, content);
        return json({ ok: true, commentId: result.id, target: result.target });
      } catch (error) {
        return json({ ok: false, error: error instanceof Error ? error.message : String(error) }, 400);
      }
    }

    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(page(), {
        headers: {
          "content-type": "text/html; charset=UTF-8",
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          "referrer-policy": "same-origin",
        },
      });
    }

    return json({ ok: false, error: "Not found" }, 404);
  },
};
