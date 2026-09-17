# Scratch Comment Bot — Cloudflare Worker版

Python版をCloudflare Workers用のJavaScriptへ移した構成です。`src/index.js`がWorker本体、`public/index.html`が画面です。

## できること

- Scratchログイン（パスワードは保存せず、短期セッションだけ保持）
- Project / Studio / Profileへの投稿キュー
- 一括開始から指定秒後の投稿
- 複数ログイン済みアカウントの順番投稿
- 投稿間隔は最低15秒
- 所有者確認つきStudio整理
  - プロジェクトをStudioから外す（プロジェクト本体は削除しない）
  - Studioコメントを削除
  - キュレーター・マネージャーをStudioから外す
  - 各カテゴリの一括処理と停止

## デプロイ

Node.js 18以上を用意し、このフォルダで実行します。

```text
npm install
npx wrangler login
npx wrangler deploy
```

`wrangler.jsonc`のWorker名は、既存の`scratch-comment-bot-web`に合わせています。別Workerにしたいときだけ`name`を変更してください。APIトークンやパスワードをチャットへ貼り付けないでください。

## 重要な制限

- Cloudflare Workerの短期メモリを使うため、再起動・再デプロイ・別インスタンスへの切り替えでログイン状態とキューが消えます。
- 長時間のキューや大量処理を常時動かす用途には、Durable ObjectまたはCloudflare Queuesへの移行が必要です。
- Scratch側のレート制限や利用規約を守ってください。CAPTCHAやセキュリティ検証は回避しません。
- このコードをデプロイしても、別名のWorkerやGitHubリポジトリが自動で書き換わることはありません。
