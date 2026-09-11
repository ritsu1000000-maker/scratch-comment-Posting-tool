from getpass import getpass
from datetime import datetime
import json
import time

import scratchattach as scratch3
import main as core

APP_NAME = "Scratch Comment Bot v7 - Multi Account"
SESSIONS = {}


def _session_key(username):
    return str(username).strip().lower()


def find_session(username):
    key = _session_key(username)
    for saved_name, session in SESSIONS.items():
        if _session_key(saved_name) == key:
            return saved_name, session
    return None, None


def login_username(username=None):
    if username is None:
        print("\nScratchへログインします。")
        username = input("Scratchユーザー名: ").strip()
    else:
        username = str(username).strip()
        print(f"\nScratchへログインします: {username}")

    if not username:
        print("ユーザー名が空です。")
        return None

    existing_name, existing = find_session(username)
    if existing is not None:
        print(f"すでにログイン済みです: {existing_name}")
        return existing

    password = getpass("Scratchパスワード: ")
    if not password:
        print("パスワードが空です。")
        return None

    try:
        print("ログイン中...")
        session = scratch3.login(username, password)
        canonical = getattr(session, "username", username)
        SESSIONS[canonical] = session
        print(f"ログイン成功: {canonical}")
        return session
    except Exception as e:
        print(f"ログイン失敗: {e}")
        return None


def login_multiple():
    print("\n--- 複数アカウントを連続ログイン ---")
    print("ユーザー名を空欄でEnterすると終了します。")
    while True:
        username = input("Scratchユーザー名: ").strip()
        if not username:
            break
        login_username(username)
    print(f"現在のログイン数: {len(SESSIONS)}")


def list_accounts():
    print("\n--- ログイン中のアカウント ---")
    if not SESSIONS:
        print("ログイン中のアカウントはありません。")
        return []
    names = list(SESSIONS.keys())
    for i, name in enumerate(names, 1):
        print(f"{i}. {name}")
    print(f"合計: {len(names)}")
    return names


def logout_account():
    names = list_accounts()
    if not names:
        return
    raw = input("ログアウトする番号: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(names)):
        print("番号が正しくありません。")
        return
    name = names[int(raw) - 1]
    SESSIONS.pop(name, None)
    print(f"ログアウトしました: {name}")


def account_manager():
    while True:
        print("\n--- アカウント管理 ---")
        print("1. アカウントを1つ追加ログイン")
        print("2. 複数アカウントを連続ログイン")
        print("3. ログイン中アカウント一覧")
        print("4. 1アカウントをログアウト")
        print("0. 戻る")
        choice = input("選択 > ").strip()

        if choice == "1":
            login_username()
        elif choice == "2":
            login_multiple()
        elif choice == "3":
            list_accounts()
        elif choice == "4":
            logout_account()
        elif choice == "0":
            return
        else:
            print("0〜4から選んでください。")


def choose_account(prompt="使用するアカウント"):
    if not SESSIONS:
        print("\n先にScratchへログインしてください。")
        if login_username() is None:
            return None, None

    while True:
        names = list(SESSIONS.keys())
        print(f"\n--- {prompt} ---")
        for i, name in enumerate(names, 1):
            print(f"{i}. {name}")
        print("L. 別アカウントを追加ログイン")
        raw = input("選択 > ").strip()

        if raw.lower() == "l":
            login_username()
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            name = names[int(raw) - 1]
            return name, SESSIONS[name]
        print("一覧の番号、または L を入力してください。")


def normalize_schedule(item):
    core.normalize_schedule(item)
    if "account_username" not in item:
        item["account_username"] = None
    return item


def load_schedules():
    schedules = core.load_json(core.SCHEDULE_FILE, [])
    changed = False
    for item in schedules:
        before = dict(item)
        normalize_schedule(item)
        if item != before:
            changed = True
    if changed:
        core.save_json(core.SCHEDULE_FILE, schedules)
    return schedules


def schedule_sort_key(item):
    return core.schedule_sort_key(item)


def format_schedule_when(item):
    return core.format_schedule_when(item)


def add_schedule():
    schedules = load_schedules()
    account_username, _session = choose_account("この予約で使うアカウント")
    if not account_username:
        return

    target_type, target_value = core.ask_target()
    print("\n予約方式: 毎日・無期限（終了日なし）")
    post_time = core.ask_time()
    message = core.ask_message()
    if not message:
        return

    duplicate = any(
        x.get("time") == post_time
        and x.get("target_type", "project") == target_type
        and str(x.get("target_value", x.get("target_id", x.get("project_id")))) == str(target_value)
        and x.get("message") == message
        and _session_key(x.get("account_username", "")) == _session_key(account_username)
        for x in schedules
    )
    if duplicate:
        print("同じアカウント・投稿先・時刻・内容の予約がすでにあります。")
        return

    new_id = max([x.get("id", 0) for x in schedules], default=0) + 1
    item = {
        "id": new_id,
        "account_username": account_username,
        "target_type": target_type,
        "target_value": target_value,
        "mode": "daily",
        "time": post_time,
        "message": message,
        "enabled": True,
    }
    schedules.append(item)
    schedules.sort(key=schedule_sort_key)
    core.save_json(core.SCHEDULE_FILE, schedules)

    print("\n毎日・無期限予約を追加しました。")
    print(f"予約ID: {new_id}")
    print(f"使用アカウント: {account_username}")
    print(f"投稿先: {core.target_label(target_type)} {target_value}")
    print(f"日時: {format_schedule_when(item)}")
    print(f"コメント: {message}")


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
        account = item.get("account_username") or "未指定"
        print(
            f"[{item['id']}] {format_schedule_when(item)} | "
            f"@{account} | {core.target_label(target_type)} {target_value} | {status}"
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
    core.save_json(core.SCHEDULE_FILE, new_schedules)
    state = core.load_json(core.STATE_FILE, {})
    state.pop(str(schedule_id), None)
    core.save_json(core.STATE_FILE, state)
    print("削除しました。")


def toggle_schedule():
    schedule_id, schedules = choose_schedule_id("ON/OFFを切り替える")
    if schedule_id is None:
        return
    for item in schedules:
        if item["id"] == schedule_id:
            item["enabled"] = not item.get("enabled", True)
            core.save_json(core.SCHEDULE_FILE, schedules)
            print("ONにしました。" if item["enabled"] else "OFFにしました。")
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
    print("1. 使用アカウント")
    print("2. 投稿先")
    print("3. 投稿時刻")
    print("4. コメント")
    print("5. 全部")
    choice = input("選択 > ").strip()

    if choice in ("1", "5"):
        account_username, _session = choose_account("この予約で使うアカウント")
        if not account_username:
            return
        item["account_username"] = account_username
    if choice in ("2", "5"):
        item["target_type"], item["target_value"] = core.ask_target()
    if choice in ("3", "5"):
        item["time"] = core.ask_time()
    if choice in ("4", "5"):
        message = core.ask_message("新しいコメント")
        if not message:
            return
        item["message"] = message
    if choice not in ("1", "2", "3", "4", "5"):
        print("変更しませんでした。")
        return

    item["mode"] = "daily"
    item.pop("date", None)
    schedules.sort(key=schedule_sort_key)
    core.save_json(core.SCHEDULE_FILE, schedules)
    state = core.load_json(core.STATE_FILE, {})
    state.pop(str(schedule_id), None)
    core.save_json(core.STATE_FILE, state)
    print("予約を更新しました。")


def post_now():
    account_username, session = choose_account("投稿に使うアカウント")
    if session is None:
        return
    target_type, target_value = core.ask_target()
    message = core.ask_message("コメント")
    if not message:
        return

    print(f"\n使用アカウント: {account_username}")
    print(f"投稿先: {core.target_label(target_type)} {target_value}")
    print(f"内容: {message}")
    if input("投稿しますか？ (y/N): ").strip().lower() != "y":
        print("キャンセルしました。")
        return

    try:
        comment_id = core.post_comment(session, target_type, target_value, message)
        core.append_log(
            "success", target_type, target_value, message,
            f"account={account_username}; comment_id={comment_id}"
        )
        print(f"投稿成功！ @{account_username} / コメントID: {comment_id}")
    except Exception as e:
        core.append_log(
            "failed", target_type, target_value, message,
            f"account={account_username}; {e}"
        )
        print(f"投稿失敗: {e}")


def check_target():
    account_username, session = choose_account("接続確認に使うアカウント")
    if session is None:
        return
    target_type, target_value = core.ask_target()
    try:
        target = core.connect_target(session, target_type, target_value)
        if hasattr(target, "update"):
            try:
                target.update()
            except Exception:
                pass
        print(f"接続OK: @{account_username} -> {core.target_label(target_type)} {target_value}")
    except Exception as e:
        print(f"接続失敗: {e}")


def assign_legacy_accounts(schedules):
    changed = False
    for item in schedules:
        if item.get("account_username"):
            continue
        print(f"\n予約ID {item.get('id')} は旧形式で、使用アカウントが未指定です。")
        name, _session = choose_account("この予約に割り当てるアカウント")
        if not name:
            return False
        item["account_username"] = name
        changed = True
    if changed:
        core.save_json(core.SCHEDULE_FILE, schedules)
    return True


def ensure_required_sessions(schedules):
    required = []
    for item in schedules:
        if not item.get("enabled", True):
            continue
        name = item.get("account_username")
        if name and all(_session_key(name) != _session_key(x) for x in required):
            required.append(name)

    for username in required:
        _name, session = find_session(username)
        if session is not None:
            continue
        print(f"\n予約で必要なアカウント @{username} が未ログインです。")
        session = login_username(username)
        if session is None:
            print("必要なアカウントへログインできないため、自動投稿を開始しません。")
            return False
    return True


def run_scheduler():
    schedules = load_schedules()
    enabled = [x for x in schedules if x.get("enabled", True)]
    if not enabled:
        print("有効な予約がありません。先に予約を追加してください。")
        return

    if not assign_legacy_accounts(schedules):
        return
    enabled = [x for x in schedules if x.get("enabled", True)]
    if not ensure_required_sessions(enabled):
        return

    state = core.load_json(core.STATE_FILE, {})
    target_cache = {}
    last_post_timestamp = 0.0

    print("\n複数アカウント対応の自動投稿を開始しました。")
    print(f"全アカウント共通で投稿間隔は最低 {core.MIN_POST_INTERVAL} 秒です。")
    print("Ctrl+C で停止できます。\n")
    list_schedules()

    try:
        while True:
            now = datetime.now()
            schedules = load_schedules()

            for item in schedules:
                if not item.get("enabled", True):
                    continue
                if not core.schedule_is_due(item, now):
                    continue
                if core.schedule_already_done(state, item, now):
                    continue

                elapsed = time.time() - last_post_timestamp
                if last_post_timestamp and elapsed < core.MIN_POST_INTERVAL:
                    continue

                account_username = item.get("account_username")
                saved_name, session = find_session(account_username)
                if session is None:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] @{account_username} は未ログインのため予約ID {item['id']} を保留")
                    continue

                target_type = item.get("target_type", "project")
                target_value = item.get("target_value", item.get("target_id", item.get("project_id")))
                cache_key = (_session_key(saved_name), target_type, str(target_value))

                try:
                    if cache_key not in target_cache:
                        target_cache[cache_key] = core.connect_target(session, target_type, target_value)
                    target = target_cache[cache_key]
                    comment = target.post_comment(item["message"])
                    comment_id = getattr(comment, "id", "不明")

                    core.mark_schedule_done(state, item, now)
                    core.save_json(core.STATE_FILE, state)
                    last_post_timestamp = time.time()
                    core.append_log(
                        "success", target_type, target_value, item["message"],
                        f"account={saved_name}; comment_id={comment_id}", item.get("id")
                    )
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿成功 | @{saved_name} | 予約ID {item['id']} | "
                        f"{core.target_label(target_type)} {target_value} | コメントID {comment_id}"
                    )
                except Exception as e:
                    core.append_log(
                        "failed", target_type, target_value, item["message"],
                        f"account={account_username}; {e}", item.get("id")
                    )
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{stamp}] 投稿失敗 | @{account_username} | 予約ID {item['id']} | "
                        f"{core.target_label(target_type)} {target_value} | {e}"
                    )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n自動投稿を停止しました。")


def show_logs():
    if not core.LOG_FILE.exists():
        print("投稿履歴はまだありません。")
        return
    try:
        lines = core.LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"履歴を読めませんでした: {e}")
        return
    print("\n--- 最近の投稿履歴（最大30件） ---")
    for line in lines[-30:]:
        try:
            x = json.loads(line)
            print(
                f"{x.get('timestamp', '?')} | {x.get('status', '?')} | "
                f"{core.target_label(x.get('target_type', '?'))} {x.get('target', '?')}"
            )
            print(f"    {x.get('message', '')}")
            if x.get("detail"):
                print(f"    {x.get('detail')}")
        except Exception:
            print(line)


def menu():
    while True:
        print("\n" + "=" * 62)
        print(f" {APP_NAME}")
        print(f" ログイン中: {len(SESSIONS)} アカウント")
        print("=" * 62)
        print("A. アカウント管理（複数ログイン）")
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

        choice = input("\n選択 > ").strip().lower()
        if choice == "a":
            account_manager()
        elif choice == "1":
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
            print("A または 0〜9 から選んでください。")


if __name__ == "__main__":
    menu()
