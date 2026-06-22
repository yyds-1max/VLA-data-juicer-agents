from vla_data_juicer_agents import session_cli


def test_session_cli_parser_has_no_no_llm_flag():
    parser = session_cli.build_parser()

    args = parser.parse_args(["--message", "处理 20270605 的导航数据", "--model", "qwen-plus"])

    assert args.message == "处理 20270605 的导航数据"
    assert args.model == "qwen-plus"
    assert not hasattr(args, "no_llm")


def test_session_cli_one_shot_delegates_to_tui(monkeypatch):
    seen = {}

    def fake_run_tui_session(args):
        seen["message"] = args.message
        seen["working_dir"] = args.working_dir
        seen["model"] = args.model
        return 17

    monkeypatch.setattr(session_cli, "run_tui_session", fake_run_tui_session)

    code = session_cli.main(["--message", "处理 20270605 的导航数据", "--working-dir", ".djx-test"])

    assert code == 17
    assert seen["working_dir"] == ".djx-test"
    assert seen["message"] == "处理 20270605 的导航数据"


def test_session_cli_interactive_delegates_to_tui(monkeypatch):
    seen = {}

    def fake_run_tui_session(args):
        seen["message"] = args.message
        seen["working_dir"] = args.working_dir
        return 23

    monkeypatch.setattr(session_cli, "run_tui_session", fake_run_tui_session)

    code = session_cli.main(["--working-dir", ".djx-interactive"])

    assert code == 23
    assert seen == {"message": None, "working_dir": ".djx-interactive"}


def test_session_cli_rejects_empty_one_shot_message(capsys):
    code = session_cli.main(["--message", "   "])

    assert code == 2
    assert "message must not be empty" in capsys.readouterr().err


def test_pyproject_registers_session_entrypoint():
    text = open("pyproject.toml", encoding="utf-8").read()

    assert 'vla-data-agent = "vla_data_juicer_agents.session_cli:main"' in text
