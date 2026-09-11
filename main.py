from getpass import getpass
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import json
import re
import time

import scratchattach as scratch3

APP_NAME = "Scratch Comment Bot v6 - Unlimited Reservations"
SCHEDULE_FILE = Path("schedules.json")
STATE_FILE = Path("schedule_state.json")
LOG_FILE = Path("post_log.jsonl")
MIN_POST_INTERVAL = 15
MAX_COMMENT_LENGTH = 500
MAX_SCHEDULES = None  # None = 予約件数にアプリ側の固定上限なし


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


def append_log(status, target_type, target_value, message, detail="", schedule_id=None):
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "target_type": target_type,
        "target": str(target_value),
        "message": message,
        "detail": str(detail),
        "schedule_id": schedule_id,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


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


def target_label(target_type):
    return {
        "project": "Project",
        "studio": "Studio",
        "profile": "Profile",
    }.get(target_type, target_type)


def normalize_target_value(target_type, value):
    if target_type in ("project", "studio"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return str(value).strip()


def parse_scratch_target(raw, expected_type=None):
    """ID / username / Scratch URL を投稿先に変換する。"""
    raw = raw.strip()
    if not raw:
        raise ValueError("投稿先が空です。")

    # URLでなくても /projects/123 のような入力を許可
    text = raw
    try:
        parsed = urlparse(raw if "://" in raw else "https://scratch.mit.edu" + (raw if raw.startswith("/") else "/" + raw))
        path = parsed.path
    except Exception:
        path = raw

    m = re.search(r"/projects/(\d+)", path, re.I)
    if m:
        result = ("project", int(m.group(1)))
    else:
        m = re.search(r"/studios/(\d+)", path, re.I)
        if m:
            result = ("studio", int(m.group(1)))
        else:
            m = re.search(r"/users/([^/?#]+)", path, re.I)
            if m:
                result = ("profile", m.group(1))
            elif expected_type in ("project", "studio") and raw.isdigit():
                result = (expected_type, int(raw))
            elif expected_type == "profile":
                username = raw.strip().strip("/")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", username):
                    raise ValueError("プロフィールはユーザー名か Scratch のプロフィールURLを入力してください。")
                result = ("profile", username)
            else:
                raise ValueError("ScratchのProject / Studio / Profileを判定できませんでした。")

    if expected_type and result[0] != expected_type:
        raise ValueError(f"選択した種類は {target_label(expected_type)} ですが、入力は {target_label(result[0])} に見えます。")
    return result


def ask_target():
    while True:
        print("\n投稿先を選択")
        print("1. Project")
        print("2. Studio")
        print("3. Profile")
        print("4. URLから自動判定")
        choice = input("選択 > ").strip()

        type_map = {"1": "project", "2": "studio", "3": "profile"}
        expected_type = type_map.get(choice)

        if choice not in ("1", "2", "3", "4"):
            print("1〜4を選んでください。")
            continue

        if expected_type == "project":
            prompt = "Project ID またはURL"
        elif expected_type == "studio":
            prompt = "Studio ID またはURL"
        elif expected_type == "profile":
            prompt = "Scratchユーザー名 またはプロフィールURL"
        else:
            prompt = "Scratch URL"

        raw = input(f"{prompt}: ").strip()
        try:
            return parse_scratch_target(raw, expected_type=expected_type)
        except ValueError as e:
            print(f"入力エラー: {e}")


def connect_target(session, target_type, target_value):
    if target_type == "studio":
        return session.connect_studio(int(target_value))
    if target_type == "profile":
        return session.connect_user(str(target_value))
    return session.connect_project(int(target_value))


def ask_time(label="投稿時刻"):
    while True:
        raw = input(f"{label} (HH:MM 例 18:30): ").strip()
        try:
            datetime.strptime(raw, "%H:%M")
            return raw
        except ValueError:
            print("HH:MM形式で入力してください。例: 07:05 / 18:30")


def ask_message(prompt="投稿するコメント"):
    message = input(f"{prompt}: ").strip()
    if not message:
        print("コメントが空です。")
        return None
    if len(message) > MAX_COMMENT_LENGTH:
        print(f"コメントが長すぎます。{MAX_COMMENT_LENGTH}文字以内にしてください（現在 {len(message)}文字）。")
        return None
    return message


def normalize_schedule(item):
    # 旧バージョンの予約データを v5 の「毎日・無期限」形式へ移行
    if "target_type" not in item:
        item["target_type"] = "project"
    if "target_value" not in item:
        item["target_value"] = item.get("target_id", item.get("project_id"))
    item["target_value"] = normalize_target_value(item["target_type"], item.get("target_value"))
    item["mode"] = "daily"
    item.pop("date", None)
    if "enabled" not in item:
        item["enabled"] = True
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


def schedule_sort_key(item):
    return (item.get("time", "99:99"), item.get("id", 0))


def format_schedule_when(item):
    return f"毎日・無期限 {item.get('time', '?')}"


def add_schedule():
    schedules = load_schedules()

    target_type, target_value = ask_target()
    print("\n予約方式: 毎日・無期限（終了日なし）")
    post_time = ask_time()
    message = ask_message()
    if not message:
        return

    duplicate = any(
        x.get("time") == post_time
        and x.get("target_type", "project") == target_type
        and str(x.get("target_value", x.get("target_id", x.get("project_id")))) == str(target_value)
        and x.get("message") == message
        for x in schedules
    )
    if duplicate:
        print("同じ内容の予約がすでにあります。")
        return

    new_id = max([x.get("id", 0) for x in schedules], default=0) + 1
    item = {
        "id": new_id,
        "target_type": target_type,
        "target_value": target_value,
        "mode": "daily",
        "time": post_time,
        "message": message,
        "enabled": True,
    }

    schedules.append(item)
    schedules.sort(key=schedule_sort_key)
    save_json(SCHEDULE_FILE, schedules)

    print("\n毎日・無期限予約を追加しました。")
    print(f"予約件数: {len(schedules)} / 上限なし")
    print(f"予約ID: {new_id}")
    print(f"投稿先: {target_label(target_type)} {target_value}")
    print(f"日時: {format_schedule_when(item)}")
    print(f"コメント: {message}")
    print("この予約はOFFまたは削除するまで毎日続きます。")


def list_schedules():
    schedules = load_schedules()

    if not schedules:
        print("\n予約はありません。")
        return

    print("\n--- 毎日・無期限予約 ---")
    print(f"予約件数: {len(schedules)} / 上限なし")
    for item in schedules:
        status = "ON" if item.get("enabled", True) else "OFF"
        target_type = item.get("target_type", "project")
        target_value = item.get("target_value", item.get("target_id", item.get("project_id")))
        print(
            f"[{item['id']}] {format_schedule_when(item)} | "
            f"{target_label(target_type)} {target_value} | {status}"
        )
        print(f"    {item['message']}")


def choose_schedule_id(action):
    schedules = load_schedules()
    if not schedules:
        print("予約はありません。")
        return None, schedules

    list_schedules()
    raw = input(f"\n{action}予約ID: ").strip()
    if not raw.isdigit():
        print("数字で入力してください。")
        return None, schedules
    return int(raw), schedules


def delete_schedule():
    schedule_id, schedules = choose_schedule_id("削除する")
    if schedule_id is None:
        return

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
    schedule_id, schedules = choose_schedule_id("ON/OFFを切り替える")
    if schedule_id is None:
        return

    for item in schedules:
        if item["id"] == schedule_id:
            item["enabled"] = not item.get("enabled", True)
            save_json(SCHEDULE_FILE, schedules)
            print("ONにしました。毎日予約を再開します。" if item["enabled"] else "OFFにしました。毎日予約を停止します。")
            return
    print("そのIDは見つかりません。")


def edit_schedule():
    schedule_id, schedules = choose_schedule_id("編集する")
    if schedule_id is None:
        return

    item = next((x for x in schedules if x["id"] == schedule_id), None)
    if not item:
        print("そのIDは見つかりません。")
        return

    print("\n変更する項目")
    print("1. 投稿先")
    print("2. 投稿時刻")
    print("3. コメント")
    print("4. 全部")
    choice = input("選択 > ").strip()

    if choice in ("1", "4"):
        item["target_type"], item["target_value"] = ask_target()

    if choice in ("2", "4"):
        item["time"] = ask_time()

    if choice in ("3", "4"):
        message = ask_message("新しいコメント")
        if not message:
            return
        item["message"] = message

    if choice not in ("1", "2", "3", "4"):
        print("変更しませんでした。")
        return

    item["mode"] = "daily"
    item.pop("date", None)
    schedules.sort(key=schedule_sort_key)
    save_json(SCHEDULE_FILE, schedules)
    state = load_json(STATE_FILE, {})
    state.pop(str(schedule_id), None)
    save_json(STATE_FILE, state)
    print("毎日・無期限予約を更新しました。")


def post_comment(session, target_type, target_value, message):
    target = connect_target(session, target_type, target_value)
    comment = target.post_comment(message)
    return getattr(comment, "id", "不明")


def post_now():
    session = login()
    if not session:
        return

    target_type, target_value = ask_target()
    message = ask_message("コメント")
    if not message:
        return

    print(f"\n投稿先: {target_label(target_type)} {target_value}")
    print(f"内容: {message}")
    confirm = input("投稿しますか？ (y/N): ").strip().lower()
    if confirm != "y":
        print("キャンセルしました。")
        return

    try:
        comment_id = post_comment(session, target_type, target_value, message)
        append_log("success", target_type, target_value, message, f"comment_id={comment_id}")
        print(f"投稿成功！ コメントID: {comment_id}")
    except Exception as e:
        append_log("failed", target_type, target_value, message, str(e))
        print(f"投稿失敗: {e}")


def check_target():
    session = login()
    if not session:
        return
    target_type, target_value = ask_target()
    try:
        target = connect_target(session, target_type, target_value)
        # 接続オブジェクトが作れることを確認。ネットワーク取得が可能なら update() も試す。
        if hasattr(target, "update"):
            try:
                target.update()
            except Exception:
                pass
        print(f"接続OK: {target_label(target_type)} {target_value}")
    except Exception as e:
        print(f"接続失敗: {e}")


def show_logs():
    if not LOG_FILE.exists():
        print("投稿履歴はまだありません。")
        return

    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"履歴を読めませんでした: {e}")
        return

    if not lines:
        print("投稿履歴はまだありません。")
        return

    print("\n--- 最近の投稿履歴（最大30件） ---")
    for line in lines[-30:]:
        try:
            x = json.loads(line)
            print(
                f"{x.get('timestamp', '?')} | {x.get('status', '?')} | "
                f"{target_label(x.get('target_type', '?'))} {x.get('target', '?')}"
            )
            print(f"    {x.get('message', '')}")
            if x.get("detail"):
                print(f"    {x.get('detail')}")
        except Exception:
            print(line)


def schedule_is_due(item, now):
    return item.get("time") == now.strftime("%H:%M")


def mark_schedule_done(state, item, now):
    # その日の投稿が終わったことだけ記録。翌日になると再び実行可能。
    state[str(item["id"])] = now.strftime("%Y-%m-%d")


def schedule_already_done(state, item, now):
    return state.get(str(item["id"])) == now.strftime("%Y-%m-%d")


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

    print("\n毎日・無期限の自動投稿を開始しました。")
    print(f"投稿間隔は最低 {MIN_POST_INTERVAL} 秒です。")
    print("この画面を開いたままにしてください。Ctrl+C で停止できます。\n")
    list_schedules()

    try:
        while True:
            now = datetime.now()
            schedules = load_schedules()

            for item in schedules:
                if not item.get("enabled", True):
                    continue
                if not schedule_is_due(item, now):
                    continue
                if schedule_already_done(state, item, now):
                    continue

                elapsed = time.time() - last_post_timestamp
                if last_post_timestamp and elapsed < MIN_POST_INTERVAL:
                    continue

                target_type = item.get("target_type", "project")
                target_value = item.get("target_value", item.get("target_id", item.get("project_id")))
                cache_key = (target_type, str(target_value))

                try:
                    if cache_key not in target_cache:
                        target_cache[cache_key] = connect_target(session, target_type, target_value)

                    target = target_cache[cache_key]
                    comment = target.post_comment(item["message"])
                    comment_id = getattr(comment, "id", "不明")

                    mark_schedule_done(state, item, now)
                    save_json(STATE_FILE, state)
                    last_post_timestamp = time.time()

                    append_log(
                        "success", target_type, target_value, item["message"],
                        f"comment_id={comment_id}", item.get("id")
                    )
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿成功 | 予約ID {item['id']} | "
                        f"{target_label(target_type)} {target_value} | "
                        f"コメントID {comment_id}"
                    )

                except Exception as e:
                    append_log(
                        "failed", target_type, target_value, item["message"],
                        str(e), item.get("id")
                    )
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿失敗 | 予約ID {item['id']} | "
                        f"{target_label(target_type)} {target_value} | {e}"
                    )

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n自動投稿を停止しました。")


def menu():
    while True:
        print("\n" + "=" * 56)
        print(f" {APP_NAME}")
        print("=" * 56)
        print("1. 毎日・無期限予約を追加（件数上限なし）")
        print("2. 投稿予約一覧")
        print("3. 投稿予約を編集")
        print("4. 投稿予約を削除")
        print("5. 投稿予約をON/OFF")
        print("6. 自動投稿を開始")
        print("7. 今すぐ1件投稿")
        print("8. 投稿先の接続確認")
        print("9. 投稿履歴を見る")
        print("0. 終了")

        choice = input("\n選択 > ").strip()

        if choice == "1":
            add_schedule()
        elif choice == "2":
            list_schedules()
        elif choice == "3":
            edit_schedule()
        elif choice == "4":
            delete_schedule()
        elif choice == "5":
            toggle_schedule()
        elif choice == "6":
            run_scheduler()
        elif choice == "7":
            post_now()
        elif choice == "8":
            check_target()
        elif choice == "9":
            show_logs()
        elif choice == "0":
            print("終了します。")
            break
        else:
            print("0〜9から選んでください。")


if __name__ == "__main__":
    menu()
