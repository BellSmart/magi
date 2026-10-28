"""账号管理命令行。

用户明确不要管理后台，因此建号、改密、封禁都走这里。
管理员在服务器上执行，天然有 shell 权限，不需要额外再鉴权一层。
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.sessions import active_sessions, revoke_all_for_user
from app.config import get_settings
from app.db import init_db, session_scope
from app.models import User


def _die(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def _read_password(allow_weak: bool = False) -> str:
    settings = get_settings()
    minimum = 1 if allow_weak else settings.min_password_length

    def _too_short(password: str) -> bool:
        return len(password) < minimum

    # 非交互（管道 / 部署脚本）时从 stdin 读一行，供自动化建号使用。
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
        if _too_short(password):
            _die(f"口令至少需要 {settings.min_password_length} 位")
        return password

    while True:
        first = getpass.getpass("口令: ")
        if _too_short(first):
            print(f"口令至少需要 {minimum} 位")
            continue
        second = getpass.getpass("再输一次: ")
        if first != second:
            print("两次输入不一致")
            continue
        return first


def _find(db, username: str) -> User | None:  # noqa: ANN001
    # 用户名大小写敏感：Alice 与 alice 是两个不同账号。
    return db.scalar(select(User).where(User.username == username))


def cmd_create_user(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as db:
        if _find(db, args.username) is not None:
            _die(f"用户 {args.username} 已存在")
        if args.allow_weak_password:
            print("警告：已跳过口令强度校验，该账号不适合公网长期使用。")
        password = _read_password(allow_weak=args.allow_weak_password)
        user = User(
            username=args.username,
            password_hash=hash_password(password),
            is_admin=args.admin,
        )
        db.add(user)
        db.flush()
        print(f"已创建用户 {user.username}（id={user.id}，管理员={user.is_admin}）")


def cmd_reset_password(args: argparse.Namespace) -> None:
    init_db()
    with session_scope() as db:
        user = _find(db, args.username)
        if user is None:
            _die(f"用户 {args.username} 不存在")
        user.password_hash = hash_password(_read_password(allow_weak=args.allow_weak_password))
        from app.models import utcnow

        user.password_changed_at = utcnow()
        killed = revoke_all_for_user(db, user.id)
        print(f"已重置 {user.username} 的口令，并吊销 {killed} 个在线会话")


def cmd_set_active(args: argparse.Namespace, active: bool) -> None:
    init_db()
    with session_scope() as db:
        user = _find(db, args.username)
        if user is None:
            _die(f"用户 {args.username} 不存在")
        user.is_active = active
        killed = revoke_all_for_user(db, user.id) if not active else 0
        print(
            f"已{'启用' if active else '封禁'} {user.username}"
            + (f"，吊销 {killed} 个在线会话" if killed else "")
        )


def cmd_list_users(_: argparse.Namespace) -> None:
    init_db()
    with session_scope() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        if not users:
            print("（暂无用户）")
            return
        print(f"{'ID':<4} {'用户名':<20} {'状态':<6} {'管理员':<6} 在线")
        for user in users:
            print(
                f"{user.id:<4} {user.username:<20} "
                f"{'启用' if user.is_active else '封禁':<6} "
                f"{'是' if user.is_admin else '否':<6} "
                f"{active_sessions(db, user.id):<4}"
            )


def cmd_gen_secret(_: argparse.Namespace) -> None:
    print(secrets.token_urlsafe(48))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="MAGI 账号管理")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="创建账号")
    p.add_argument("--username", required=True)
    p.add_argument("--admin", action="store_true")
    p.add_argument(
        "--allow-weak-password",
        action="store_true",
        help="跳过最小长度校验（会打印警告，不建议用于公网账号）",
    )
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("reset-password", help="重置口令（会踢掉该用户全部会话）")
    p.add_argument("--username", required=True)
    p.add_argument("--allow-weak-password", action="store_true")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("disable-user", help="封禁账号并踢下线")
    p.add_argument("--username", required=True)
    p.set_defaults(func=lambda a: cmd_set_active(a, False))

    p = sub.add_parser("enable-user", help="解除封禁")
    p.add_argument("--username", required=True)
    p.set_defaults(func=lambda a: cmd_set_active(a, True))

    p = sub.add_parser("list-users", help="列出账号")
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("gen-secret", help="生成 MAGI_SECRET_KEY")
    p.set_defaults(func=cmd_gen_secret)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
