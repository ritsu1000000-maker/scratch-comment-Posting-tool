# Scratch Comment Bot v3

Scratchの **Project / Studio** のどちらかを投稿先として指定し、
決まった時刻にコメントを投稿できるPythonツールです。

## v3の追加機能

- Projectを投稿先に設定
- Studioを投稿先に設定
- 予約ごとに投稿先を保存
- ProjectとStudioの予約を混在可能
- 今すぐ投稿でもProject / Studioを選択可能
- v2の古いProject予約も自動で引き継ぎ

## インストール

```bash
pip install -r requirements.txt
```

## 起動

`start.bat`

または:

```bash
python main.py
```

## 予約例：Project

```text
投稿先を選択
1. Project
2. Studio
選択 > 1

プロジェクトID: 123456789
投稿時刻: 18:30
投稿するコメント: 今日もよろしく！
```

## 予約例：Studio

```text
投稿先を選択
1. Project
2. Studio
選択 > 2

Studio ID: 987654321
投稿時刻: 20:00
投稿するコメント: 今日の更新です！
```

## 注意

- 自動投稿中はPCとBotを起動したままにしてください。
- PCがスリープ・シャットダウン中は投稿できません。
- Scratchパスワードはファイル保存しません。
- Scratch側でコメントが無効な投稿先には投稿できません。
- Scratch側の投稿制限やミュートは回避しません。
