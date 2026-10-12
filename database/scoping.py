import contextvars
from contextlib import contextmanager

_tag: contextvars.ContextVar[str] = contextvars.ContextVar("bot_tag", default="main")

GLOBAL_SETTING_KEYS = {"clones", "clone_seq"}


def set_bot_tag(tag: str) -> contextvars.Token:
    return _tag.set(tag)


def reset_bot_tag(token: contextvars.Token) -> None:
    _tag.reset(token)


def current_tag() -> str:
    return _tag.get()


def scoped_key(key: str) -> str:
    if key in GLOBAL_SETTING_KEYS:
        return key
    tag = _tag.get()
    if tag == "main":
        return key
    return f"{tag}:{key}"


@contextmanager
def as_tag(tag: str):
    token = _tag.set(tag)
    try:
        yield
    finally:
        _tag.reset(token)
