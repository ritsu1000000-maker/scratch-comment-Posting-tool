from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_FILE = BASE_DIR / "schedules.json"
REACTION_FILE = BASE_DIR / "reactions.json"
REACTION_STATE_FILE = BASE_DIR / "reaction_state.json"
MAX_COMMENT_LENGTH = 500
MAX_QUEUE_ITEMS = 200
MAX_REACTION_RULES = 50
MAX_TRACKED_COMMENTS = 2000
MAX_REPLY_CANDIDATES = 50
DEFAULT_MAX_REPLIES = 1
MIN_POST_INTERVAL = 15
MAX_STUDIO_ITEMS = 100
MAX_STUDIO_BULK_PROJECTS = 1000
STUDIO_ACTION_INTERVAL = 3
JST = ZoneInfo("Asia/Tokyo")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

# セッションはメモリにだけ保持する。パスワードは保存しない。
sessions: dict[str, Any] = {}
sessions_lock = threading.RLock()
post_lock = threading.Lock()
last_post_at = 0.0
studio_action_lock = threading.Lock()
last_studio_action_at = 0.0
studio_bulk_state_lock = threading.RLock()
studio_bulk_job: dict[str, Any] | None = None
studio_bulk_stop_event: threading.Event | None = None
studio_bulk_thread: threading.Thread | None = None

queue_items: list[dict[str, Any]] = []
queue_lock = threading.RLock()
schedules_lock = threading.RLock()
queue_running = False
queue_batch_started_at: float | None = None

reaction_rules: list[dict[str, Any]] = []
reaction_state: dict[str, Any] = {}
reaction_lock = threading.RLock()
reaction_running = False
reaction_stop_event = threading.Event()
reaction_thread: threading.Thread | None = None
reaction_due_at: dict[str, float] = {}


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def clean_message(value: Any) -> str:
    message = str(value or "").strip()
    if not message:
        raise ValueError("コメントを入力してください。")
    if len(message) > MAX_COMMENT_LENGTH:
        raise ValueError(f"コメントは{MAX_COMMENT_LENGTH}文字以内にしてください。")
    return message


def clean_template(value: Any) -> str:
    """返信テンプレートもScratchコメントと同じ長さ制限にする。"""
    return clean_message(value)


def normalize_target(target_type: Any, raw_value: Any) -> tuple[str, str | int]:
    target_type = str(target_type or "").strip().lower()
    raw = str(raw_value or "").strip()
    if target_type not in {"project", "studio", "profile"}:
        raise ValueError("投稿先の種類が不正です。")
    if not raw:
        raise ValueError("投稿先を入力してください。")

    path = raw
    if "://" in raw:
        path = urlparse(raw).path
    project_match = re.search(r"/projects/(\d+)", path, re.IGNORECASE)
    studio_match = re.search(r"/studios/(\d+)", path, re.IGNORECASE)
    user_match = re.search(r"/users/([A-Za-z0-9_-]{1,30})", path, re.IGNORECASE)

    if target_type == "project":
        value = project_match.group(1) if project_match else raw
        if not value.isdigit():
            raise ValueError("Projectは数字のIDまたはProject URLを指定してください。")
        return target_type, int(value)
    if target_type == "studio":
        value = studio_match.group(1) if studio_match else raw
        if not value.isdigit():
            raise ValueError("Studioは数字のIDまたはStudio URLを指定してください。")
        return target_type, int(value)

    value = user_match.group(1) if user_match else raw.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", value):
        raise ValueError("ProfileはScratchユーザー名またはProfile URLを指定してください。")
    return target_type, value


def selected_usernames(account_value: Any) -> list[str]:
    account = str(account_value or "").strip()
    with sessions_lock:
        available = list(sessions.keys())
    if account == "*":
        if not available:
            raise ValueError("先にScratchへログインしてください。")
        return available
    if not account:
        raise ValueError("使用するScratchアカウントを選択してください。")
    if account not in available:
        raise ValueError("選択したScratchアカウントはログインされていません。")
    return [account]


def selected_single_username(account_value: Any) -> str:
    account = str(account_value or "").strip()
    if account == "*":
        raise ValueError("コメント反応Botは返信元アカウントを1つ選択してください。")
    selected_usernames(account)
    return account


def normalize_studio_id(raw_value: Any) -> int:
    """受け取ったStudio IDまたはStudio URLを数値IDに正規化する。"""
    raw = str(raw_value or "").strip()
    if not raw:
        raise ValueError("スタジオIDまたはStudio URLを入力してください。")
    path = raw
    if "://" in raw:
        path = urlparse(raw).path
    match = re.search(r"/studios/(\d+)", path, re.IGNORECASE)
    value = match.group(1) if match else raw.strip("/")
    if not value.isdigit():
        raise ValueError("Studioは数字のIDまたはStudio URLを指定してください。")
    return int(value)


def owned_studio(username: str, raw_studio_id: Any):
    """ログイン中アカウントが所有するStudioだけを返す。失敗時は拒否する。"""
    with sessions_lock:
        session = sessions.get(username)
    if session is None:
        raise ValueError("先にScratchへログインしてください。")

    studio_id = normalize_studio_id(raw_studio_id)
    studio = session.connect_studio(studio_id)
    # connect_studio() は現在のStudio情報を取得するが、所有者判定前に更新を明示する。
    updated = studio.update()
    if updated == "429":
        raise RuntimeError("Scratch側で一時的に制限されています。時間を置いて再試行してください。")
    if not updated:
        raise ValueError("スタジオ情報を取得できませんでした。")
    owner_id = getattr(studio, "host_id", None)
    linked_user = session.connect_linked_user()
    logged_in_id = getattr(linked_user, "id", None)
    if owner_id is None or logged_in_id is None or str(owner_id) != str(logged_in_id):
        raise PermissionError("ログイン中アカウントが所有するスタジオだけ利用できます。")
    return session, studio


def studio_item_text(value: Any) -> str:
    return str(value or "").strip()


def ensure_studio_mutation_success(response: Any) -> None:
    """scratchattachがJSONで返すエラーを見逃さない。"""
    if not isinstance(response, dict):
        return
    code = response.get("code")
    if code in (None, "", 200, "200", "OK", "ok", "Success", "success"):
        return
    message = response.get("message") or code
    raise RuntimeError(f"Scratch側の操作に失敗しました: {message}")


def studio_snapshot(username: str, studio: Any) -> dict[str, Any]:
    """Studioの現在状態を表示用データに変換する。"""
    projects = studio.projects(limit=MAX_STUDIO_ITEMS, offset=0)
    comments = studio.comments(limit=MAX_STUDIO_ITEMS, offset=0)
    curators = studio.curators(limit=MAX_STUDIO_ITEMS, offset=0)
    managers = studio.managers(limit=MAX_STUDIO_ITEMS, offset=0)
    return {
        "id": str(getattr(studio, "id", "")),
        "title": studio_item_text(getattr(studio, "title", "")),
        "owner": username,
        "projects": [
            {
                "id": str(getattr(item, "id", "")),
                "title": studio_item_text(getattr(item, "title", "")) or "（タイトル不明）",
                "author": studio_item_text(getattr(item, "author_name", "")),
            }
            for item in projects
        ],
        "comments": [
            {
                "id": str(getattr(item, "id", "")),
                "author": studio_item_text(getattr(item, "author_name", "")),
                "content": studio_item_text(getattr(item, "content", "")),
                "created": studio_item_text(getattr(item, "datetime_created", "")),
            }
            for item in comments
        ],
        "curators": [
            {"username": studio_item_text(getattr(item, "username", ""))}
            for item in curators
            if studio_item_text(getattr(item, "username", ""))
        ],
        "managers": [
            {"username": studio_item_text(getattr(item, "username", ""))}
            for item in managers
            if studio_item_text(getattr(item, "username", ""))
        ],
        "limits": {
            "projects": len(projects) >= MAX_STUDIO_ITEMS,
            "comments": len(comments) >= MAX_STUDIO_ITEMS,
            "curators": len(curators) >= MAX_STUDIO_ITEMS,
            "managers": len(managers) >= MAX_STUDIO_ITEMS,
        },
    }


def wait_for_studio_action(stop_event: threading.Event | None = None) -> bool:
    """整理操作を直列化し、短時間の連続リクエストを抑える。"""
    global last_studio_action_at
    with studio_action_lock:
        wait_for = STUDIO_ACTION_INTERVAL - (time.monotonic() - last_studio_action_at)
        while wait_for > 0:
            if stop_event is not None and stop_event.is_set():
                return False
            time.sleep(min(0.25, wait_for))
            wait_for = STUDIO_ACTION_INTERVAL - (time.monotonic() - last_studio_action_at)
        if stop_event is not None and stop_event.is_set():
            return False
        last_studio_action_at = time.monotonic()
        return True


def studio_action_members(studio: Any, kind: str) -> list[Any]:
    if kind == "projects":
        return list(studio.projects(limit=MAX_STUDIO_ITEMS, offset=0) or [])
    if kind == "comments":
        return list(studio.comments(limit=MAX_STUDIO_ITEMS, offset=0) or [])
    if kind == "curators":
        return list(studio.curators(limit=MAX_STUDIO_ITEMS, offset=0) or [])
    if kind == "managers":
        return list(studio.managers(limit=MAX_STUDIO_ITEMS, offset=0) or [])
    raise ValueError("整理対象が不正です。")


def studio_bulk_members(studio: Any, kind: str) -> list[Any]:
    """一括整理用の一覧を取得する。プロジェクトだけは広めの上限で読む。"""
    if kind == "projects":
        return list(studio.projects(limit=MAX_STUDIO_BULK_PROJECTS, offset=0) or [])
    return studio_action_members(studio, kind)


def studio_bulk_targets(username: str, studio: Any, kind: str) -> list[str]:
    """一括整理の対象ID/ユーザー名を重複なく作る。所有者自身は除外する。"""
    if kind not in {"projects", "comments", "curators", "managers"}:
        raise ValueError("一括整理対象が不正です。")
    targets: list[str] = []
    seen: set[str] = set()
    for item in studio_bulk_members(studio, kind):
        if kind in {"projects", "comments"}:
            target = str(getattr(item, "id", "")).strip()
            if not target.isdigit():
                continue
        else:
            target = studio_item_text(getattr(item, "username", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", target):
                continue
            if target.casefold() == username.casefold():
                continue
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def studio_bulk_apply(studio: Any, kind: str, target: str) -> None:
    """一括整理の1件を実行する。プロジェクト本体は削除しない。"""
    if kind == "projects":
        ensure_studio_mutation_success(studio.remove_project(int(target)))
    elif kind == "comments":
        ensure_studio_mutation_success(studio.delete_comment(comment_id=int(target)))
    elif kind in {"curators", "managers"}:
        # scratchattachではマネージャーもremove_curator()でStudioから外れる。
        ensure_studio_mutation_success(studio.remove_curator(target))
    else:
        raise ValueError("一括整理対象が不正です。")


def studio_bulk_job_for_client() -> dict[str, Any] | None:
    """一括解除ジョブの状態を画面へ返す（内部オブジェクトは返さない）。"""
    with studio_bulk_state_lock:
        if studio_bulk_job is None:
            return None
        return {
            "status": studio_bulk_job.get("status"),
            "accountUsername": studio_bulk_job.get("accountUsername"),
            "studioId": studio_bulk_job.get("studioId"),
            "kind": studio_bulk_job.get("kind"),
            "label": studio_bulk_job.get("label"),
            "total": int(studio_bulk_job.get("total", 0)),
            "completed": int(studio_bulk_job.get("completed", 0)),
            "removed": list(studio_bulk_job.get("removed", [])),
            "failed": list(studio_bulk_job.get("failed", [])),
            "current": studio_bulk_job.get("current"),
            "startedAt": studio_bulk_job.get("startedAt"),
            "finishedAt": studio_bulk_job.get("finishedAt"),
        }


def set_studio_bulk_job(job_ref: dict[str, Any] | None = None, **changes: Any) -> None:
    with studio_bulk_state_lock:
        if studio_bulk_job is not None and (job_ref is None or studio_bulk_job is job_ref):
            studio_bulk_job.update(changes)


def run_studio_bulk_remove(
    username: str,
    studio_id: int,
    targets: list[str],
    stop_event: threading.Event,
    job_ref: dict[str, Any],
    kind: str,
) -> None:
    """所有確認済みStudioを一括整理する。"""
    try:
        # ジョブ開始後にも所有者を再確認する。
        _, studio = owned_studio(username, studio_id)
        for target in targets:
            if stop_event.is_set():
                set_studio_bulk_job(job_ref, status="stopped", current=None)
                break
            set_studio_bulk_job(job_ref, current=target)
            if not wait_for_studio_action(stop_event):
                set_studio_bulk_job(job_ref, status="stopped", current=None)
                break
            try:
                studio_bulk_apply(studio, kind, target)
            except Exception as exc:
                with studio_bulk_state_lock:
                    if studio_bulk_job is job_ref:
                        job_ref["status"] = "failed"
                        job_ref["current"] = None
                        job_ref["failed"].append({"id": target, "error": str(exc)})
                break
            with studio_bulk_state_lock:
                if studio_bulk_job is job_ref:
                    job_ref["removed"].append(target)
                    job_ref["completed"] += 1
                    job_ref["current"] = None
        else:
            set_studio_bulk_job(job_ref, status="completed", current=None)
    except Exception as exc:
        with studio_bulk_state_lock:
            if studio_bulk_job is job_ref:
                job_ref["status"] = "failed"
                job_ref["current"] = None
                job_ref["failed"].append({"id": None, "error": str(exc)})
    finally:
        with studio_bulk_state_lock:
            if studio_bulk_job is job_ref:
                if job_ref.get("status") == "running" and stop_event.is_set():
                    job_ref["status"] = "stopped"
                job_ref["finishedAt"] = datetime.now(JST).isoformat(timespec="seconds")


def connect_target(session: Any, target_type: str, target_value: str | int):
    if target_type == "project":
        return session.connect_project(int(target_value))
    if target_type == "studio":
        return session.connect_studio(int(target_value))
    return session.connect_user(str(target_value))


def post_comment(username: str, target_type: str, target_value: str | int, message: str) -> str:
    """投稿を直列化し、Scratch側のレート制限に配慮する。"""
    global last_post_at
    with sessions_lock:
        session = sessions.get(username)
    if session is None:
        raise RuntimeError(f"{username} はログインされていません。")

    with post_lock:
        wait_for = MIN_POST_INTERVAL - (time.monotonic() - last_post_at)
        if wait_for > 0:
            time.sleep(wait_for)
        target = connect_target(session, target_type, target_value)
        comment = target.post_comment(message)
        last_post_at = time.monotonic()
        return str(getattr(comment, "id", "不明"))


def reply_to_comment(username: str, comment: Any, message: str) -> str:
    """コメントへの返信を直列化し、Scratch側のレート制限に配慮する。"""
    global last_post_at
    with sessions_lock:
        session = sessions.get(username)
    if session is None:
        raise RuntimeError(f"{username} はログインされていません。")

    with post_lock:
        wait_for = MIN_POST_INTERVAL - (time.monotonic() - last_post_at)
        if wait_for > 0:
            time.sleep(wait_for)
        # comments() が返す Comment は同じ認証セッションを持っているため、
        # comment.reply() でProject/Studio/Profileを共通に扱える。
        posted = comment.reply(message)
        last_post_at = time.monotonic()
        return str(getattr(posted, "id", "不明"))


def post_for_accounts(usernames: list[str], target_type: str, target_value: str | int, message: str):
    sent: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for username in usernames:
        try:
            comment_id = post_comment(username, target_type, target_value, message)
            sent.append({"username": username, "commentId": comment_id})
        except Exception as exc:  # 投稿先ごとの失敗を一覧に残す
            failed.append({"username": username, "error": str(exc)})
    return sent, failed


def load_schedules() -> list[dict[str, Any]]:
    if not SCHEDULE_FILE.exists():
        return []
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_schedules(items: list[dict[str, Any]]) -> None:
    temporary = SCHEDULE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SCHEDULE_FILE)


schedules = load_schedules()


def load_reaction_rules() -> list[dict[str, Any]]:
    if not REACTION_FILE.exists():
        return []
    try:
        data = json.loads(REACTION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_reaction_rules(items: list[dict[str, Any]] | None = None) -> None:
    values = reaction_rules if items is None else items
    temporary = REACTION_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(REACTION_FILE)


def load_reaction_state() -> dict[str, Any]:
    if not REACTION_STATE_FILE.exists():
        return {"initialized": {}, "seen": {}, "pending": {}, "pool": {}}
    try:
        data = json.loads(REACTION_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("initialized", {})
        data.setdefault("seen", {})
        data.setdefault("pending", {})
        data.setdefault("pool", {})
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return {"initialized": {}, "seen": {}, "pending": {}, "pool": {}}


def save_reaction_state() -> None:
    temporary = REACTION_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(reaction_state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(REACTION_STATE_FILE)


reaction_rules = load_reaction_rules()
reaction_state = load_reaction_state()


def schedule_for_client(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "accountUsername": item.get("accountUsername", ""),
        "targetType": item.get("targetType", "project"),
        "targetId": item.get("targetId", ""),
        "postTime": item.get("postTime", ""),
        "message": item.get("message", ""),
        "enabled": bool(item.get("enabled", True)),
        "lastRunDate": item.get("lastRunDate"),
        "lastError": item.get("lastError"),
    }


def queue_for_client(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result.pop("_startedAt", None)
    if result.get("status") == "waiting" and queue_batch_started_at is not None:
        due = queue_batch_started_at + int(result.get("delaySeconds", 0))
        result["remainingSeconds"] = max(0, int(due - time.time() + 0.999))
    else:
        result["remainingSeconds"] = None
    return result


def reaction_for_client(item: dict[str, Any]) -> dict[str, Any]:
    reaction_type = str(item.get("reactionType", "keyword") or "keyword")
    # 旧版で保存した lottery 設定は、今回の仕様のランダム返信として扱う。
    if reaction_type == "lottery":
        reaction_type = "random_reply"
    templates = item.get("replyTemplates")
    if not isinstance(templates, list):
        templates = [str(item.get("replyTemplate", "") or "")]
    result = {
        "id": item.get("id"),
        "accountUsername": item.get("accountUsername", ""),
        "targetType": item.get("targetType", "project"),
        "targetId": item.get("targetId", ""),
        "reactionType": reaction_type,
        "matchMode": item.get("matchMode", "contains"),
        "triggerText": item.get("triggerText", ""),
        "replyTemplate": item.get("replyTemplate", ""),
        "replyTemplates": [str(value) for value in templates if str(value).strip()],
        "pollSeconds": int(item.get("pollSeconds", 30)),
        "maxReplies": int(item.get("maxReplies", DEFAULT_MAX_REPLIES)),
        "enabled": bool(item.get("enabled", True)),
        "lastRunAt": item.get("lastRunAt"),
        "lastAction": item.get("lastAction"),
        "lastError": item.get("lastError"),
    }
    rule_key = str(result["id"])
    with reaction_lock:
        result["pendingCount"] = len(reaction_state.get("pending", {}).get(rule_key, []))
    return result


def reaction_comment_record(comment: Any) -> dict[str, str]:
    try:
        comment_id = str(getattr(comment, "id", ""))
    except Exception:
        comment_id = ""
    try:
        text = str(getattr(comment, "text"))
    except Exception:
        text = str(getattr(comment, "content", "") or "")
    return {
        "id": comment_id,
        "text": text,
        "author": str(getattr(comment, "author_name", "") or ""),
        "authorId": str(getattr(comment, "author_id", "") or ""),
    }


def reaction_comment_key(record: dict[str, Any]) -> str:
    return str(record.get("id") or "")


def target_comments(target: Any) -> list[Any]:
    try:
        result = target.comments(limit=40)
    except TypeError:
        result = target.comments()
    return list(result or [])


def reaction_matches(rule: dict[str, Any], record: dict[str, str]) -> bool:
    match_mode = str(rule.get("matchMode") or "contains")
    needle = str(rule.get("triggerText") or "").strip()
    if match_mode == "any":
        return True
    if not needle:
        return False
    text = record.get("text", "")
    if match_mode == "exact":
        return text.strip() == needle
    return needle.casefold() in text.casefold()


def reaction_reply_text(template: str, record: dict[str, str]) -> str:
    # format_mapではなく単純置換にして、利用者が本文中に{}を書いても壊れないようにする。
    return (
        str(template)
        .replace("{username}", record.get("author", ""))
        .replace("{comment}", record.get("text", ""))
        .replace("{id}", record.get("id", ""))
    )


def reaction_reply_for_rule(rule: dict[str, Any], record: dict[str, str]) -> str:
    templates = rule.get("replyTemplates")
    if not isinstance(templates, list):
        templates = [rule.get("replyTemplate", "")]
    templates = [str(value).strip() for value in templates if str(value).strip()]
    if not templates:
        raise ValueError("返信候補が設定されていません。")
    reaction_type = str(rule.get("reactionType") or "keyword")
    template = random.choice(templates) if reaction_type in {"random_reply", "lottery"} else templates[0]
    return reaction_reply_text(template, record)


def _state_records(bucket: str, rule_key: str) -> list[dict[str, Any]]:
    value = reaction_state.setdefault(bucket, {}).setdefault(rule_key, [])
    if not isinstance(value, list):
        value = []
        reaction_state[bucket][rule_key] = value
    return value


def _remember_seen(rule_key: str, records: list[dict[str, str]]) -> set[str]:
    seen_list = _state_records("seen", rule_key)
    seen = {str(x) for x in seen_list}
    for record in records:
        key = reaction_comment_key(record)
        if key and key not in seen:
            seen_list.append(key)
            seen.add(key)
    if len(seen_list) > MAX_TRACKED_COMMENTS:
        del seen_list[:-MAX_TRACKED_COMMENTS]
    return seen


def _update_reaction_result(rule_id: int | str, *, action: str | None = None, error: str | None = None) -> None:
    now = datetime.now(JST).isoformat(timespec="seconds")
    with reaction_lock:
        for rule in reaction_rules:
            if str(rule.get("id")) == str(rule_id):
                rule["lastRunAt"] = now
                rule["lastAction"] = action
                rule["lastError"] = error
                break
        save_reaction_rules()
        save_reaction_state()


def process_reaction_rule(rule: dict[str, Any]) -> None:
    rule_id = str(rule.get("id"))
    username = str(rule.get("accountUsername") or "")
    with sessions_lock:
        session = sessions.get(username)
    if session is None:
        raise RuntimeError(f"{username} はログインされていません。")

    target_type, target_value = normalize_target(rule.get("targetType"), rule.get("targetId"))
    target = connect_target(session, target_type, target_value)
    comments = target_comments(target)
    records: list[dict[str, str]] = []
    for comment in comments:
        record = reaction_comment_record(comment)
        if reaction_comment_key(record):
            records.append(record)
    # APIは新しい順に返すので、処理順は古い順にしておく。
    records.reverse()
    now = datetime.now(JST).isoformat(timespec="seconds")

    with reaction_lock:
        initialized = reaction_state.setdefault("initialized", {})
        seen = _state_records("seen", rule_id)
        known = {str(x) for x in seen}
        if not initialized.get(rule_id):
            for record in records:
                key = reaction_comment_key(record)
                if key and key not in known:
                    seen.append(key)
                    known.add(key)
            if len(seen) > MAX_TRACKED_COMMENTS:
                del seen[:-MAX_TRACKED_COMMENTS]
            initialized[rule_id] = now
            save_reaction_state()
            initialized_now = True
        else:
            initialized_now = False

    if initialized_now:
        _update_reaction_result(rule.get("id"), action="初回確認: 既存コメントを基準化", error=None)
        return

    with reaction_lock:
        seen = _state_records("seen", rule_id)
        known = {str(x) for x in seen}
        new_records = [record for record in records if reaction_comment_key(record) not in known]
        _remember_seen(rule_id, new_records)
        new_records = [
            record for record in new_records
            if record.get("author", "").casefold() != username.casefold()
        ]
        eligible = [record for record in new_records if reaction_matches(rule, record)]
        max_replies = max(1, int(rule.get("maxReplies", DEFAULT_MAX_REPLIES)))
        pending = _state_records("pending", rule_id)
        pending_keys = {reaction_comment_key(item) for item in pending}
        for record in eligible:
            if reaction_comment_key(record) not in pending_keys:
                pending.append(record)
                pending_keys.add(reaction_comment_key(record))
        work_items = list(pending)
        reaction_state.setdefault("pending", {})[rule_id] = []
        save_reaction_state()

    sent_count = 0
    errors: list[str] = []
    remaining: list[dict[str, Any]] = []
    for record in work_items:
        if sent_count >= max_replies:
            remaining.append(record)
            continue
        try:
            reply = clean_template(reaction_reply_for_rule(rule, record))
            reply_to_comment(username, _find_comment_object(comments, record, target), reply)
            sent_count += 1
        except Exception as exc:
            attempts = int(record.get("attempts", 0)) + 1
            if attempts < 3:
                record["attempts"] = attempts
                remaining.append(record)
            errors.append(f"{record.get('author') or record.get('id')}: {exc}")
    with reaction_lock:
        reaction_state.setdefault("pending", {})[rule_id] = remaining[-MAX_TRACKED_COMMENTS:]
        save_reaction_state()
    action = f"新着{len(new_records)}件 / 返信{sent_count}件"
    _update_reaction_result(rule.get("id"), action=action, error="; ".join(errors) if errors else None)


def _find_comment_object(comments: list[Any], record: dict[str, Any], target: Any | None = None) -> Any:
    wanted = reaction_comment_key(record)
    for comment in comments:
        if str(getattr(comment, "id", "")) == wanted:
            return comment
    if target is not None and hasattr(target, "comment_by_id"):
        return target.comment_by_id(wanted)
    raise RuntimeError("対象コメントを取得できませんでした。次回確認時に再試行してください。")


def reaction_loop() -> None:
    global reaction_thread
    try:
        while not reaction_stop_event.wait(1):
            with reaction_lock:
                if not reaction_running:
                    break
                now = time.monotonic()
                due: list[dict[str, Any]] = []
                for item in reaction_rules:
                    if not item.get("enabled", True):
                        continue
                    rule_key = str(item.get("id"))
                    if reaction_due_at.get(rule_key, 0) <= now:
                        due.append(dict(item))
                        reaction_due_at[rule_key] = now + max(15, int(item.get("pollSeconds", 30)))
            for rule in due:
                try:
                    process_reaction_rule(rule)
                except Exception as exc:
                    _update_reaction_result(rule.get("id"), action="確認エラー", error=str(exc))
    finally:
        reaction_thread = None


def start_reaction_thread() -> None:
    global reaction_thread
    with reaction_lock:
        if reaction_thread is None or not reaction_thread.is_alive():
            reaction_stop_event.clear()
            reaction_thread = threading.Thread(target=reaction_loop, daemon=True, name="comment-reaction-bot")
            reaction_thread.start()


def scheduler_loop() -> None:
    global schedules
    while True:
        try:
            now = datetime.now(JST)
            today = now.date().isoformat()
            post_time = now.strftime("%H:%M")
            due_items: list[dict[str, Any]] = []
            with schedules_lock:
                for item in schedules:
                    if item.get("enabled", True) and item.get("postTime") == post_time and item.get("lastRunDate") != today:
                        item["lastRunDate"] = today
                        item["lastError"] = None
                        due_items.append(item.copy())
            if due_items:
                save_schedules(schedules)
                for item in due_items:
                    try:
                        target_type, target_value = normalize_target(item["targetType"], item["targetId"])
                        usernames = selected_usernames(item["accountUsername"])
                        _, failed = post_for_accounts(usernames, target_type, target_value, clean_message(item["message"]))
                        if failed:
                            item["lastError"] = "; ".join(f"{x['username']}: {x['error']}" for x in failed)
                            with schedules_lock:
                                for original in schedules:
                                    if original.get("id") == item.get("id"):
                                        original["lastError"] = item["lastError"]
                                        break
                    except Exception as exc:
                        with schedules_lock:
                            for original in schedules:
                                if original.get("id") == item.get("id"):
                                    original["lastError"] = str(exc)
                                    break
                    save_schedules(schedules)
        except Exception:
            # スケジューラの例外でWebサーバーを止めない。
            pass
        time.sleep(10)


def run_queue(batch_started: float, item_ids: list[int]) -> None:
    global queue_running, queue_batch_started_at
    try:
        for item_id in item_ids:
            with queue_lock:
                item = next((x for x in queue_items if x["id"] == item_id), None)
                if item is None or item.get("status") != "waiting":
                    continue
                delay = int(item.get("delaySeconds", 0))
            due = batch_started + delay
            while time.time() < due:
                with queue_lock:
                    if not queue_running:
                        return
                time.sleep(min(0.25, max(0.01, due - time.time())))
            with queue_lock:
                item = next((x for x in queue_items if x["id"] == item_id), None)
                if item is None or not queue_running:
                    return
                item["status"] = "sending"
                item["startedAt"] = datetime.now(JST).isoformat(timespec="seconds")
            try:
                target_type, target_value = normalize_target(item["targetType"], item["targetId"])
                usernames = selected_usernames(item["accountUsername"])
                sent, failed = post_for_accounts(usernames, target_type, target_value, clean_message(item["message"]))
                with queue_lock:
                    item["sent"] = sent
                    item["failed"] = failed
                    item["status"] = "sent" if not failed else ("failed" if not sent else "partial")
                    item["finishedAt"] = datetime.now(JST).isoformat(timespec="seconds")
            except Exception as exc:
                with queue_lock:
                    item["status"] = "failed"
                    item["failed"] = [{"error": str(exc)}]
                    item["finishedAt"] = datetime.now(JST).isoformat(timespec="seconds")
    finally:
        with queue_lock:
            queue_running = False
            queue_batch_started_at = None


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def state():
    with sessions_lock:
        account_list = [{"username": name} for name in sessions]
    with schedules_lock:
        schedule_list = [schedule_for_client(x) for x in schedules]
    with queue_lock:
        queue_list = [queue_for_client(item) for item in queue_items]
        running = queue_running
    with reaction_lock:
        reaction_list = [reaction_for_client(item) for item in reaction_rules]
        reaction_is_running = reaction_running
    return jsonify({
        "loggedIn": bool(account_list),
        "accounts": account_list,
        "schedules": schedule_list,
        "queue": queue_list,
        "queueRunning": running,
        "queueBatchStartedAt": queue_batch_started_at,
        "reactions": reaction_list,
        "reactionRunning": reaction_is_running,
        "studioBulk": studio_bulk_job_for_client(),
    })


@app.post("/api/login")
def login():
    data = body()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        return json_error("ユーザー名とパスワードを入力してください。")
    if len(username) > 30 or len(password) > 200:
        return json_error("入力が長すぎます。")
    try:
        import scratchattach as scratch3

        session = scratch3.login(username, password)
    except ModuleNotFoundError:
        return json_error("scratchattachが未インストールです。requirements.txtをインストールしてください。", 500)
    except Exception:
        return json_error("Scratchログインに失敗しました。ユーザー名・パスワードを確認してください。", 401)
    with sessions_lock:
        sessions[username] = session
    return jsonify({"ok": True, "username": username})


@app.post("/api/logout")
def logout():
    username = str(body().get("accountUsername") or "").strip()
    with studio_bulk_state_lock:
        if (
            studio_bulk_job is not None
            and studio_bulk_job.get("status") == "running"
            and studio_bulk_job.get("accountUsername") == username
            and studio_bulk_stop_event is not None
        ):
            studio_bulk_stop_event.set()
    with sessions_lock:
        sessions.pop(username, None)
    return jsonify({"ok": True})


def normalize_reaction_input(data: dict[str, Any]) -> dict[str, Any]:
    account = selected_single_username(data.get("accountUsername"))
    target_type, target_value = normalize_target(data.get("targetType"), data.get("targetId"))
    reaction_type = str(data.get("reactionType") or "keyword").strip().lower()
    if reaction_type == "lottery":
        reaction_type = "random_reply"
    if reaction_type not in {"keyword", "random_reply"}:
        raise ValueError("Botの種類が不正です。")
    match_mode = str(data.get("matchMode") or "contains").strip().lower()
    if match_mode not in {"contains", "exact", "any"}:
        raise ValueError("一致方法が不正です。")
    trigger_text = str(data.get("triggerText") or "").strip()
    if match_mode != "any" and not trigger_text:
        raise ValueError("反応キーワードを入力してください。")
    if len(trigger_text) > MAX_COMMENT_LENGTH:
        raise ValueError(f"キーワードは{MAX_COMMENT_LENGTH}文字以内にしてください。")
    raw_candidates = data.get("replyTemplates")
    if raw_candidates is None:
        raw_candidates = data.get("replyTemplate", "")
    if isinstance(raw_candidates, list):
        candidate_values = raw_candidates
    else:
        raw_text = str(raw_candidates or "")
        # 固定返信では改行を本文の一部として保持する。
        # ランダム返信だけ、1行ごとに候補として扱う。
        candidate_values = [raw_text] if reaction_type == "keyword" else raw_text.splitlines()
    reply_templates = []
    for value in candidate_values:
        if str(value).strip():
            reply_templates.append(clean_template(value))
    if not reply_templates:
        raise ValueError("返信候補を1行以上入力してください。")
    if len(reply_templates) > MAX_REPLY_CANDIDATES:
        raise ValueError(f"返信候補は最大{MAX_REPLY_CANDIDATES}件です。")
    try:
        poll_seconds = int(data.get("pollSeconds", 30))
        max_replies = int(data.get("maxReplies", DEFAULT_MAX_REPLIES))
    except (TypeError, ValueError):
        raise ValueError("確認間隔と返信数は数字で指定してください。")
    if not 15 <= poll_seconds <= 3600:
        raise ValueError("確認間隔は15〜3600秒で指定してください。")
    if not 1 <= max_replies <= 10:
        raise ValueError("1回の最大返信数は1〜10で指定してください。")
    return {
        "accountUsername": account,
        "targetType": target_type,
        "targetId": str(target_value),
        "reactionType": reaction_type,
        "matchMode": match_mode,
        "triggerText": trigger_text,
        "replyTemplate": reply_templates[0],
        "replyTemplates": reply_templates,
        "pollSeconds": poll_seconds,
        "maxReplies": max_replies,
    }


@app.post("/api/reactions")
def add_reaction_rule():
    data = body()
    try:
        normalized = normalize_reaction_input(data)
    except ValueError as exc:
        return json_error(str(exc))
    with reaction_lock:
        if len(reaction_rules) >= MAX_REACTION_RULES:
            return json_error(f"反応Botは最大{MAX_REACTION_RULES}件です。")
        duplicate = any(
            all(item.get(key) == value for key, value in normalized.items())
            for item in reaction_rules
        )
        if duplicate:
            return json_error("同じ設定の反応Botがすでにあります。")
        new_id = max((int(x.get("id", 0)) for x in reaction_rules), default=0) + 1
        item = {
            "id": new_id,
            **normalized,
            "enabled": True,
            "lastRunAt": None,
            "lastAction": None,
            "lastError": None,
            "createdAt": datetime.now(JST).isoformat(timespec="seconds"),
        }
        reaction_rules.append(item)
        save_reaction_rules()
    return jsonify({"ok": True, "reaction": reaction_for_client(item)})


@app.post("/api/reactions/start")
def start_reactions():
    global reaction_running
    with reaction_lock:
        if not any(item.get("enabled", True) for item in reaction_rules):
            return json_error("ONになっている反応Botがありません。")
        reaction_running = True
        for item in reaction_rules:
            if item.get("enabled", True):
                reaction_due_at[str(item.get("id"))] = 0
        reaction_stop_event.clear()
        start_reaction_thread()
    return jsonify({"ok": True})


@app.post("/api/reactions/stop")
def stop_reactions():
    global reaction_running
    with reaction_lock:
        reaction_running = False
        reaction_stop_event.set()
    return jsonify({"ok": True})


@app.post("/api/reactions/<int:reaction_id>/<action>")
def reaction_action(reaction_id: int, action: str):
    global reaction_rules
    if action not in {"toggle", "delete"}:
        return json_error("操作が不正です。")
    with reaction_lock:
        found = next((item for item in reaction_rules if int(item.get("id", -1)) == reaction_id), None)
        if found is None:
            return json_error("反応Botが見つかりません。", 404)
        if action == "toggle":
            found["enabled"] = not bool(found.get("enabled", True))
            save_reaction_rules()
            return jsonify({"ok": True, "reaction": reaction_for_client(found)})
        reaction_rules = [item for item in reaction_rules if int(item.get("id", -1)) != reaction_id]
        reaction_state.get("initialized", {}).pop(str(reaction_id), None)
        reaction_state.get("seen", {}).pop(str(reaction_id), None)
        reaction_state.get("pending", {}).pop(str(reaction_id), None)
        reaction_state.get("pool", {}).pop(str(reaction_id), None)
        reaction_due_at.pop(str(reaction_id), None)
        save_reaction_rules()
        save_reaction_state()
    return jsonify({"ok": True})


@app.post("/api/post-now")
def post_now():
    data = body()
    try:
        usernames = selected_usernames(data.get("accountUsername"))
        target_type, target_value = normalize_target(data.get("targetType"), data.get("targetId"))
        message = clean_message(data.get("message"))
        sent, failed = post_for_accounts(usernames, target_type, target_value, message)
        return jsonify({"ok": not failed, "sent": sent, "failed": failed})
    except ValueError as exc:
        return json_error(str(exc))


@app.get("/api/queue")
def get_queue():
    with queue_lock:
        return jsonify({"queue": [queue_for_client(x) for x in queue_items], "running": queue_running})


@app.post("/api/queue")
def add_queue_item():
    data = body()
    try:
        selected_usernames(data.get("accountUsername"))
        target_type, target_value = normalize_target(data.get("targetType"), data.get("targetId"))
        message = clean_message(data.get("message"))
        delay = int(data.get("delaySeconds", 0))
        if delay < 0 or delay > 86400:
            raise ValueError("投稿までの秒数は0〜86400秒で指定してください。")
    except (ValueError, TypeError):
        return json_error("入力内容を確認してください。")
    with queue_lock:
        if queue_running:
            return json_error("一括投稿中はキューを変更できません。")
        if len(queue_items) >= MAX_QUEUE_ITEMS:
            return json_error(f"キューは最大{MAX_QUEUE_ITEMS}件です。")
        new_id = max((int(x["id"]) for x in queue_items), default=0) + 1
        queue_items.append({
            "id": new_id,
            "accountUsername": str(data.get("accountUsername")),
            "targetType": target_type,
            "targetId": str(target_value),
            "message": message,
            "delaySeconds": delay,
            "status": "queued",
            "sent": [],
            "failed": [],
            "createdAt": datetime.now(JST).isoformat(timespec="seconds"),
        })
        return jsonify({"ok": True, "item": queue_for_client(queue_items[-1])})


@app.post("/api/queue/start")
def start_queue():
    global queue_running, queue_batch_started_at
    with queue_lock:
        if queue_running:
            return json_error("一括投稿はすでに実行中です。")
        pending = [x for x in queue_items if x.get("status") == "queued"]
        if not pending:
            return json_error("投稿待ちのコメントがありません。")
        queue_running = True
        queue_batch_started_at = time.time()
        batch_started = queue_batch_started_at
        item_ids = []
        for item in pending:
            item["status"] = "waiting"
            item_ids.append(int(item["id"]))
        thread = threading.Thread(target=run_queue, args=(batch_started, item_ids), daemon=True)
        thread.start()
    return jsonify({"ok": True, "startedAt": batch_started, "count": len(item_ids)})


@app.post("/api/queue/stop")
def stop_queue():
    global queue_running
    with queue_lock:
        queue_running = False
        for item in queue_items:
            if item.get("status") == "waiting":
                item["status"] = "cancelled"
    return jsonify({"ok": True})


@app.post("/api/queue/clear")
def clear_queue():
    with queue_lock:
        if queue_running:
            return json_error("一括投稿中はキューを空にできません。")
        queue_items.clear()
    return jsonify({"ok": True})


@app.post("/api/queue/<int:item_id>/delete")
def delete_queue_item(item_id: int):
    with queue_lock:
        if queue_running:
            return json_error("一括投稿中はキューを変更できません。")
        before = len(queue_items)
        queue_items[:] = [x for x in queue_items if int(x["id"]) != item_id]
        if len(queue_items) == before:
            return json_error("キュー項目が見つかりません。", 404)
    return jsonify({"ok": True})


@app.post("/api/studio/inspect")
def inspect_owned_studio():
    """所有スタジオの現在の内容を読み取り、整理画面へ返す。"""
    data = body()
    try:
        username = selected_single_username(data.get("accountUsername"))
        _, studio = owned_studio(username, data.get("studioId"))
        return jsonify({"ok": True, "studio": studio_snapshot(username, studio)})
    except PermissionError as exc:
        return json_error(str(exc), 403)
    except (ValueError, TypeError) as exc:
        return json_error(str(exc))
    except Exception as exc:
        return json_error(f"スタジオ情報の取得に失敗しました: {exc}", 502)


STUDIO_BULK_LABELS = {
    "projects": "プロジェクトをスタジオから外す",
    "comments": "コメントを削除",
    "curators": "キュレーターを外す",
    "managers": "マネージャーを外す",
}


def start_studio_bulk_job(kind: str, data: dict[str, Any]):
    """所有確認済みStudioの指定カテゴリを順番に整理するジョブを開始する。"""
    global studio_bulk_job, studio_bulk_stop_event, studio_bulk_thread
    if data.get("confirm") is not True:
        return json_error("操作確認が必要です。画面の確認ダイアログから実行してください。")
    if kind not in STUDIO_BULK_LABELS:
        return json_error("一括整理対象が不正です。")

    try:
        username = selected_single_username(data.get("accountUsername"))
        studio_id = normalize_studio_id(data.get("studioId"))
        _, studio = owned_studio(username, studio_id)
        targets = studio_bulk_targets(username, studio, kind)
        expected_count = data.get("expectedCount")
        if expected_count is not None:
            try:
                expected_count = int(expected_count)
            except (TypeError, ValueError):
                raise ValueError("確認した件数が不正です。")
            if expected_count != len(targets):
                raise ValueError("対象件数が変わりました。スタジオを再読み込みしてから実行してください。")
    except PermissionError as exc:
        return json_error(str(exc), 403)
    except (ValueError, TypeError) as exc:
        return json_error(str(exc))
    except Exception as exc:
        return json_error(f"一括整理の準備に失敗しました: {exc}", 502)

    if not targets:
        return jsonify({"ok": True, "started": False, "total": 0, "message": f"{STUDIO_BULK_LABELS[kind]}の対象はありません。"})

    with studio_bulk_state_lock:
        if (
            (studio_bulk_job is not None and studio_bulk_job.get("status") == "running")
            or (studio_bulk_thread is not None and studio_bulk_thread.is_alive())
        ):
            return json_error("すでに一括整理が実行中です。", 409)
        studio_bulk_stop_event = threading.Event()
        job = {
            "status": "running",
            "accountUsername": username,
            "studioId": str(getattr(studio, "id", studio_id)),
            "kind": kind,
            "label": STUDIO_BULK_LABELS[kind],
            "total": len(targets),
            "completed": 0,
            "removed": [],
            "failed": [],
            "current": None,
            "startedAt": datetime.now(JST).isoformat(timespec="seconds"),
            "finishedAt": None,
        }
        studio_bulk_job = job
        studio_bulk_thread = threading.Thread(
            target=run_studio_bulk_remove,
            args=(username, studio_id, targets, studio_bulk_stop_event, job, kind),
            daemon=True,
            name=f"studio-{kind}-bulk-cleanup",
        )
        studio_bulk_thread.start()
    return jsonify({"ok": True, "started": True, "total": len(targets), "kind": kind})


@app.post("/api/studio/bulk/start")
def start_studio_bulk_cleanup():
    data = body()
    return start_studio_bulk_job(str(data.get("kind") or "").strip().lower(), data)


@app.post("/api/studio/projects/remove-all/start")
def start_studio_bulk_remove():
    """互換用: 所有Studioから全プロジェクトの関連付けを外す。"""
    return start_studio_bulk_job("projects", body())


@app.post("/api/studio/bulk/stop")
@app.post("/api/studio/projects/remove-all/stop")
def stop_studio_bulk_remove():
    """実行中の一括整理を次の項目から停止する。"""
    data = body()
    try:
        username = selected_single_username(data.get("accountUsername"))
        studio_id = normalize_studio_id(data.get("studioId"))
    except (ValueError, TypeError) as exc:
        return json_error(str(exc))

    with studio_bulk_state_lock:
        job = studio_bulk_job
        if job is None or job.get("status") != "running":
            return json_error("実行中の一括整理はありません。", 409)
        if job.get("accountUsername") != username or str(job.get("studioId")) != str(studio_id):
            return json_error("別のスタジオの一括整理は停止できません。", 403)
        if studio_bulk_stop_event is not None:
            studio_bulk_stop_event.set()
    return jsonify({"ok": True, "stopping": True})


@app.post("/api/studio/action")
def studio_action():
    """所有スタジオ内の対象を1件だけ整理する。"""
    data = body()
    if data.get("confirm") is not True:
        return json_error("操作確認が必要です。画面の確認ダイアログから実行してください。")

    action = str(data.get("action") or "").strip().lower()
    allowed_actions = {"remove_project", "delete_comment", "remove_curator", "remove_manager"}
    if action not in allowed_actions:
        return json_error("整理操作が不正です。")

    try:
        username = selected_single_username(data.get("accountUsername"))
        _, studio = owned_studio(username, data.get("studioId"))
        target = str(data.get("target") or "").strip()
        if not target:
            raise ValueError("整理対象が指定されていません。")

        if action == "remove_project":
            if not target.isdigit():
                raise ValueError("プロジェクトIDが不正です。")
            projects = studio_action_members(studio, "projects")
            if not any(str(getattr(item, "id", "")) == target for item in projects):
                raise ValueError("そのプロジェクトは現在の一覧にありません。先に再読み込みしてください。")
            wait_for_studio_action()
            # プロジェクト自体は削除せず、このStudioからだけ外す。
            ensure_studio_mutation_success(studio.remove_project(int(target)))

        elif action == "delete_comment":
            if not target.isdigit():
                raise ValueError("コメントIDが不正です。")
            comments = studio_action_members(studio, "comments")
            if not any(str(getattr(item, "id", "")) == target for item in comments):
                raise ValueError("そのコメントは現在の一覧にありません。先に再読み込みしてください。")
            wait_for_studio_action()
            studio.delete_comment(comment_id=int(target))

        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", target):
                raise ValueError("ユーザー名が不正です。")
            if target.casefold() == username.casefold():
                raise ValueError("所有者自身は整理対象にできません。")
            kind = "curators" if action == "remove_curator" else "managers"
            members = studio_action_members(studio, kind)
            if not any(studio_item_text(getattr(item, "username", "")).casefold() == target.casefold() for item in members):
                raise ValueError("そのメンバーは現在の一覧にありません。先に再読み込みしてください。")
            wait_for_studio_action()
            # scratchattachではマネージャーもremove_curator()でStudioから外れる。
            studio.remove_curator(target)

        return jsonify({"ok": True, "action": action, "target": target})
    except PermissionError as exc:
        return json_error(str(exc), 403)
    except (ValueError, TypeError) as exc:
        return json_error(str(exc))
    except Exception as exc:
        return json_error(f"整理操作に失敗しました: {exc}", 502)


@app.post("/api/schedules")
def add_schedule():
    global schedules
    data = body()
    try:
        selected_usernames(data.get("accountUsername"))
        target_type, target_value = normalize_target(data.get("targetType"), data.get("targetId"))
        message = clean_message(data.get("message"))
        post_time = str(data.get("postTime") or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", post_time):
            raise ValueError("投稿時刻はHH:MM形式で入力してください。")
    except (ValueError, TypeError) as exc:
        return json_error(str(exc))
    with schedules_lock:
        duplicate = any(
            x.get("accountUsername") == str(data.get("accountUsername"))
            and x.get("targetType") == target_type
            and str(x.get("targetId")) == str(target_value)
            and x.get("postTime") == post_time
            and x.get("message") == message
            for x in schedules
        )
        if duplicate:
            return json_error("同じ内容の予約がすでにあります。")
        new_id = max((int(x.get("id", 0)) for x in schedules), default=0) + 1
        item = {
            "id": new_id,
            "accountUsername": str(data.get("accountUsername")),
            "targetType": target_type,
            "targetId": str(target_value),
            "postTime": post_time,
            "message": message,
            "enabled": True,
            "lastRunDate": None,
            "lastError": None,
        }
        schedules.append(item)
        schedules.sort(key=lambda x: (x.get("postTime", "99:99"), int(x.get("id", 0))))
        save_schedules(schedules)
    return jsonify({"ok": True, "schedule": schedule_for_client(item)})


@app.post("/api/schedules/<int:schedule_id>/<action>")
def schedule_action(schedule_id: int, action: str):
    global schedules
    if action not in {"toggle", "delete"}:
        return json_error("操作が不正です.")
    if action == "delete":
        with schedules_lock:
            before = len(schedules)
            schedules = [x for x in schedules if int(x.get("id", -1)) != schedule_id]
            if len(schedules) == before:
                return json_error("予約が見つかりません。", 404)
            save_schedules(schedules)
        return jsonify({"ok": True})
    with schedules_lock:
        for item in schedules:
            if int(item.get("id", -1)) == schedule_id:
                item["enabled"] = not bool(item.get("enabled", True))
                save_schedules(schedules)
                return jsonify({"ok": True, "schedule": schedule_for_client(item)})
    return json_error("予約が見つかりません。", 404)


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True, name="daily-scheduler").start()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    print(f"Scratch Comment Bot: http://{host}:{port}/")
    print("パスワードは保存せず、ログイン中だけメモリに保持します。")
    open_browser = os.environ.get("OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no", "off"}
    if open_browser:
        browser_timer = threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/"))
        browser_timer.daemon = True
        browser_timer.start()
    app.run(host=host, port=port, debug=False)
