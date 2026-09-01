import uuid

import bashlex

from vendors.openhands.core.logger import openhands_logger as logger


def split_bash_commands(commands: str) -> list[str]:
    if not commands.strip():
        return ['']
    try:
        parsed = bashlex.parse(commands)
    except (
        bashlex.errors.ParsingError,
        NotImplementedError,
        TypeError,
        AttributeError,
    ):
        logger.debug(
            f'Failed to parse bash commands\n'
            f'[input]: {commands}\n'
            f'The original command will be returned as is.',
            exc_info=True,
        )
        return [commands]

    result: list[str] = []
    last_end = 0

    for node in parsed:
        start, end = node.pos
        if start > last_end:
            between = commands[last_end:start]
            logger.debug(f'BASH PARSING between: {between}')
            if result:
                result[-1] += between.rstrip()
            elif between.strip():
                result.append(between.rstrip())

        command = commands[start:end].rstrip()
        logger.debug(f'BASH PARSING command: {command}')
        result.append(command)
        last_end = end

    remaining = commands[last_end:].rstrip()
    logger.debug(f'BASH PARSING remaining: {remaining}')
    if last_end < len(commands) and result:
        result[-1] += remaining
        logger.debug(f'BASH PARSING result[-1] += remaining: {result[-1]}')
    elif last_end < len(commands) and remaining:
        result.append(remaining)
        logger.debug(f'BASH PARSING result.append(remaining): {result[-1]}')
    return result


def escape_bash_special_chars(command: str) -> str:
    r"""Returns the command as-is without modification."""
    return command


def remove_command_prefix(command_output: str, command: str) -> str:
    return command_output.lstrip().removeprefix(command.lstrip()).lstrip()


def build_tmux_session_name(username: str | None) -> str:
    username_part = username or 'user'
    username_part = username_part.replace(':', '__').replace('.', '_')
    return f'openhands-{username_part}-{uuid.uuid4()}'
