"""Interactive guided setup (wizard) for icloudpd.

Running icloudpd from a terminal *with no arguments at all* is otherwise a dead
end (it just prints the usage text). In that single case the binary instead
walks the user through the settings it needs: Apple ID, password, iCloud domain
and download directory, plus the most common download options.

Any command line argument switches the wizard off, so scripted and experienced
usage behaves exactly like upstream. ``--guided`` / ``--no-guided`` force it on
or off explicitly.

Only input collection happens here; the collected answers are applied on top of
the parsed configuration. Note that only ``--username`` has to be spliced back
into ``argv`` (it acts as the per-account group separator), while every other
value is written straight into the config objects -- which also keeps the
password out of the process list.

The Apple ID and the download directory are offered as defaults when the last
successful run remembered them (see :mod:`icloudpd.remember`). Pass
``--no-remember`` to switch that off.

The module also owns the terminal check and the "press any key to exit" prompt
used after a run that was started from a file manager, where the console would
otherwise close before the result could be read. Pass ``--no-pause`` to skip it.
"""

import getpass
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Sequence

from icloudpd.config import GlobalConfig, UserConfig
from icloudpd.remember import Remembered
from pyicloud_ipd.version_size import AssetVersionSize

DOMAIN_CHOICES: Sequence[str] = ["com", "cn"]
SIZE_CHOICES: Sequence[str] = ["original", "medium", "thumb", "adjusted", "alternative"]

USERNAME_FLAGS: Sequence[str] = ["-u", "--username"]
PASSWORD_FLAGS: Sequence[str] = ["-p", "--password"]
DIRECTORY_FLAGS: Sequence[str] = ["-d", "--directory"]
ALBUM_FLAGS: Sequence[str] = ["-a", "--album"]
SIZE_FLAGS: Sequence[str] = ["--size"]
RECENT_FLAGS: Sequence[str] = ["--recent"]
DOMAIN_FLAGS: Sequence[str] = ["--domain"]
WATCH_FLAGS: Sequence[str] = ["--watch-with-interval"]

GUIDED_FLAG = "--guided"
NO_GUIDED_FLAG = "--no-guided"
NO_REMEMBER_FLAG = "--no-remember"
NO_PAUSE_FLAG = "--no-pause"

# Flags that only tune how an interactive run behaves. On their own they are not
# a request to download anything, so they must not switch the wizard off -- and
# therefore they are not "meaningful" arguments (see ``_meaningful_args``).
_BEHAVIOUR_FLAGS = (NO_REMEMBER_FLAG, NO_PAUSE_FLAG)

# Tokens that clear an optional numeric setting instead of setting it to a number.
_NONE_TOKENS = {"0", "none", "-", "no", "不", "全部", "无"}


class GuidedSetupCancelled(Exception):
    """Raised when the user aborts the guided setup (Ctrl-C or end of input)."""


@dataclass
class GuidedAnswers:
    """Values collected by the guided setup.

    ``None`` means "not asked", so nothing is applied for that setting. Settings
    that legitimately accept ``None`` (recent, watch interval) use an extra
    ``*_asked`` flag to tell "not asked" apart from "answered: no limit".
    """

    username: str | None = None
    password: str | None = None
    password_asked: bool = False
    domain: str | None = None
    directory: str | None = None
    sizes: list[str] = field(default_factory=list)
    sizes_asked: bool = False
    recent: int | None = None
    recent_asked: bool = False
    albums: list[str] = field(default_factory=list)
    albums_asked: bool = False
    skip_videos: bool | None = None
    watch_with_interval: int | None = None
    watch_asked: bool = False


def is_interactive() -> bool:
    """True when both stdin and stdout are attached to a terminal."""
    try:
        return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        # pytest replaces stdio with capture objects that may not support isatty()
        return False


def _read_key() -> None:
    """Block until a single key is pressed, without needing Enter."""
    if os.name == "nt":
        import msvcrt

        msvcrt.getch()
        return

    import termios
    import tty

    descriptor = sys.stdin.fileno()
    settings = termios.tcgetattr(descriptor)
    try:
        # Raw mode makes Enter count as "any key" and returns Ctrl-C as a byte
        # instead of raising, which is what we want for a final prompt.
        tty.setraw(descriptor)
        os.read(descriptor, 1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, settings)


def wait_for_exit(message: str = "按任意键退出 ...") -> None:
    """Hold the window open until a key is pressed.

    Started from a file manager the console would otherwise vanish the moment
    the run ends, taking the result summary with it. This only makes sense when
    a human is actually in front of a terminal: a run whose stdio is a pipe or a
    file (cron, a wrapper script, CI) must never block on a key that is never
    going to come. Pass ``--no-pause`` to opt out even in a terminal.
    """
    if not is_interactive():
        return

    try:
        print()
        print(message, end="", flush=True)
    except (OSError, ValueError):
        return

    try:
        _read_key()
    except (EOFError, KeyboardInterrupt):
        # the user is already on their way out; nothing to hold open any more
        return
    except (OSError, ValueError):
        return

    try:
        print()
    except (OSError, ValueError):
        pass


def _flag_values(args: Sequence[str], flags: Sequence[str]) -> list[str]:
    """Collect values of ``--flag value`` and ``--flag=value`` from raw arguments."""
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        for flag in flags:
            if arg == flag:
                if index + 1 < len(args):
                    values.append(args[index + 1])
                    index += 1
                break
            if arg.startswith(flag + "="):
                values.append(arg[len(flag) + 1 :])
                break
        index += 1
    return values


def _flag_value(args: Sequence[str], flags: Sequence[str]) -> str | None:
    """First value of a single-valued option, or None."""
    values = _flag_values(args, flags)
    return values[0] if values else None


def _flag_present(args: Sequence[str], flags: Sequence[str]) -> bool:
    for arg in args:
        for flag in flags:
            if arg == flag or arg.startswith(flag + "="):
                return True
    return False


def _flag_count(args: Sequence[str], flags: Sequence[str]) -> int:
    count = 0
    for arg in args:
        for flag in flags:
            if arg == flag or arg.startswith(flag + "="):
                count += 1
                break
    return count


def _asks_for_help_or_version(args: Sequence[str]) -> bool:
    return _flag_present(args, ["-h", "--help", "--version"])


def _directory_required_by_mode(args: Sequence[str]) -> bool:
    """False when the run does not download anything, so no directory is needed."""
    return not _flag_present(args, ["--auth-only", "-l", "--list-albums", "--list-libraries"])


def _meaningful_args(args: Sequence[str]) -> list[str]:
    """Arguments that express intent, i.e. everything but the behaviour flags.

    ``--no-remember`` only says "do not keep my Apple ID and directory around"
    and ``--no-pause`` only says "do not wait for a key at the end"; neither is
    a request to download something, so they must not switch the wizard off.
    Otherwise opting out of remembering would also cost the user the very
    wizard it is meant to configure.
    """
    return [arg for arg in args if arg not in _BEHAVIOUR_FLAGS]


def _needs_guided_setup(args: Sequence[str]) -> bool:
    """Auto trigger: a completely bare invocation, and nothing else.

    Any argument at all means the caller already knows what it wants, so those
    runs keep upstream behaviour untouched. That matters for two reasons: a
    partially specified command line must not turn into an interactive prompt
    (it would hang cron/docker/pipe callers), and a run that is missing a
    required option should fail with upstream's error message rather than
    silently start asking questions.
    """
    return len(_meaningful_args(args)) == 0


def should_run_guided_setup(args: Sequence[str]) -> bool:
    """Decide whether the guided setup should run for this invocation."""
    if _asks_for_help_or_version(args):
        return False
    if _flag_present(args, [NO_GUIDED_FLAG]):
        return False
    if _flag_count(args, USERNAME_FLAGS) > 1:
        # the guided setup handles a single account; multi-account runs stay scripted
        return False
    if _flag_present(args, [GUIDED_FLAG]):
        return True
    if not is_interactive():
        return False
    return _needs_guided_setup(args)


def has_username_option(args: Sequence[str]) -> bool:
    """True when the command line already names an account."""
    return _flag_count(args, USERNAME_FLAGS) > 0


def _read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as error:
        raise GuidedSetupCancelled() from error


def _ask_text(label: str, default: str | None = None, required: bool = False) -> str:
    """Ask for free text. Pressing Enter returns ``default``."""
    while True:
        suffix = f" [{default}]" if default else ""
        raw = _read_line(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("  这一项不能为空，请重新输入。")


def _ask_choice(label: str, choices: Sequence[str], default: str) -> str:
    while True:
        raw = _ask_text(f"{label} ({'/'.join(choices)})", default=default).lower()
        if raw in choices:
            return raw
        print(f"  请输入以下之一：{', '.join(choices)}")


def _ask_yes_no(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _ask_text(f"{label} [{hint}]").lower()
        if raw == "":
            return default
        if raw in ("y", "yes", "是", "1"):
            return True
        if raw in ("n", "no", "否", "2"):
            return False
        print("  请输入 y 或 n。")


def _ask_int(label: str, default: int | None = None, none_hint: str = "不限制") -> int | None:
    """Ask for a non-negative integer. Empty keeps the default, ``0`` clears it."""
    hint = str(default) if default is not None else none_hint
    while True:
        raw = _read_line(f"{label} [{hint}]: ").strip()
        if raw == "":
            return default
        if raw.lower() in _NONE_TOKENS:
            return None
        try:
            value = int(raw)
        except ValueError:
            print("  请输入一个整数。")
            continue
        if value < 0:
            print("  请输入一个非负整数。")
            continue
        return value


def _ask_csv(label: str, hint: str) -> list[str]:
    raw = _read_line(f"{label} [{hint}]: ").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]


def _ask_sizes(label: str, default: list[str]) -> list[str]:
    hint = ",".join(default) if default else "original"
    while True:
        raw = _read_line(f"{label} ({'/'.join(SIZE_CHOICES)}, 逗号分隔) [{hint}]: ").strip()
        if not raw:
            return list(default) if default else ["original"]
        parts = [part.strip().lower() for part in raw.replace("，", ",").split(",") if part.strip()]
        unsupported = [part for part in parts if part not in SIZE_CHOICES]
        if unsupported:
            print(f"  不支持：{', '.join(unsupported)}")
            continue
        return parts


def _ask_password(username: str) -> str | None:
    # The password is never written to the memory file, and the system keyring is
    # deliberately not consulted: the wizard asks for the password itself, so a
    # keyring lookup would only add an extra, unasked-for credential source.
    prompt = f"{username} 的 iCloud 密码*（输入时不显示；直接回车表示暂时不输入）: "
    try:
        return getpass.getpass(prompt) or None
    except EOFError as error:
        raise GuidedSetupCancelled() from error


def _print_header() -> None:
    print()
    print("iCloud 照片下载 - 引导设置")
    print("直接回车表示使用方括号 [ ] 中的默认值；随时按 Ctrl-C 退出。")
    print()


def _print_summary(answers: GuidedAnswers) -> None:
    lines = []
    if answers.username:
        lines.append(f"Apple ID: {answers.username}")
    if answers.domain:
        lines.append(f"iCloud 区域: {answers.domain}")
    if answers.directory:
        lines.append(f"保存目录: {pathlib.Path(answers.directory).expanduser()}")
    if answers.sizes_asked:
        lines.append(f"照片尺寸: {', '.join(answers.sizes)}")
    if answers.recent_asked:
        lines.append(f"最近张数: {answers.recent if answers.recent else '全部'}")
    if answers.albums_asked:
        lines.append(f"相册: {', '.join(answers.albums) if answers.albums else '全部照片'}")
    if answers.skip_videos is not None:
        lines.append(f"跳过视频: {'是' if answers.skip_videos else '否'}")
    if answers.watch_asked:
        lines.append(
            "定时循环: "
            + (f"{answers.watch_with_interval} 秒" if answers.watch_with_interval else "不启用")
        )
    if not lines:
        return
    print()
    print("即将使用以下设置：")
    for line in lines:
        print(f"  - {line}")
    print()


def run_guided_setup(args: Sequence[str], remembered: Remembered | None = None) -> GuidedAnswers:
    """Ask the user for settings, filling in whatever the command line missed.

    A bare invocation asks for the four essential settings only and then starts
    downloading; ``--guided`` additionally walks through the common download
    options, which is what a first-time user wants when they are unsure.

    ``remembered`` carries the Apple ID and directory of the last successful run
    and is used for nothing but pre-filling those two prompts.
    """
    last_run = remembered or Remembered()
    answers = GuidedAnswers()
    ask_everything = _flag_present(args, [GUIDED_FLAG])
    ask_directory = _directory_required_by_mode(args) and not _flag_present(args, DIRECTORY_FLAGS)

    _print_header()

    existing_username = _flag_value(args, USERNAME_FLAGS)
    if existing_username:
        answers.username = existing_username
    else:
        answers.username = _ask_text(
            "Apple ID（邮箱或手机号前面+86）",
            default=last_run.username,
            required=True,
        )

    if not _flag_present(args, PASSWORD_FLAGS):
        answers.password_asked = True
        answers.password = _ask_password(answers.username)

    # the core settings are always asked: silently defaulting the region to `com`
    # would be wrong for users in mainland China
    if not _flag_present(args, DOMAIN_FLAGS):
        answers.domain = _ask_choice(
            "iCloud 区域（中国大陆选 cn）",
            DOMAIN_CHOICES,
            "com",
        )

    if ask_directory:
        default_directory = (
            _flag_value(args, DIRECTORY_FLAGS)
            or last_run.directory
            or str(pathlib.Path.home() / "Pictures" / "iCloud Photos")
        )
        answers.directory = _ask_text("下载保存目录", default=default_directory, required=True)

    if ask_everything:
        existing_recent = _flag_value(args, RECENT_FLAGS)
        answers.sizes_asked = True
        answers.sizes = _ask_sizes("照片尺寸", _flag_values(args, SIZE_FLAGS))

        answers.recent_asked = True
        answers.recent = _ask_int(
            "只下载最近 N 张照片",
            default=int(existing_recent) if existing_recent else None,
            none_hint="全部",
        )

        answers.albums_asked = True
        answers.albums = _ask_csv(
            "要下载的相册", ", ".join(_flag_values(args, ALBUM_FLAGS)) or "全部照片"
        )

        existing_watch = _flag_value(args, WATCH_FLAGS)
        answers.watch_asked = True
        answers.watch_with_interval = _ask_int(
            "定时循环同步间隔秒数",
            default=int(existing_watch) if existing_watch else None,
            none_hint="只跑一次",
        )

        answers.skip_videos = _ask_yes_no(
            "跳过视频，只下载照片", default=_flag_present(args, ["--skip-videos"])
        )

    _print_summary(answers)
    return answers


def apply_answers(
    answers: GuidedAnswers, global_config: GlobalConfig, user_config: UserConfig | None
) -> None:
    """Apply collected answers on top of the parsed configuration."""
    if answers.domain is not None:
        global_config.domain = answers.domain
    if answers.watch_asked:
        global_config.watch_with_interval = answers.watch_with_interval

    if user_config is None:
        return
    if answers.password_asked:
        user_config.password = answers.password
    if answers.directory:
        user_config.directory = str(pathlib.Path(answers.directory).expanduser())
    if answers.sizes_asked:
        user_config.sizes = [AssetVersionSize(size) for size in answers.sizes]
    if answers.recent_asked:
        user_config.recent = answers.recent
    if answers.albums_asked:
        user_config.albums = list(answers.albums)
    if answers.skip_videos is not None:
        user_config.skip_videos = answers.skip_videos
