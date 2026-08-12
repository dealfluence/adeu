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


def test_cli_subcommand_serve_wiring(monkeypatch, capsys):
    lines = [
        json.dumps({"command": "ping"}),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    monkeypatch.setattr("sys.argv", ["adeu", "serve"])

    from adeu.cli import main

    try:
        main()
    except SystemExit as e:
        assert e.code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 1
    assert json.loads(captured[0]) == {"status": "ok", "pong": True}


def test_daemon_survives_write_error(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    bad_dir = tmp_path / "nonexistent_parent_dir" / "sub"
    bad_output = bad_dir / "output.docx"

    # Make the parent path a file so mkdir/write fails with OSError
    blocker_file = tmp_path / "nonexistent_parent_dir"
    blocker_file.write_text("i am a file, not a directory")

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": [{"type": "modify", "target_text": "document", "new_text": "modified"}],
                "output": str(bad_output),
            }
        ),
        json.dumps({"command": "ping"}),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    err_res = json.loads(captured[0])
    assert err_res.get("status") == "error"
    # The write goes through the CLI's _write_output_or_exit, which signals
    # failure with SystemExit: the daemon must report that helper's structured
    # code and diagnostics, not the stringified exit status ("Error: 1").
    assert err_res.get("error") == "write_failed"
    assert "Could not write output file" in err_res.get("message", "")

    ping_res = json.loads(captured[1])
    assert ping_res == {"status": "ok", "pong": True}


def test_serve_response_budget_guard(monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    monkeypatch.setenv("ADEU_MAX_RESPONSE_CHARS", "10")

    lines = [
        json.dumps(
            {
                "command": "extract",
                "file_path": str(fixture_path),
                "page": "all",
            }
        ),
        json.dumps(
            {
                "command": "extract",
                "file_path": str(fixture_path),
                "page": "all",
                "force": True,
            }
        ),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    res1 = json.loads(captured[0])
    assert res1.get("status") == "error"
    assert res1.get("error") == "response_budget_exceeded"

    res2 = json.loads(captured[1])
    assert res2.get("title") == "golden.docx"
    assert "markdown" in res2


def test_serve_apply_zero_edits_applied(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    out_docx = tmp_path / "should_not_exist.docx"

    # A non-empty batch whose every edit fails is a failed batch, even under
    # --partial: nothing was applied, so nothing may be written.
    changes = [{"type": "modify", "target_text": "nonexistent text 123456", "new_text": "foo"}]

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": changes,
                "output": str(out_docx),
                "partial": True,
            }
        ),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 1

    res = json.loads(captured[0])
    assert res.get("status") == "error"
    assert res.get("output_path") is None
    assert not out_docx.exists()


def test_serve_apply_empty_batch_saves_an_unmodified_copy(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    out_docx = tmp_path / "copy.docx"

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": [],
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

    # `adeu apply` with a 0-change batch writes an unmodified copy and succeeds;
    # the daemon must not turn "nothing applied" into a failure.
    res = json.loads(captured[0])
    assert res.get("status") == "ok"
    assert res.get("output_path") == str(out_docx)
    assert out_docx.exists()


def test_serve_bad_changes_file_returns_the_structured_cli_code(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    changes_file = tmp_path / "batch.json"
    changes_file.write_text('{"edits": [], "actions": []}', encoding="utf-8")

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": str(changes_file),
                "output": str(tmp_path / "out.docx"),
            }
        ),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 1

    # _load_batch_from_json rejects the batch via _cli_error → SystemExit. The
    # daemon must surface that code and prose, not "Error: 1".
    res = json.loads(captured[0])
    assert res.get("status") == "error"
    assert res.get("error") == "invalid_changes_file"
    assert res.get("output_path") is None
    assert "pre-v1.1.0" in res.get("message", "")


def test_serve_changes_mode_page_all_skips_the_budget_guard(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    redlined = tmp_path / "redlined.docx"
    monkeypatch.setenv("ADEU_MAX_RESPONSE_CHARS", "10")

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": [{"type": "modify", "target_text": "document", "new_text": "record"}],
                "output": str(redlined),
            }
        ),
        json.dumps(
            {
                "command": "extract",
                "file_path": str(redlined),
                "mode": "changes",
                "page": "all",
            }
        ),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    apply_res = json.loads(captured[0])
    assert apply_res.get("edits_applied") == 1

    # The ledger is a bounded summary, so `--mode changes` is exempt from the
    # response budget guard (cli.py checks it only on the want_all_pages path).
    res = json.loads(captured[1])
    assert res.get("error") is None
    assert res.get("title") == "redlined.docx"
    assert "Chg:" in res.get("markdown", "")


def test_serve_apply_refuses_non_docx_output(tmp_path, monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")
    out_txt = tmp_path / "out.txt"

    lines = [
        json.dumps(
            {
                "command": "apply",
                "file_path": str(fixture_path),
                "changes": [{"type": "modify", "target_text": "document", "new_text": "modified"}],
                "output": str(out_txt),
            }
        ),
        json.dumps({"command": "ping"}),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2

    # `adeu apply` refuses a non-.docx output via _require_docx_output; the
    # daemon must apply the same gate and write nothing.
    res = json.loads(captured[0])
    assert res.get("status") == "error"
    assert res.get("error") == "invalid_input"
    assert "must end in .docx" in res.get("message", "")
    assert res.get("output_path") is None
    assert not out_txt.exists()

    assert json.loads(captured[1]) == {"status": "ok", "pong": True}


def test_serve_outline_warm_read_does_not_reparse_the_docx(monkeypatch, capsys):
    fixture_path = get_fixture_path("golden.docx")

    from adeu.mcp_components import doc_cache as doc_cache_module

    doc_cache_module.doc_cache.clear()

    parses: list = []
    real_load = doc_cache_module.DocProjectionCache._load_document

    def counting_load(path, cb=None):
        parses.append(str(path))
        return real_load(path, cb)

    monkeypatch.setattr(doc_cache_module.DocProjectionCache, "_load_document", staticmethod(counting_load))

    def refuse_load(path):
        parses.append(f"serve:{path}")
        raise AssertionError(f"outline re-parsed {path}")

    monkeypatch.setattr("adeu.serve._load_docx_or_exit", refuse_load)

    lines = [
        json.dumps({"command": "extract", "file_path": str(fixture_path), "mode": "outline"}),
        json.dumps({"command": "extract", "file_path": str(fixture_path), "mode": "outline"}),
        json.dumps({"command": "exit"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    code = run_serve()
    assert code == 0

    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2
    assert json.loads(captured[0]) == json.loads(captured[1])
    assert json.loads(captured[0]).get("error") is None
    assert json.loads(captured[0]).get("title") == "golden.docx"

    # One parse total — the cold doc_cache fill, which computes the outline
    # nodes itself. Rendering the outline consults the cached nodes only, so
    # neither the cold nor the warm read may open the DOCX again.
    assert parses == [str(fixture_path.resolve())]
