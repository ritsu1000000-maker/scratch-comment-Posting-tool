# Scratch Comment Posting Tool

Scratch の **Project / Studio / Profile** にコメントするツールです。

このリポジトリには次の2種類があります。

- **Cloudflare Worker Web版**: ブラウザから複数アカウントにログインし、使用アカウントを切り替えて1件ずつコメント投稿
- **Python CLI版**: `main_multi.py` で複数アカウント、毎日・無期限予約、投稿履歴などを管理

## Cloudflare Worker Web版

`wrangler.jsonc` の `main` は `src/index.js` を指定しています。

```bash
npx wrangler deploy
```

デプロイ後にWorkerのURLを開くと、確認ページではなくWeb UIが表示されます。

### Web版の機能

- Scratchユーザー名 / パスワードでログイン
- 複数アカウントを同じ画面に追加
- 使用アカウントを切り替え
- Project / Studio / Profile へコメント投稿
- ID、ユーザー名、Scratch URLを入力可能
- 500文字制限
- このタブ内の投稿履歴
- `/health` ヘルスチェック

### Web版のログイン情報

- Scratchパスワードは保存しません。
- ログイン後のScratchセッション情報はブラウザの `sessionStorage` に保存します。
- タブを閉じるとWeb版のログイン状態は消えます。
- 一度の操作で送信するコメントは1件です。
- 画面側で連続送信を避けるため15秒間隔を入れています。

## Python CLI版

Windowsでは `install.bat` を実行し、その後 `start.bat` を起動してください。

または:

```bash
python -m pip install -r requirements.txt
python main_multi.py
```

### CLI版の主な機能

- 複数Scratchアカウントを起動中に保持
- Project / Studio / Profile へのコメント投稿
- 使用アカウントを選択して今すぐ1件投稿
- 予約ごとに使用アカウントを指定
- 毎日・無期限予約
- 予約件数はアプリ側の固定上限なし
- 予約内容の編集 / 削除 / ON・OFF
- 投稿先の接続確認
- 投稿履歴を `post_log.jsonl` に保存
- 500文字を超えるコメントを事前に検出
- 自動投稿時は最低15秒の間隔を維持

## CLI版のデータファイル

- `schedules.json` : 毎日・無期限予約一覧
- `schedule_state.json` : 同じ日に重複実行しないための状態
- `post_log.jsonl` : 投稿成功 / 失敗の履歴

Scratchのパスワードはこれらのデータファイルには保存しません。

## 注意

Scratch側のコメント無効設定、ミュート、認証、レート制限などは回避しません。Scratch側の仕様変更やホスティング元のネットワーク制限によって、ログインや投稿が失敗する場合があります。
