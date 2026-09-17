const MAX_COMMENT_LENGTH = 500;
const MAX_QUEUE_ITEMS = 200;
const MAX_STUDIO_ITEMS = 100;
const MAX_STUDIO_BULK_PROJECTS = 1000;
const MIN_POST_INTERVAL_MS = 15_000;
const STUDIO_ACTION_INTERVAL_MS = 3_000;
const CLIENT_COOKIE = "scratch_bot_sid";
const SCRATCH_UA = "ScratchCommentBot-Worker/1.0";

// Workerのインスタンスが保持する短期セッションです。パスワードは保存しません。
// Workerの再起動・再デプロイで消えるため、長期運用ではDurable Object等を追加してください。
const clients = new Map();
let postTail = Promise.resolve();
let lastPostAt = 0;
let studioTail = Promise.resolve();
let lastStudioActionAt = 0;

class HttpError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

function fail(message, status = 400) {
  throw new HttpError(message, status);
}

function makeClientState() {
  return {
    accounts: new Map(),
    queue: [],
    queueRunning: false,
    queueStopRequested: false,
    queueBatchStartedAt: null,
    nextQueueId: 1,
    targetCache: new Map(),
    studioBulk: null,
    lastSeen: Date.now(),
  };
}

function parseCookies(header) {
  const result = {};
  for (const part of String(header || "").split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    const key = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (key) result[key] = value;
  }
  return result;
}

function sessionCookie(sid) {
  return `${CLIENT_COOKIE}=${sid}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400`;
}

function clientContext(request) {
  const cookies = parseCookies(request.headers.get("cookie"));
  let sid = cookies[CLIENT_COOKIE];
  let created = false;
  if (!sid || !/^[a-f0-9-]{20,80}$/i.test(sid)) {
    sid = crypto.randomUUID();
    created = true;
  }
  let state = clients.get(sid);
  if (!state) {
    state = makeClientState();
    clients.set(sid, state);
  }
  state.lastSeen = Date.now();
  // 放置された短期セッションを上限付きで掃除します。
  if (clients.size > 500) {
    const old = [...clients.entries()].sort((a, b) => a[1].lastSeen - b[1].lastSeen).slice(0, clients.size - 500);
    for (const [key] of old) clients.delete(key);
  }
  return {sid, state, created};
}

function jsonResponse(value, status = 200, context) {
  const headers = new Headers({
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  if (context?.created) headers.set("set-cookie", sessionCookie(context.sid));
  return new Response(JSON.stringify(value), {status, headers});
}

function errorResponse(error, context) {
  const status = Number(error?.status) || 500;
  const message = error instanceof HttpError ? error.message : "サーバー側で処理に失敗しました。";
  return jsonResponse({error: message}, status, context);
}

async function readJson(request) {
  const text = await request.text();
  if (text.length > 1_000_000) fail("入力が大きすぎます。", 413);
  if (!text.trim()) return {};
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value)) fail("JSON形式が不正です。");
    return value;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    fail("JSON形式が不正です。");
  }
}

function cleanMessage(value) {
  const message = String(value ?? "").trim();
  if (!message) fail("コメントを入力してください。");
  if (message.length > MAX_COMMENT_LENGTH) fail(`コメントは${MAX_COMMENT_LENGTH}文字以内にしてください。`);
  return message;
}

function normalizeTarget(type, rawValue) {
  const targetType = String(type || "").trim().toLowerCase();
  const raw = String(rawValue || "").trim();
  if (!["project", "studio", "profile"].includes(targetType)) fail("投稿先の種類が不正です。");
  if (!raw) fail("投稿先を入力してください。");
  let source = raw;
  if (raw.includes("://")) {
    try { source = new URL(raw).pathname; } catch { fail("URLの形式が不正です。"); }
  }
  if (targetType === "project") {
    const match = source.match(/\/projects\/(\d+)/i);
    const id = match ? match[1] : raw;
    if (!/^\d+$/.test(id)) fail("Projectは数字のIDまたはProject URLを指定してください。");
    return {type: targetType, id};
  }
  if (targetType === "studio") {
    const match = source.match(/\/studios\/(\d+)/i);
    const id = match ? match[1] : raw;
    if (!/^\d+$/.test(id)) fail("Studioは数字のIDまたはStudio URLを指定してください。");
    return {type: targetType, id};
  }
  const match = source.match(/\/users\/([A-Za-z0-9_-]{1,30})/i);
  const username = (match ? match[1] : raw).replace(/^\/+|\/+$/g, "");
  if (!/^[A-Za-z0-9_-]{1,30}$/.test(username)) fail("ProfileはScratchユーザー名またはProfile URLを指定してください。");
  return {type: targetType, id: username};
}

function accountNames(state, value, single = false) {
  const name = String(value || "").trim();
  if (name === "*") {
    if (single) fail("この操作ではScratchアカウントを1つ選択してください。");
    if (!state.accounts.size) fail("先にScratchへログインしてください。");
    return [...state.accounts.keys()];
  }
  if (!name) fail("使用するScratchアカウントを選択してください。");
  if (!state.accounts.has(name)) fail("選択したScratchアカウントはログインされていません。");
  return [name];
}

function getAccount(state, username) {
  const account = state.accounts.get(String(username || "").trim());
  if (!account) fail("選択したScratchアカウントはログインされていません。", 401);
  return account;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, Math.max(0, ms)));
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 15_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {...options, signal: controller.signal});
  } finally {
    clearTimeout(timer);
  }
}

function headerEntries(headers) {
  if (headers && typeof headers.getSetCookie === "function") return headers.getSetCookie();
  const value = headers?.get("set-cookie");
  return value ? [value] : [];
}

function cookieValue(values, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`(?:^|[;,]\\s*)${escaped}=(?:"([^"]+)"|([^;,\\s]+))`);
  for (const value of values || []) {
    const match = String(value).match(pattern);
    if (match) return decodeURIComponent(match[1] || match[2] || "");
  }
  return "";
}

async function scratchLogin(username, password) {
  const loginResponse = await fetchWithTimeout("https://scratch.mit.edu/login/", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      cookie: "scratchcsrftoken=a; scratchlanguage=en;",
      "x-requested-with": "XMLHttpRequest",
      "user-agent": SCRATCH_UA,
    },
    body: JSON.stringify({username, password}),
  });
  const sessionId = cookieValue(headerEntries(loginResponse.headers), "scratchsessionsid");
  if (!loginResponse.ok || !sessionId) {
    fail("Scratchログインに失敗しました。ユーザー名・パスワード、またはScratch側の認証状態を確認してください。", 401);
  }
  const cookie = `scratchsessionsid=${sessionId}; scratchcsrftoken=a; scratchlanguage=en;`;
  const sessionResponse = await fetchWithTimeout("https://scratch.mit.edu/session", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      cookie,
      "user-agent": SCRATCH_UA,
    },
  });
  const sessionData = await sessionResponse.json().catch(() => ({}));
  const user = sessionData?.user || {};
  const actualUsername = String(user.username || username);
  const xtoken = String(user.token || "");
  if (!sessionResponse.ok || !xtoken) fail("Scratchセッション情報を取得できませんでした。", 502);

  const profileResponse = await fetchWithTimeout(`https://api.scratch.mit.edu/users/${encodeURIComponent(actualUsername)}`, {
    headers: {accept: "application/json", "user-agent": SCRATCH_UA},
  });
  const profile = await profileResponse.json().catch(() => ({}));
  if (!profileResponse.ok || !profile?.id) fail("Scratchユーザー情報を取得できませんでした。", 502);
  return {username: actualUsername, userId: String(profile.id), sessionId, cookie, xtoken};
}

async function scratchRequest(account, url, options = {}) {
  const headers = new Headers({
    accept: "application/json",
    "user-agent": SCRATCH_UA,
    "x-csrftoken": "a",
    "x-requested-with": "XMLHttpRequest",
    origin: "https://scratch.mit.edu",
    referer: "https://scratch.mit.edu/",
    cookie: account.cookie,
  });
  if (account.xtoken) headers.set("x-token", account.xtoken);
  for (const [key, value] of Object.entries(options.headers || {})) headers.set(key, value);
  const requestOptions = {...options, headers};
  delete requestOptions.signal;
  return fetchWithTimeout(url, requestOptions);
}

async function jsonBody(response) {
  return response.json().catch(() => null);
}

async function ensureScratchOk(response, label) {
  if (response.ok) return;
  if (response.status === 429) fail(`${label}でScratch側の制限（429）が返りました。時間を置いて再試行してください。`, 429);
  if (response.status === 401 || response.status === 403) fail(`${label}が拒否されました。ログイン状態または権限を確認してください。`, response.status);
  fail(`${label}に失敗しました（HTTP ${response.status}）。`, 502);
}

async function fetchPaged(account, baseUrl, limit) {
  const result = [];
  const pageSize = 40;
  for (let offset = 0; offset < limit; offset += pageSize) {
    const count = Math.min(pageSize, limit - offset);
    const joiner = baseUrl.includes("?") ? "&" : "?";
    const response = await scratchRequest(account, `${baseUrl}${joiner}limit=${count}&offset=${offset}&cachebust=${Date.now()}`);
    await ensureScratchOk(response, "Scratchの一覧取得");
    const data = await jsonBody(response);
    if (!Array.isArray(data)) break;
    result.push(...data);
    if (data.length < count) break;
  }
  return result.slice(0, limit);
}

async function resolveTarget(state, account, target) {
  if (target.type !== "project") return target;
  const key = `${account.username}:${target.type}:${target.id}`;
  const cached = state.targetCache.get(key);
  if (cached) return cached;
  const response = await scratchRequest(account, `https://api.scratch.mit.edu/projects/${target.id}`);
  await ensureScratchOk(response, "Projectの取得");
  const data = await jsonBody(response);
  const author = String(data?.author?.username || data?.author?.name || "");
  if (!data?.id || !author) fail("Projectの作者名を取得できませんでした。", 502);
  const resolved = {...target, author};
  state.targetCache.set(key, resolved);
  return resolved;
}

async function createComment(state, account, target, content, parentId = "", commenteeId = "") {
  const resolved = await resolveTarget(state, account, target);
  const operation = async () => {
    const wait = MIN_POST_INTERVAL_MS - (Date.now() - lastPostAt);
    if (wait > 0) await sleep(wait);
    let endpoint;
    if (resolved.type === "project") endpoint = `https://api.scratch.mit.edu/proxy/comments/project/${resolved.id}/`;
    else if (resolved.type === "studio") endpoint = `https://api.scratch.mit.edu/proxy/comments/studio/${resolved.id}/`;
    else endpoint = `https://scratch.mit.edu/site-api/comments/user/${encodeURIComponent(resolved.id)}/add/`;
    const response = await scratchRequest(account, endpoint, {
      method: "POST",
      headers: {"content-type": "application/json", referer: "https://scratch.mit.edu/"},
      body: JSON.stringify({commentee_id: commenteeId || "", content, parent_id: parentId || ""}),
    });
    const text = await response.text();
    if (!response.ok) {
      if (response.status === 429) fail("投稿が制限されました（429）。最低間隔を守って時間を置いてください。", 429);
      fail(`Scratchへの投稿に失敗しました（HTTP ${response.status}）。`, 502);
    }
    let parsed = {};
    try { parsed = JSON.parse(text); } catch { /* Profile投稿はHTMLを返す場合があります。 */ }
    lastPostAt = Date.now();
    const id = parsed?.id || (text.match(/comments-(\d+)/i) || [])[1] || "投稿済み";
    return String(id);
  };
  const next = postTail.then(operation, operation);
  postTail = next.catch(() => {});
  return next;
}

function studioId(value) {
  const raw = String(value || "").trim();
  if (!raw) fail("スタジオIDまたはStudio URLを入力してください。");
  let source = raw;
  if (raw.includes("://")) {
    try { source = new URL(raw).pathname; } catch { fail("Studio URLの形式が不正です。"); }
  }
  const match = source.match(/\/studios\/(\d+)/i);
  const id = match ? match[1] : raw.replace(/^\/+|\/+$/g, "");
  if (!/^\d+$/.test(id)) fail("Studioは数字のIDまたはStudio URLを指定してください。");
  return id;
}

async function ownedStudio(state, username, rawId) {
  const account = getAccount(state, username);
  const id = studioId(rawId);
  const response = await scratchRequest(account, `https://api.scratch.mit.edu/studios/${id}`);
  await ensureScratchOk(response, "スタジオ情報の取得");
  const studio = await jsonBody(response);
  if (!studio?.id || String(studio.host) !== String(account.userId)) {
    fail("ログイン中アカウントが所有するスタジオだけ利用できます。", 403);
  }
  return {account, id, studio};
}

function itemAuthor(item) {
  return String(item?.author?.username || item?.author?.name || item?.author_name || "");
}

function itemText(item) {
  return String(item?.content || item?.text || "").replace(/<[^>]*>/g, "").trim();
}

async function studioSnapshot(state, username, rawId) {
  const {account, id, studio} = await ownedStudio(state, username, rawId);
  const [projects, comments, curators, managers] = await Promise.all([
    fetchPaged(account, `https://api.scratch.mit.edu/studios/${id}/projects`, MAX_STUDIO_ITEMS),
    fetchPaged(account, `https://api.scratch.mit.edu/studios/${id}/comments/`, MAX_STUDIO_ITEMS),
    fetchPaged(account, `https://api.scratch.mit.edu/studios/${id}/curators`, MAX_STUDIO_ITEMS),
    fetchPaged(account, `https://api.scratch.mit.edu/studios/${id}/managers`, MAX_STUDIO_ITEMS),
  ]);
  return {
    id: String(studio.id || id),
    title: String(studio.title || ""),
    owner: username,
    projects: projects.map(item => ({id: String(item.id || ""), title: String(item.title || "（タイトル不明）"), author: itemAuthor(item)})).filter(item => /^\d+$/.test(item.id)),
    comments: comments.map(item => ({id: String(item.id || ""), author: itemAuthor(item), content: itemText(item), created: String(item.datetime_created || item.created || "")})).filter(item => /^\d+$/.test(item.id)),
    curators: curators.map(item => ({username: String(item.username || "")})).filter(item => /^[A-Za-z0-9_-]{1,30}$/.test(item.username)),
    managers: managers.map(item => ({username: String(item.username || "")})).filter(item => /^[A-Za-z0-9_-]{1,30}$/.test(item.username)),
    limits: {projects: projects.length >= MAX_STUDIO_ITEMS, comments: comments.length >= MAX_STUDIO_ITEMS, curators: curators.length >= MAX_STUDIO_ITEMS, managers: managers.length >= MAX_STUDIO_ITEMS},
  };
}

function studioListEndpoint(id, kind) {
  if (kind === "projects") return `https://api.scratch.mit.edu/studios/${id}/projects`;
  if (kind === "comments") return `https://api.scratch.mit.edu/studios/${id}/comments/`;
  if (kind === "curators") return `https://api.scratch.mit.edu/studios/${id}/curators`;
  if (kind === "managers") return `https://api.scratch.mit.edu/studios/${id}/managers`;
  fail("一括整理対象が不正です。");
}

async function studioTargets(account, id, kind) {
  const limit = kind === "projects" ? MAX_STUDIO_BULK_PROJECTS : MAX_STUDIO_ITEMS;
  const list = await fetchPaged(account, studioListEndpoint(id, kind), limit);
  const result = [];
  const seen = new Set();
  for (const item of list) {
    const target = kind === "projects" || kind === "comments" ? String(item.id || "") : String(item.username || "");
    if (!target || seen.has(target)) continue;
    if ((kind === "projects" || kind === "comments") && !/^\d+$/.test(target)) continue;
    if ((kind === "curators" || kind === "managers") && !/^[A-Za-z0-9_-]{1,30}$/.test(target)) continue;
    if ((kind === "curators" || kind === "managers") && target.toLowerCase() === account.username.toLowerCase()) continue;
    seen.add(target);
    result.push(target);
  }
  return result;
}

async function mutateStudio(account, id, kind, target) {
  let response;
  if (kind === "projects") {
    response = await scratchRequest(account, `https://api.scratch.mit.edu/studios/${id}/project/${target}`, {method: "DELETE"});
  } else if (kind === "comments") {
    response = await scratchRequest(account, `https://api.scratch.mit.edu/proxy/comments/studio/${id}/comment/${target}/`, {method: "DELETE"});
  } else {
    response = await scratchRequest(account, `https://scratch.mit.edu/site-api/users/curators-in/${id}/remove/?usernames=${encodeURIComponent(target)}`, {method: "PUT", headers: {"content-type": "application/json"}});
  }
  const text = await response.text();
  await ensureScratchOk(new Response(text, {status: response.status, headers: response.headers}), "スタジオ整理");
  try {
    const parsed = JSON.parse(text);
    if (parsed && parsed.code && !["200", "OK", "Success", "success"].includes(String(parsed.code))) {
      fail(`Scratch側の操作に失敗しました: ${parsed.message || parsed.code}`, 502);
    }
  } catch { /* 空レスポンスやHTMLはHTTPステータスで判定します。 */ }
}

async function serializedStudioMutation(state, account, id, kind, target, stopCheck = () => false) {
  const operation = async () => {
    const wait = STUDIO_ACTION_INTERVAL_MS - (Date.now() - lastStudioActionAt);
    if (wait > 0) await sleep(wait);
    if (stopCheck()) return false;
    await mutateStudio(account, id, kind, target);
    lastStudioActionAt = Date.now();
    return true;
  };
  const next = studioTail.then(operation, operation);
  studioTail = next.catch(() => {});
  return next;
}

function bulkClient(job) {
  if (!job) return null;
  return {
    status: job.status,
    accountUsername: job.accountUsername,
    studioId: job.studioId,
    kind: job.kind,
    label: job.label,
    total: job.total,
    completed: job.completed,
    removed: [...job.removed],
    failed: [...job.failed],
    current: job.current,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt,
  };
}

async function runStudioBulk(state, username, id, targets, kind, job) {
  const account = state.accounts.get(username);
  try {
    if (!account) fail("ログイン中アカウントが見つかりません。", 401);
    await ownedStudio(state, username, id);
    for (const target of targets) {
      if (job.stopRequested) { job.status = "stopped"; break; }
      job.current = target;
      try {
        const completed = await serializedStudioMutation(state, account, id, kind, target, () => job.stopRequested);
        if (!completed) { job.status = "stopped"; break; }
        job.removed.push(target);
        job.completed += 1;
        job.current = null;
      } catch (error) {
        job.failed.push({id: target, error: error.message || "整理に失敗しました。"});
        job.status = "failed";
        job.current = null;
        break;
      }
    }
    if (job.status === "running") job.status = "completed";
  } catch (error) {
    job.status = "failed";
    job.current = null;
    job.failed.push({id: null, error: error.message || "整理に失敗しました。"});
  } finally {
    job.finishedAt = new Date().toISOString();
    job.stopRequested = false;
  }
}

const BULK_LABELS = {
  projects: "プロジェクトをスタジオから外す",
  comments: "コメントを削除",
  curators: "キュレーターを外す",
  managers: "マネージャーを外す",
};

async function startStudioBulk(state, data, ctx) {
  if (data.confirm !== true) fail("操作確認が必要です。画面の確認ダイアログから実行してください。");
  const kind = String(data.kind || "").trim().toLowerCase();
  if (!BULK_LABELS[kind]) fail("一括整理対象が不正です。");
  const username = String(data.accountUsername || "").trim();
  if (username === "*") fail("一括整理ではアカウントを1つ選択してください。");
  const {account, id} = await ownedStudio(state, username, data.studioId);
  if (state.studioBulk?.status === "running") fail("すでに一括整理が実行中です。", 409);
  const targets = await studioTargets(account, id, kind);
  if (data.expectedCount !== undefined && Number(data.expectedCount) !== targets.length) {
    fail("対象件数が変わりました。スタジオを再読み込みしてから実行してください。");
  }
  if (!targets.length) return {ok: true, started: false, total: 0, message: `${BULK_LABELS[kind]}の対象はありません。`};
  const job = {
    status: "running", accountUsername: username, studioId: id, kind,
    label: BULK_LABELS[kind], total: targets.length, completed: 0,
    removed: [], failed: [], current: null,
    startedAt: new Date().toISOString(), finishedAt: null, stopRequested: false,
  };
  state.studioBulk = job;
  ctx.waitUntil(runStudioBulk(state, username, id, targets, kind, job));
  return {ok: true, started: true, total: targets.length, kind};
}

async function studioAction(state, data) {
  if (data.confirm !== true) fail("操作確認が必要です。画面の確認ダイアログから実行してください。");
  const action = String(data.action || "").trim().toLowerCase();
  const map = {remove_project: "projects", delete_comment: "comments", remove_curator: "curators", remove_manager: "managers"};
  const kind = map[action];
  if (!kind) fail("整理操作が不正です。");
  const username = String(data.accountUsername || "").trim();
  const target = String(data.target || "").trim();
  if (!target) fail("整理対象が指定されていません。");
  const {account, id} = await ownedStudio(state, username, data.studioId);
  if ((kind === "projects" || kind === "comments") && !/^\d+$/.test(target)) fail("対象IDが不正です。");
  if ((kind === "curators" || kind === "managers") && !/^[A-Za-z0-9_-]{1,30}$/.test(target)) fail("ユーザー名が不正です。");
  if ((kind === "curators" || kind === "managers") && target.toLowerCase() === username.toLowerCase()) fail("所有者自身は整理対象にできません。");
  const targets = await studioTargets(account, id, kind);
  if (!targets.includes(target)) fail("その対象は現在の一覧にありません。先に再読み込みしてください。");
  await serializedStudioMutation(state, account, id, kind, target);
  return {ok: true, action, target};
}

function queueClient(item, state) {
  const result = {...item};
  if (result.status === "waiting" && state.queueBatchStartedAt !== null) {
    const due = state.queueBatchStartedAt + Number(result.delaySeconds || 0) * 1000;
    result.remainingSeconds = Math.max(0, Math.ceil((due - Date.now()) / 1000));
  } else result.remainingSeconds = null;
  return result;
}

function statePayload(state) {
  return {
    loggedIn: state.accounts.size > 0,
    accounts: [...state.accounts.values()].map(account => ({username: account.username})),
    queue: state.queue.map(item => queueClient(item, state)),
    queueRunning: state.queueRunning,
    queueBatchStartedAt: state.queueBatchStartedAt,
    studioBulk: bulkClient(state.studioBulk),
  };
}

async function runQueue(state, startedAt, ids) {
  try {
    for (const id of ids) {
      if (state.queueStopRequested) break;
      const item = state.queue.find(value => value.id === id);
      if (!item || item.status !== "waiting") continue;
      const due = startedAt + Number(item.delaySeconds || 0) * 1000;
      while (!state.queueStopRequested && Date.now() < due) await sleep(Math.min(250, due - Date.now()));
      if (state.queueStopRequested) break;
      item.status = "sending";
      item.sent = [];
      item.failed = [];
      try {
        const target = normalizeTarget(item.targetType, item.targetId);
        for (const username of accountNames(state, item.accountUsername)) {
          try {
            const account = getAccount(state, username);
            const commentId = await createComment(state, account, target, cleanMessage(item.message));
            item.sent.push({username, commentId});
          } catch (error) {
            item.failed.push({username, error: error.message || "投稿に失敗しました。"});
          }
        }
        item.status = item.failed.length ? (item.sent.length ? "partial" : "failed") : "sent";
      } catch (error) {
        item.status = "failed";
        item.failed = [{error: error.message || "投稿に失敗しました。"}];
      }
      item.finishedAt = new Date().toISOString();
    }
  } finally {
    state.queueRunning = false;
    state.queueStopRequested = false;
    state.queueBatchStartedAt = null;
  }
}

async function assetResponse(request, env) {
  if (env.ASSETS?.fetch) return env.ASSETS.fetch(request);
  return new Response("public/index.html をAssets bindingで設定してください。", {status: 503});
}

export default {
  async fetch(request, env, ctx) {
    const context = clientContext(request);
    const {state} = context;
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/healthz") return jsonResponse({ok: true, runtime: "cloudflare-worker"}, 200, context);
      if (request.method === "GET" && url.pathname === "/api/state") return jsonResponse(statePayload(state), 200, context);
      if (request.method === "GET") return assetResponse(request, env);
      if (request.method !== "POST") return jsonResponse({error: "メソッドが対応していません。"}, 405, context);
      const data = await readJson(request);

      if (url.pathname === "/api/login") {
        const username = String(data.username || "").trim();
        const password = String(data.password || "");
        if (!/^[A-Za-z0-9_-]{1,30}$/.test(username) || !password || password.length > 200) fail("ユーザー名またはパスワードを確認してください。");
        const account = await scratchLogin(username, password);
        state.accounts.set(account.username, account);
        return jsonResponse({ok: true, username: account.username}, 200, context);
      }
      if (url.pathname === "/api/logout") {
        const username = String(data.accountUsername || "").trim();
        if (state.studioBulk?.status === "running" && state.studioBulk.accountUsername === username) state.studioBulk.stopRequested = true;
        state.accounts.delete(username);
        return jsonResponse({ok: true}, 200, context);
      }
      if (url.pathname === "/api/queue") {
        if (state.queueRunning) fail("一括投稿中はキューを変更できません。", 409);
        if (state.queue.length >= MAX_QUEUE_ITEMS) fail(`キューは最大${MAX_QUEUE_ITEMS}件です。`);
        accountNames(state, data.accountUsername);
        const target = normalizeTarget(data.targetType, data.targetId);
        const message = cleanMessage(data.message);
        const delaySeconds = Number(data.delaySeconds || 0);
        if (!Number.isInteger(delaySeconds) || delaySeconds < 0 || delaySeconds > 86_400) fail("投稿までの秒数は0〜86400秒で指定してください。");
        const item = {id: state.nextQueueId++, accountUsername: String(data.accountUsername), targetType: target.type, targetId: target.id, message, delaySeconds, status: "queued", sent: [], failed: [], createdAt: new Date().toISOString()};
        state.queue.push(item);
        return jsonResponse({ok: true, item: queueClient(item, state)}, 200, context);
      }
      if (url.pathname === "/api/queue/start") {
        if (state.queueRunning) fail("一括投稿はすでに実行中です。", 409);
        const pending = state.queue.filter(item => item.status === "queued");
        if (!pending.length) fail("投稿待ちのコメントがありません。");
        state.queueRunning = true;
        state.queueStopRequested = false;
        state.queueBatchStartedAt = Date.now();
        const startedAt = state.queueBatchStartedAt;
        const ids = pending.map(item => { item.status = "waiting"; return item.id; });
        ctx.waitUntil(runQueue(state, startedAt, ids));
        return jsonResponse({ok: true, count: ids.length, startedAt}, 200, context);
      }
      if (url.pathname === "/api/queue/repeat") {
        if (state.queueRunning) fail("一括投稿はすでに実行中です。", 409);
        if (!state.queue.length) fail("繰り返す投稿キューがありません。");
        if (state.queue.every(item => item.status === "queued")) fail("まだ実行済みのキューがありません。一括開始を使ってください。");

        state.queueRunning = true;
        state.queueStopRequested = false;
        state.queueBatchStartedAt = Date.now();
        const startedAt = state.queueBatchStartedAt;
        const ids = state.queue.map(item => {
          item.status = "waiting";
          item.sent = [];
          item.failed = [];
          delete item.startedAt;
          delete item.finishedAt;
          return item.id;
        });
        ctx.waitUntil(runQueue(state, startedAt, ids));
        return jsonResponse({ok: true, count: ids.length, startedAt}, 200, context);
      }
      if (url.pathname === "/api/queue/stop") {
        state.queueStopRequested = true;
        for (const item of state.queue) if (item.status === "waiting") item.status = "cancelled";
        return jsonResponse({ok: true}, 200, context);
      }
      if (url.pathname === "/api/queue/clear") {
        if (state.queueRunning) fail("一括投稿中はキューを空にできません。", 409);
        state.queue = [];
        return jsonResponse({ok: true}, 200, context);
      }
      const queueMatch = url.pathname.match(/^\/api\/queue\/(\d+)\/delete$/);
      if (queueMatch) {
        if (state.queueRunning) fail("一括投稿中はキューを変更できません。", 409);
        const id = Number(queueMatch[1]);
        const before = state.queue.length;
        state.queue = state.queue.filter(item => item.id !== id);
        if (state.queue.length === before) fail("キュー項目が見つかりません。", 404);
        return jsonResponse({ok: true}, 200, context);
      }
      if (url.pathname === "/api/studio/inspect") {
        const username = String(data.accountUsername || "").trim();
        const studio = await studioSnapshot(state, username, data.studioId);
        return jsonResponse({ok: true, studio}, 200, context);
      }
      if (url.pathname === "/api/studio/action") {
        return jsonResponse(await studioAction(state, data), 200, context);
      }
      if (url.pathname === "/api/studio/bulk/start") {
        return jsonResponse(await startStudioBulk(state, data, ctx), 200, context);
      }
      if (url.pathname === "/api/studio/bulk/stop") {
        const username = String(data.accountUsername || "").trim();
        const id = studioId(data.studioId);
        const job = state.studioBulk;
        if (!job || job.status !== "running") fail("実行中の一括整理はありません。", 409);
        if (job.accountUsername !== username || String(job.studioId) !== String(id)) fail("別のスタジオの一括整理は停止できません。", 403);
        job.stopRequested = true;
        return jsonResponse({ok: true, stopping: true}, 200, context);
      }
      return jsonResponse({error: "見つかりません。"}, 404, context);
    } catch (error) {
      return errorResponse(error, context);
    }
  },
};
