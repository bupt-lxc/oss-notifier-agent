"""Recognize explicit work claims in GitHub issue text and comments."""

import re
from collections.abc import Iterable, Iterator

_CODE_BLOCK = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|\Z)"
    r".*?(?:^[ \t]{0,3}(?P=fence)[`~]*[ \t]*(?=\r?$)|\Z)",
    re.MULTILINE | re.DOTALL,
)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
_SENTENCES = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])\s+")
_COMMAND = re.compile(r"/(?:assign|claim)(?:\s+me)?[.!]?", re.IGNORECASE)
_HYPOTHETICAL = re.compile(r"\b(?:if|unless|maybe|perhaps)\b|如果|假如|要是", re.IGNORECASE)

_ISSUE_REFERENCE = r"(?:this(?:\s+(?:issue|bug|task|one|fix))?|it)"
_ENGLISH_TARGET = rf"(?:{_ISSUE_REFERENCE}|(?:a|the)\s+fix(?:\s+for\s+{_ISSUE_REFERENCE})?)"
_WORK_ON_ISSUE = (
    rf"(?:take(?: on| over)?|claim|handle|(?:start )?work on|fix|tackle|pick up)\s+{_ENGLISH_TARGET}"
    rf"|pick\s+{_ENGLISH_TARGET}\s+up"
    rf"|take\s+{_ENGLISH_TARGET}\s+on"
)
_PULL_REQUEST = r"(?:a|my)\s+(?:draft\s+)?(?:pr|pull request)"
_OPEN_PULL_REQUEST = rf"(?:open|submit|send|raise)\s+{_PULL_REQUEST}(?:\s+for\s+{_ENGLISH_TARGET})?"
_ENGLISH_CLAIM = re.compile(
    r"^(?:(?:hi|hey|hello|sure|yes)(?:[,!]\s*|\s+(?=@)))?"
    r"(?:@[a-z0-9-]+[,:]?\s+)*"
    r"(?:(?:i(?:['’]ll|['’]d like to|\s+(?:will|can|want to|would like to))|(?:can|could|may) i)\s+"
    rf"(?:{_WORK_ON_ISSUE})"
    r"|i(?:['’]m| am)\s+(?:(?:already|currently)\s+)?"
    rf"(?:working on|taking|claiming|fixing|handling)\s+{_ENGLISH_TARGET}"
    rf"|i(?:['’]ll| will)\s+{_OPEN_PULL_REQUEST}"
    rf"|i have\s+{_PULL_REQUEST}\s+in progress(?:\s+for\s+{_ENGLISH_TARGET})?"
    rf"|i (?:already\s+)?(?:fixed|resolved)\s+{_ENGLISH_TARGET}\s+and will\s+{_OPEN_PULL_REQUEST}"
    r"|(?:please\s+|(?:can|could|would) you\s+(?:please\s+)?)?"
    rf"assign\s+(?:{_ENGLISH_TARGET}\s+to\s+me|me(?:\s+{_ENGLISH_TARGET})?))"
    r"(?=\s*(?:$|[.,!?;:]|\b(?:already|and|now|today|myself|please)\b))",
    re.IGNORECASE,
)

_CHINESE_TARGET = r"(?:这个(?:问题|任务|\s*issue)?|此(?:问题|任务)|这条(?:\s*issue)?|它)"
_CHINESE_CLAIM = re.compile(
    rf"^(?:我(?:(?:已经|现在)?在(?:修复?|处理|做|跟进)|(?:来|会)(?:修复?|处理|做|跟进)"
    rf"|(?:已(?:经)?)?认领)(?:{_CHINESE_TARGET})?(?:了)?"
    rf"|{_CHINESE_TARGET}\s*我(?:来|在)?(?:修复?|处理|做|认领|跟进)(?:了)?"
    rf"|(?:请|麻烦)(?:把{_CHINESE_TARGET})?(?:分配|指派)给我"
    r"|我(?:来|会|正?在|已经在)\s*(?:提(?:交)?|开|发(?:起)?)\s*(?:一?个\s*)?(?:pr|pull request)"
    rf"|(?:{_CHINESE_TARGET}\s*)?已(?:经)?认领(?:了)?)"
    r"(?=\s*(?:$|[。，！；,.!;]))",
    re.IGNORECASE,
)
_RELEASE = re.compile(
    r"^(?:/unassign(?:\s+me)?[.!]?$"
    r"|(?:closing|withdrawing|releasing|dropping)\s+my\s+claim\b"
    r"|i(?:['’]m| am)\s+(?:no longer working on|not working on\b[^.!?]*\banymore\b)"
    r"|(?:我(?:决定)?)?(?:放弃|取消)(?:了)?认领(?:了)?(?=$|[。！,，.!]))",
    re.IGNORECASE,
)


def _sentences(text: str) -> Iterator[str]:
    for line in _CODE_BLOCK.sub("\n", text).splitlines():
        if line.lstrip().startswith(">") or line.startswith(("    ", "\t")):
            continue
        for sentence in _SENTENCES.split(_INLINE_CODE.sub("", line)):
            sentence = sentence.strip()
            if sentence and not _HYPOTHETICAL.search(sentence):
                yield sentence


def _matches_claim(sentence: str) -> bool:
    return bool(
        _COMMAND.fullmatch(sentence)
        or _ENGLISH_CLAIM.match(sentence)
        or _CHINESE_CLAIM.match(sentence)
    )


def has_active_claim(comments: Iterable[tuple[str, str]]) -> bool:
    """Check chronological (login, text) pairs, letting authors release their claims."""
    claimants: set[str] = set()
    for author, text in comments:
        login = author.casefold()
        for sentence in _sentences(text):
            if login and _RELEASE.match(sentence) and not sentence.endswith(("?", "？")):
                claimants.discard(login)
            elif _matches_claim(sentence):
                claimants.add(login)
    return bool(claimants)
