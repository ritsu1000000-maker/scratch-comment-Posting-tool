from getpass import getpass
from pathlib import Path
from datetime import datetime
import json
import time
import scratchattach as scratch3

SCHEDULE_FILE = Path("schedules.json")
STATE_FILE = Path("schedule_state.json")
MIN_POST_INTERVAL = 10


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def login():
    print("\nScratchへログインします。")
    username = input("Scratchユーザー名: ").strip()
    password = getpass("Scratchパスワード: ")

    if not username or not password:
        print("ユーザー名またはパスワードが空です。")
        return None

    try:
        print("ログイン中...")
        session = scratch3.login(username, password)
        print(f"ログイン成功: {session.username}")
        return session
    except Exception as e:
        print(f"ログイン失敗: {e}")
        return None


def ask_numeric_id(label):
    while True:
        raw = input(f"{label}: ").strip()
        if raw.isdigit():
            return int(raw)
        print("数字のIDを入力してください。")


def ask_target():
    while True:
        print("\n投稿先を選択")
        print("1. Project")
        print("2. Studio")
        choice = input("選択 > ").strip()

        if choice == "1":
            return "project", ask_numeric_id("プロジェクトID")
        if choice == "2":
            return "studio", ask_numeric_id("Studio ID")

        print("1 または 2 を選んでください。")


def target_label(target_type):
    return "Project" if target_type == "project" else "Studio"


def connect_target(session, target_type, target_id):
    if target_type == "studio":
        return session.connect_studio(target_id)
    return session.connect_project(target_id)


def ask_time():
    while True:
        raw = input("投稿時刻 (HH:MM 例 18:30): ").strip()
        try:
            datetime.strptime(raw, "%H:%M")
            return raw
        except ValueError:
            print("HH:MM形式で入力してください。例: 07:05 / 18:30")


def normalize_schedule(item):
    # v2以前の予約はProject予約としてそのまま利用可能
    if "target_type" not in item:
        item["target_type"] = "project"
    if "target_id" not in item:
        item["target_id"] = item.get("project_id")
    return item


def load_schedules():
    schedules = load_json(SCHEDULE_FILE, [])
    changed = False

    for item in schedules:
        before = dict(item)
        normalize_schedule(item)
        if item != before:
            changed = True

    if changed:
        save_json(SCHEDULE_FILE, schedules)

    return schedules


def add_schedule():
    schedules = load_schedules()

    target_type, target_id = ask_target()
    post_time = ask_time()
    message = input("投稿するコメント: ").strip()

    if not message:
        print("コメントが空です。")
        return

    # 同じ時刻でも投稿先が違えば登録可能
    duplicate = any(
        x["time"] == post_time
        and x.get("target_type", "project") == target_type
        and x.get("target_id", x.get("project_id")) == target_id
        and x.get("message") == message
        for x in schedules
    )
    if duplicate:
        print("同じ内容の予約がすでにあります。")
        return

    new_id = max([x.get("id", 0) for x in schedules], default=0) + 1

    schedules.append({
        "id": new_id,
        "target_type": target_type,
        "target_id": target_id,
        "time": post_time,
        "message": message,
        "enabled": True
    })

    schedules.sort(key=lambda x: (x["time"], x["id"]))
    save_json(SCHEDULE_FILE, schedules)

    print("\n予約を追加しました。")
    print(f"予約ID: {new_id}")
    print(f"投稿先: {target_label(target_type)} {target_id}")
    print(f"毎日: {post_time}")
    print(f"コメント: {message}")


def list_schedules():
    schedules = load_schedules()

    if not schedules:
        print("\n予約はありません。")
        return

    print("\n--- 投稿予約 ---")
    for item in schedules:
        status = "ON" if item.get("enabled", True) else "OFF"
        target_type = item.get("target_type", "project")
        target_id = item.get("target_id", item.get("project_id"))

        print(
            f"[{item['id']}] {item['time']} | "
            f"{target_label(target_type)} {target_id} | {status}"
        )
        print(f"    {item['message']}")


def delete_schedule():
    schedules = load_schedules()
    if not schedules:
        print("予約はありません。")
        return

    list_schedules()

    raw = input("\n削除する予約ID: ").strip()
    if not raw.isdigit():
        print("数字で入力してください。")
        return

    schedule_id = int(raw)
    new_schedules = [x for x in schedules if x["id"] != schedule_id]

    if len(new_schedules) == len(schedules):
        print("そのIDは見つかりません。")
        return

    save_json(SCHEDULE_FILE, new_schedules)

    state = load_json(STATE_FILE, {})
    state.pop(str(schedule_id), None)
    save_json(STATE_FILE, state)

    print("削除しました。")


def toggle_schedule():
    schedules = load_schedules()
    if not schedules:
        print("予約はありません。")
        return

    list_schedules()
    raw = input("\nON/OFFを切り替える予約ID: ").strip()

    if not raw.isdigit():
        print("数字で入力してください。")
        return

    schedule_id = int(raw)

    for item in schedules:
        if item["id"] == schedule_id:
            item["enabled"] = not item.get("enabled", True)
            save_json(SCHEDULE_FILE, schedules)
            print("ONにしました。" if item["enabled"] else "OFFにしました。")
            return

    print("そのIDは見つかりません。")


def post_now():
    session = login()
    if not session:
        return

    target_type, target_id = ask_target()
    message = input("コメント: ").strip()

    if not message:
        print("コメントが空です。")
        return

    try:
        target = connect_target(session, target_type, target_id)
        comment = target.post_comment(message)
        comment_id = getattr(comment, "id", "不明")
        print(
            f"投稿成功！ {target_label(target_type)} {target_id} | "
            f"コメントID: {comment_id}"
        )
    except Exception as e:
        print(f"投稿失敗: {e}")


def run_scheduler():
    schedules = load_schedules()
    enabled = [x for x in schedules if x.get("enabled", True)]

    if not enabled:
        print("有効な予約がありません。先に予約を追加してください。")
        return

    session = login()
    if not session:
        return

    state = load_json(STATE_FILE, {})
    target_cache = {}
    last_post_timestamp = 0.0

    print("\n自動投稿を開始しました。")
    print("この画面を開いたままにしてください。")
    print("Ctrl+C で停止できます。\n")
    list_schedules()

    try:
        while True:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            schedules = load_schedules()

            for item in schedules:
                if not item.get("enabled", True):
                    continue

                sid = str(item["id"])
                target_type = item.get("target_type", "project")
                target_id = item.get("target_id", item.get("project_id"))

                if item["time"] != current_time:
                    continue

                if state.get(sid) == today:
                    continue

                elapsed = time.time() - last_post_timestamp
                if last_post_timestamp and elapsed < MIN_POST_INTERVAL:
                    continue

                cache_key = (target_type, target_id)

                try:
                    if cache_key not in target_cache:
                        target_cache[cache_key] = connect_target(
                            session, target_type, target_id
                        )

                    target = target_cache[cache_key]
                    comment = target.post_comment(item["message"])
                    comment_id = getattr(comment, "id", "不明")

                    state[sid] = today
                    save_json(STATE_FILE, state)
                    last_post_timestamp = time.time()

                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿成功 | 予約ID {item['id']} | "
                        f"{target_label(target_type)} {target_id} | "
                        f"コメントID {comment_id}"
                    )

                except Exception as e:
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿失敗 | 予約ID {item['id']} | "
                        f"{target_label(target_type)} {target_id} | {e}"
                    )

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n自動投稿を停止しました。")


def menu():
    while True:
        print("\n" + "=" * 50)
        print(" Scratch Comment Bot v3")
        print("=" * 50)
        print("1. 毎日の投稿予約を追加")
        print("2. 投稿予約一覧")
        print("3. 投稿予約を削除")
        print("4. 投稿予約をON/OFF")
        print("5. 自動投稿を開始")
        print("6. 今すぐ1件投稿")
        print("0. 終了")

        choice = input("\n選択 > ").strip()

        if choice == "1":
            add_schedule()
        elif choice == "2":
            list_schedules()
        elif choice == "3":
            delete_schedule()
        elif choice == "4":
            toggle_schedule()
        elif choice == "5":
            run_scheduler()
        elif choice == "6":
            post_now()
        elif choice == "0":
            print("終了します。")
            break
        else:
            print("0〜6から選んでください。")


if __name__ == "__main__":
    menu()
