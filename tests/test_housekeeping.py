"""Small things that keep the service tidy: odd request paths, pruning, log size, offline `route`."""

import os
import socket
import time

from test_server import server  # noqa: F401  (reuse the live-listener fixture)

from ringrouter import cli
from ringrouter.config import EXAMPLE_CONFIG
from ringrouter.hubitat import Device
from ringrouter.llm import LLMStore, Reply
from ringrouter.logfile import trim_own_log
from ringrouter.store import Store


def raw_request(base, request_line: bytes) -> bytes:
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(request_line + b" HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        return s.recv(4096)


def test_non_ascii_paths_get_404_not_a_crash(server):  # noqa: F811
    base, _, _ = server
    assert b" 404 " in raw_request(base, b"POST /hook/caf\xe9")
    assert b" 404 " in raw_request(base, b"GET /activity/caf\xe9")
    assert b" 200 " in raw_request(base, b"GET /health")             # and the listener is still up


def test_prune_covers_confirmations_answers_and_conversations():
    store = Store(":memory:")
    llm = LLMStore(store)
    old = time.time() - 100 * 86400
    kept_pid = store.add_pending("d-new", "imessage", {})
    kept_aid = llm.save_answer("ask", "m", "new question", Reply("new answer"))
    llm.remember("m", "new question", "new answer")
    with store.lock:
        store.conn.execute("INSERT INTO pending VALUES('old', 'd-old', 'imessage', '{}', 'done', ?, ?)", (old, old))
        store.conn.execute("INSERT INTO answers VALUES('old', ?, 'ask', 'm', 'q', 'a', '[]', NULL)", (old,))
        store.conn.execute("INSERT INTO convo VALUES('m', 'user', 'old question', ?)", (old,))
    store.prune(90)
    llm.prune(90)
    with store.lock:
        pending = [r[0] for r in store.conn.execute("SELECT id FROM pending")]
        convo = [r[0] for r in store.conn.execute("SELECT content FROM convo")]
    assert pending == [kept_pid] and convo == ["new question", "new answer"]
    assert llm.get_answer("old") is None and llm.get_answer(kept_aid)["answer"] == "new answer"


def test_log_is_trimmed_in_place_and_appends_continue(tmp_path):
    path = tmp_path / "ring-router.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)      # how launchd opens it
    try:
        for i in range(400):
            os.write(fd, f"line {i:04d}\n".encode())
        assert trim_own_log(fd, max_bytes=2000, keep_bytes=500, only_under=tmp_path)
        os.write(fd, b"after\n")
    finally:
        os.close(fd)
    lines = path.read_text().splitlines()
    assert lines[0].startswith("--- log trimmed") and lines[1].startswith("line 03") and lines[-2:] == ["line 0399", "after"]
    assert path.stat().st_size < 700 and "\0" not in path.read_text()


def test_log_trim_leaves_other_files_alone(tmp_path):
    small, not_log, elsewhere = tmp_path / "small.log", tmp_path / "data.txt", tmp_path / "big.log"
    for p in (small, not_log, elsewhere):
        p.write_text("x\n" * (10 if p is small else 5000))
    for p, under in ((small, tmp_path), (not_log, tmp_path), (elsewhere, tmp_path / "somewhere-else")):
        fd = os.open(p, os.O_WRONLY | os.O_APPEND)
        try:
            assert not trim_own_log(fd, max_bytes=2000, keep_bytes=500, only_under=under)
        finally:
            os.close(fd)
    fd = os.open(elsewhere, os.O_WRONLY)                              # not append mode
    try:
        assert not trim_own_log(fd, max_bytes=2000, keep_bytes=500, only_under=tmp_path)
    finally:
        os.close(fd)
    assert elsewhere.stat().st_size == 10000


def test_route_command_does_not_call_cloud_models(monkeypatch, capsys):
    from ringrouter.llm import OpenRouter
    monkeypatch.setattr(OpenRouter, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cloud call")))
    cli.main(["--config", str(EXAMPLE_CONFIG), "route", "Ask ChatGPT what the capital of Australia is",
              "What's on my calendar Thursday?"])
    out = capsys.readouterr().out
    assert out.count("not run: this route calls a cloud model") == 2 and "[llm]" in out and "[agent]" in out


def test_plural_light_names_count_as_lights():
    for label in ("Kitchen Island Pendants", "Bedside Lamps", "Hall Sconces", "Porch Light", "Vanity Bulbs"):
        assert Device("1", label, "", {"Switch"}).is_light, label
    for label in ("Ceiling Fan", "Coffee Maker Outlet", "Front Door"):
        assert not Device("1", label, "", {"Switch"}).is_light, label
