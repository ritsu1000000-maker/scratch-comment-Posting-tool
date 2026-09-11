export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return Response.json({
        ok: true,
        service: "scratch-comment-posting-tool",
        runtime: "cloudflare-workers",
      });
    }

    const html = `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Scratch Comment Posting Tool</title>
  <style>
    body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f6f7f8;color:#1f2328}
    main{max-width:760px;margin:64px auto;padding:0 20px}
    section{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:24px}
    code{background:#f3f4f6;padding:2px 6px;border-radius:4px}
    .ok{font-weight:700}
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Scratch Comment Posting Tool</h1>
      <p class="ok">Cloudflare Worker は正常に起動しています。</p>
      <p>このページは Cloudflare デプロイ確認用です。</p>
      <p>ヘルスチェック: <code>/health</code></p>
      <p>Python CLI 本体は <code>main_multi.py</code> としてリポジトリに残っています。</p>
    </section>
  </main>
</body>
</html>`;

    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=UTF-8",
        "cache-control": "no-store",
      },
    });
  },
};
