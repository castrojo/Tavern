"""Doctor parsing and cache contracts do not need GTK or a real brew."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json

import pytest

from loader import load_module

doctor = load_module('src/doctor.py', 'doctor')


def test_healthy_report():
    report = doctor.parse_report('Your system is ready to brew.\n', 0)
    assert report.issues == ()
    assert report.raw_output == 'Your system is ready to brew.\n'


def test_warnings_exit_one_keep_blocks_and_strip_ansi():
    raw = '\x1b[33mWarning:\x1b[0m Unlinked kegs\nRun this command:\n  brew link hello\n\nWarning: Old setup\nThis is a Tier 3 configuration.\n'
    report = doctor.parse_report(raw, 1)
    assert [issue.severity for issue in report.issues] == ['unsupported', 'warning']
    assert report.issues[1].body == 'Run this command:\n  brew link hello'
    assert '\x1b' not in report.raw_output
    assert report.issues[1].content_id == doctor.parse_report(raw, 1).issues[1].content_id


def test_errors_precede_warnings():
    report = doctor.parse_report('Warning: Small problem\nDetails\nError: Major problem\nMore details', 1)
    assert [issue.title for issue in report.issues] == ['Major problem', 'Small problem']


@pytest.mark.parametrize('output,code', [('', 1), ('brew not found', 127), ('Warning: partial', 2)])
def test_command_failure_is_not_healthy(output, code):
    with pytest.raises(doctor.DoctorError):
        doctor.parse_report(output, code)


def test_cache_force_and_expiry():
    calls = []
    now = [0]

    def run():
        calls.append(True)
        return SimpleNamespace(stdout='Your system is ready to brew.', returncode=0)

    service = doctor.DoctorService(run, clock=lambda: now[0])
    first = service.load()
    assert service.load() is first
    service.load(force=True)
    assert len(calls) == 2
    now[0] = doctor.REPORT_TTL
    service.load()
    assert len(calls) == 3


def test_failed_refresh_does_not_replace_report_or_timestamp():
    calls = []
    now = [0]

    def run():
        calls.append(True)
        if len(calls) == 2:
            raise TimeoutError('Timed out')
        return SimpleNamespace(stdout='Warning: Fix your setup', returncode=1)

    service = doctor.DoctorService(run, clock=lambda: now[0])
    first = service.load()
    now[0] = doctor.REPORT_TTL
    with pytest.raises(TimeoutError):
        service.load()
    assert service._report is first
    assert service._reported_at == 0
    service.load()
    assert len(calls) == 3


def test_concurrent_loads_share_one_report():
    calls = []

    def run():
        calls.append(True)
        return SimpleNamespace(stdout='All good', returncode=0)

    service = doctor.DoctorService(run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _index: service.load(), range(8)))
    assert all(report is reports[0] for report in reports)
    assert len(calls) == 1


def test_homebrew_seven_structured_findings():
    data = {'tier': 3, 'findings': [
        {'text': 'Unlinked kegs\nDetails', 'tier': 1, 'affects': ['hello'], 'links': [],
         'remediation': {'commands': ['brew link hello'], 'text': 'Review this first'}},
        {'text': 'Old platform', 'tier': 3, 'remediation': None},
    ]}
    report = doctor.parse_report(json.dumps(data), 1)
    assert report.tier == 3
    assert report.issues[0].severity == 'unsupported'
    assert report.issues[1].title == 'Unlinked kegs'
    assert report.issues[1].body == 'Details\nReview this first\n  brew link hello'
    # remediation prose becomes prose, the command becomes a runnable command block, and the unlisted
    # affects become their own data block bound to the finding.
    assert report.issues[1].is_runnable is True
    blocks = report.issues[1].blocks
    assert blocks[0].type == 'prose' and blocks[0].value == ('Details', 'Review this first')
    assert blocks[1].type == 'command' and blocks[1].value[0].arguments == ('link', 'hello')
    assert blocks[2].type == 'data' and blocks[2].value == ('hello',)


@pytest.mark.parametrize('data,code', [({}, 0), ([], 0),
    ({'tier': 1, 'findings': []}, 1), ({'tier': 9, 'findings': []}, 0),
    ({'tier': 1, 'findings': ['broken']}, 1),
    ({'tier': 1, 'findings': [{'text': ''}]}, 1)])
def test_invalid_structured_report_is_not_healthy(data, code):
    with pytest.raises(doctor.DoctorError):
        doctor.parse_report(json.dumps(data), code)


def test_structured_report_keeps_stderr_separate_from_json_parsing():
    result = SimpleNamespace(stdout='{"tier": 1, "findings": []}', stderr='Warning on stderr', returncode=0)
    report = doctor.DoctorService(lambda: result).load()
    assert report.tier == 1
    assert 'Warning on stderr' in report.raw_output


def test_json_command_cannot_treat_non_json_success_as_healthy():
    result = SimpleNamespace(stdout='Unexpected proxy response', stderr='', returncode=0, doctor_json=True)
    with pytest.raises(doctor.DoctorError, match='Invalid Homebrew diagnostic JSON'):
        doctor.DoctorService(lambda: result).load()


# ── Block model ──────────────────────────────────────────────────────────────
# A brew doctor finding parses into typed blocks in document order. A renderer extracts a safe fix
# from a single runnable command block; everything else is prose, a data list, or a classified link.

def _issue(text):
    return doctor.parse_report(f'Warning: {text}', 1).issues[0]


def _jissue(**finding):
    finding.setdefault('tier', 1)
    finding.setdefault('affects', [])
    finding.setdefault('links', [])
    return doctor.parse_report(json.dumps({'findings': [finding], 'tier': 1}), 1).issues[0]


def test_command_block_is_runnable_only_for_a_single_brew_invocation():
    block = _issue('Fix it.\n  brew upgrade git\n').blocks[0]
    # The helper makes the first line the finding title, so the body is just the brew line, which opens the command block.
    assert block.type == 'command'
    assert block.value[0].display_command == 'brew upgrade git'
    assert block.value[0].arguments == ('upgrade', 'git')
    assert block.is_runnable is True
    assert block.runnable_step is block.value[0]
    assert block.runnable_step.arguments == ('upgrade', 'git')
    assert _issue('Fix it.\n  brew upgrade git\n').is_runnable is True


def test_multi_step_command_block_is_not_runnable():
    block = _issue('Fix it.\n  brew upgrade git\n  brew cleanup\n').blocks[0]
    assert block.type == 'command'
    assert len(block.value) == 2
    assert block.is_runnable is False
    assert block.runnable_step is None


def test_sudo_command_is_copy_only():
    block = _issue('Fix it.\n  sudo chown -R me /opt/homebrew\n').blocks[0]
    assert block.type == 'command'
    assert block.value[0].needs_admin is True
    assert block.value[0].arguments is None
    assert block.is_runnable is False
    assert block.runnable_step is None


def test_external_tool_command_is_copy_only():
    # Only a single brew invocation is ever submitted; an external tool is always copy-only.
    block = _issue('Fix it.\n  git -C /opt/homebrew stash\n').blocks[0]
    assert block.type == 'command'
    assert block.value[0].arguments is None
    assert block.is_runnable is False


def test_brew_line_with_shell_syntax_or_env_prefix_is_copy_only():
    for line in ('brew install foo && rm -rf ~', 'brew link --overwrite $(brew --prefix)/x',
                 'HOMEBREW_NO_INSTALL_FROM_API=1 brew update'):
        block = _issue(f'Fix it.\n  {line}\n').blocks[0]
        assert block.type == 'command', line
        assert block.value[0].arguments is None, line
        assert block.is_runnable is False, line


def test_stray_command_without_intro_opens_a_command_block():
    # A command with no caption intro in the body would be lost to prose; keep it so the fix is not dropped.
    issue = doctor.parse_report('Warning: Setup\nRun this first.\n  git -C /opt/homebrew stash\n', 1).issues[0]
    blocks = issue.blocks
    assert blocks[0].type == 'prose' and blocks[0].value == ('Run this first.',)
    assert blocks[1].type == 'command'
    assert blocks[1].value[0].display_command == 'git -C /opt/homebrew stash'


def test_blank_line_opens_a_new_command_block_with_its_own_caption():
    blocks = _issue('F:\n  mkdir -p /opt/homebrew\n\nThen:\n  brew tap homebrew/core\n').blocks
    assert len(blocks) == 2
    assert blocks[0].type == 'command' and blocks[0].caption == 'F:'
    assert blocks[1].type == 'command' and blocks[1].caption == 'Then:'


def test_data_block_groups_a_list_under_its_caption():
    issue = _issue('Tools at both paths:\n  openssl\n  pip3\n  python3\n')
    block = issue.blocks[0]
    assert block.type == 'data'
    assert block.caption == 'Tools at both paths:'
    assert block.value == ('openssl', 'pip3', 'python3')


def test_data_noun_cue_keeps_a_command_reading_first_item_as_data():
    # "tools exist at both paths:" names a list, so the first member "pip3" stays data, not a command.
    block = _issue('Tools exist at both paths:\n  pip3\n  python3\n').blocks[0]
    assert block.type == 'data'
    assert block.value == ('pip3', 'python3')


def test_links_are_classified_by_host():
    issue = _issue('Manual:\n  https://developer.apple.com/download/\n\nRef:\n  https://docs.brew.sh/Support-Tiers\n')
    manual = issue.blocks[0].value[0]
    ref = issue.blocks[1].value[0]
    assert manual == doctor.DoctorLink('https://developer.apple.com/download/', 'action')
    assert ref == doctor.DoctorLink('https://docs.brew.sh/Support-Tiers', 'reference')


def test_caption_folds_into_preceding_prose():
    # A caption not separated from the prose before it by a blank line is one continuous paragraph.
    blocks = _issue('Intro.\nFirst para.\nSteps:\n  brew cleanup\n').blocks
    assert blocks[0].type == 'prose' and blocks[0].value == ('First para.', 'Steps:')
    assert blocks[0].caption == ''
    assert blocks[1].type == 'command' and blocks[1].caption == ''


def test_json_remediation_command_becomes_runnable_block():
    issue = _jissue(text='kegs.', remediation={'commands': ['brew link openssl@3'], 'text': 'Re-link the keg.'})
    block = issue.runnable_block
    assert block.type == 'command'
    assert block.value[0].display_command == 'brew link openssl@3'
    assert block.value[0].arguments == ('link', 'openssl@3')
    assert block.is_runnable is True


def test_json_sudo_remediation_is_copy_only():
    issue = _jissue(text='kegs.', remediation={'commands': ['sudo chown -R me /opt/homebrew/Caskroom'], 'text': 'Fix ownership.'})
    assert issue.is_runnable is False
    command = [block for block in issue.blocks if block.type == 'command'][0]
    assert command.value[0].needs_admin is True
    assert command.value[0].arguments is None


def test_json_command_only_remediation_gets_its_own_caption():
    issue = _jissue(text='kegs.', remediation={'commands': ['brew cleanup'], 'text': ''})
    command = [block for block in issue.blocks if block.type == 'command'][0]
    assert command.caption == 'You can solve this by running:'
    assert command.value[0].arguments == ('cleanup',)
