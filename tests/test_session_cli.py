from types import SimpleNamespace

import pytest

from vla_data_juicer_agents import session_cli


def test_session_cli_parser_has_no_no_llm_flag():
    parser = session_cli.build_parser()

    args = parser.parse_args(["--message", "处理 20270605 的导航数据", "--model", "qwen-plus"])

    assert args.message == "处理 20270605 的导航数据"
    assert args.model == "qwen-plus"
    assert not hasattr(args, "no_llm")


def test_session_cli_one_shot_uses_session_agent(monkeypatch, capsys):
    seen = {}

    class FakeSessionAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def handle_message(self, message):
            seen["message"] = message
            return SimpleNamespace(text="done", stop=False)

    monkeypatch.setattr(session_cli, "VLASessionAgent", FakeSessionAgent)

    code = session_cli.main(["--message", "处理 20270605 的导航数据", "--working-dir", ".djx-test"])

    assert code == 0
    assert seen["use_llm_router"] is True
    assert seen["working_dir"] == ".djx-test"
    assert seen["message"] == "处理 20270605 的导航数据"
    assert capsys.readouterr().out.strip() == "done"


def test_session_cli_rejects_empty_one_shot_message(capsys):
    code = session_cli.main(["--message", "   "])

    assert code == 2
    assert "message must not be empty" in capsys.readouterr().err


def test_pyproject_registers_session_entrypoint():
    text = open("pyproject.toml", encoding="utf-8").read()

    assert 'vla-data-agent = "vla_data_juicer_agents.session_cli:main"' in text
