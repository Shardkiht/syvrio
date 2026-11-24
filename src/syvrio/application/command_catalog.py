"""各客户端共用的命令名称、补全提示和文本拆分。"""

from __future__ import annotations

SLASH_COMMANDS = [
    ("new", "/new", "New session"),
    ("sessions", "/sessions", "Browse sessions"),
    ("compact", "/compact", "Compact context"),
    ("undo", "/undo", "Undo and edit last message"),
    ("summary", "/summary", "Show summary"),
    ("status", "/status", "Session status"),
    ("git", "/git", "Git status and synchronization"),
    ("model", "/model [refresh]", "Models & providers"),
    ("think", "/think", "Reasoning effort"),
    ("context", "/context", "Context window"),
    ("setup", "/setup", "Run setup in terminal"),
    ("update", "/update", "Check for software updates"),
    ("rename", "/rename <title>", "Rename session"),
    ("import", "/import", "Choose local files to attach"),
    ("pending", "/pending", "Review pending"),
    ("inferences", "/inferences", "View and review inferences"),
    ("profile", "/profile", "View the generated profile"),
    ("dream", "/dream [run|status]", "Update your profile and generate hypotheses"),
    ("topics", "/topics [name]", "Browse topics"),
    ("help", "/help", "Show commands"),
    ("exit", "/exit", "Quit"),
    ("quit", "/quit", "Quit"),
]

SLASH_SUBCOMMANDS = {
    "model": (("refresh", "Refresh model list"),),
    "dream": (
        ("run", "Update your profile and propose hypotheses for review"),
        ("status", "Show Dream status and available actions"),
    ),
}


def slash_matches(prefix: str) -> list[tuple[str, str, str]]:
    command, space, argument = prefix.lower().partition(" ")
    if not space:
        return [row for row in SLASH_COMMANDS if row[0].startswith(command)]
    argument = argument.lstrip()
    if " " in argument:
        return []
    return [
        (f"{command} {name}", f"/{command} {name}", description)
        for name, description in SLASH_SUBCOMMANDS.get(command, ())
        if name.startswith(argument)
    ]


def command_names() -> frozenset[str]:
    return frozenset(name for name, _, _ in SLASH_COMMANDS)


def parse_command(line: str) -> tuple[str, str]:
    """返回命令名和参数。未知命令不能当作普通消息发送。"""
    if not line.startswith("/"):
        return "", line.strip()
    head, _, argument = line.partition(" ")
    return head[1:].lower(), argument.strip()
