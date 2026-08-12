import io
import json
from pathlib import Path

from adeu.serve import run_serve


def get_fixture_path(name: str) -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "shared" / "fixtures").is_dir():
            return parent / "shared" / "fixtures" / name
    raise FileNotFoundError(f"Could not find fixtures directory for {name}")


def test_ping_and_extract_over_one_session(monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    lines = [
        json.dumps({"command": "ping"}),
        json.dumps({"command": "extract", "file_path": str(fixture_path)}),
        json.dumps({"command": "exit"}),
    ]
    input_data = "\n".join(lines) + "\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(input_data))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    ping_res = json.loads(captured[0])
    assert ping_res == {"status": "ok", "pong": True}

    extract_res = json.loads(captured[1])
    assert extract_res.get("title") == "golden.docx"
    assert "markdown" in extract_res


def test_malformed_line_does_not_kill_the_daemon(monkeypatch, capsys):
    lines = [
        "this is not valid json",
        json.dumps({"command": "ping"}),
        json.dumps({"command": "exit"}),
    ]
    input_data = "\n".join(lines) + "\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(input_data))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    err_res = json.loads(captured[0])
    assert err_res.get("status") == "error"
    assert err_res.get("error") == "invalid_input"
    assert "message" in err_res

    ping_res = json.loads(captured[1])
    assert ping_res == {"status": "ok", "pong": True}


def test_unknown_command_and_missing_file_use_the_cli_error_codes(monkeypatch, capsys):
    lines = [
        json.dumps({"command": "nonexistent_command"}),
        json.dumps({"command": "extract", "file_path": "non_existent_file.docx"}),
        json.dumps({"command": "exit"}),
    ]
    input_data = "\n".join(lines) + "\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(input_data))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    res1 = json.loads(captured[0])
    assert res1.get("status") == "error"
    assert res1.get("error") == "invalid_input"

    res2 = json.loads(captured[1])
    assert res2.get("status") == "error"
    assert res2.get("error") == "file_not_found"


def test_serve_output_matches_one_shot_cli(monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")

    # Run daemon extract
    lines = [
        json.dumps({"command": "extract", "file_path": str(fixture_path)}),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    run_serve()
    serve_out = capsys.readouterr().out.strip()

    # Run one-shot CLI extract --json
    from adeu.cli import main

    monkeypatch.setattr("sys.argv", ["adeu", "extract", str(fixture_path), "--json"])
    try:
        main()
    except SystemExit:
        pass
    cli_out = capsys.readouterr().out.strip()

    assert json.loads(serve_out) == json.loads(cli_out)


def test_apply_over_serve_writes_the_document(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    out_docx = tmp_path / "modified.docx"

    changes = [{"type": "modify", "target_text": "document", "new_text": "modified document"}]

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": changes,
                "output": str(out_docx),
            }
        ),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 1

    apply_res = json.loads(captured[0])
    assert apply_res.get("edits_applied") == 1
    assert out_docx.exists()
