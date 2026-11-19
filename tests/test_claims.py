import pytest

from agent.pipeline.claims import has_active_claim


@pytest.mark.parametrize(
    "text",
    [
        "I'll take this.",
        "Hi, I’m currently working on this issue.",
        "I will work on a fix.",
        "I would like to work on a fix for this.",
        "Please assign this to me.",
        "I would like to work on this issue.",
        "I'd like to take this.",
        "I’d like to tackle this one.",
        "@maintainer I'd like to take this on:\n- Add a test.\nI'll open a PR once the fix is verified.",
        "Can I work on this?",
        "Could I pick this up?",
        "Can you assign me this issue?",
        "I'll submit a PR for this.",
        "I have a draft pull request in progress for this issue.",
        "I already fixed this and will open a PR.",
        "/assign",
        "/claim",
        "我来修这个。",
        "我在处理这个问题。",
        "请分配给我。",
        "已认领",
        "这个 issue 我来处理。",
        "我来提个 PR。",
        "我正在提交一个 PR。",
        "Thanks for the report. I'll take this.",
    ],
)
def test_detects_explicit_claim(text):
    assert has_active_claim([("contributor", text)])


@pytest.mark.parametrize(
    "text",
    [
        "I'm not working on this.",
        "I won't take this.",
        "Can someone assign this issue?",
        "I can take a look at this.",
        "Can I work on another issue?",
        "Can I submit a PR?",
        "I can submit a PR.",
        "Anyone can submit a PR for this.",
        "I'll submit a PR for a different issue.",
        "I'll open a PR if no one is working on this.",
        "I already fixed this in my fork as a workaround.",
        "可以提个 PR。",
        "I am working on a different issue.",
        "I'm working on this project already.",
        "I'll take this if nobody else is available.",
        "If I'm working on this next week, I'll let you know.",
        "I was assigned to this last week.",
        "Is this already claimed?",
        "> I'll take this.\nWas that directed at me?",
        "Example: `I'll take this.`",
        "```text\nI'll take this.\n```\nThis is an example.",
        "~~~\n/assign\n~~~",
        "I saw a suggestion to use /claim.",
        "还没有人认领。",
        "这个问题已认领了吗？",
        "已认领？",
        "这个 issue 可以认领吗？",
    ],
)
def test_ignores_negation_quotes_hypotheticals_and_questions(text):
    assert not has_active_claim([("contributor", text)])


@pytest.mark.parametrize(
    "release",
    ["Closing my claim", "I'm no longer working on this.", "我放弃认领了。", "/unassign"],
)
def test_same_author_can_release_their_claim(release):
    assert not has_active_claim([("Jane", "I'll take this."), ("jane", release)])


def test_cannot_release_another_authors_claim():
    assert has_active_claim([("jane", "/claim"), ("sara", "Closing my claim")])


def test_author_can_claim_again_after_releasing():
    assert has_active_claim([("jane", "/claim"), ("jane", "/unassign"), ("jane", "/assign")])
