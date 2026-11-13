import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import replay_pipeline as rp
import cvdp_generate as cg


def _prob(n, qs):
    return {"clusters": [{"id": i, "weight": 1, "passed": i == 0} for i in range(n)],
            "questions": qs}


def _q(i, j, label_i="A"):
    return {"qid": f"p:{i}-{j}", "ci": i, "cj": j, "label_i": label_i}


def test_single_cluster_accepted_without_questions():
    assert rp.decide(_prob(1, []), lambda q: None) == 0


def test_two_clusters_winner_accepted():
    p = _prob(2, [_q(0, 1, "B")])
    assert rp.decide(p, lambda q: rp._winner(q, "B")) == 0
    assert rp.decide(p, lambda q: rp._winner(q, "A")) == 1


def test_neither_and_split_defer():
    p = _prob(2, [_q(0, 1)])
    assert rp.decide(p, lambda q: rp._winner(q, "NEITHER")) is None
    assert rp.decide(p, lambda q: "split") is None
    assert rp.decide(p, lambda q: rp._winner(q, "TRUNCATED")) is None


def test_three_clusters_needs_clean_sweep():
    qs = [_q(0, 1), _q(0, 2), _q(1, 2)]
    p = _prob(3, qs)
    # 0 beats both
    assert rp.decide(p, lambda q: q["ci"] if q["ci"] == 0 else q["cj"]) == 0
    # cycle: 0>1, 2>0, 1>2 -> nobody sweeps
    cyc = {(0, 1): 0, (0, 2): 2, (1, 2): 1}
    assert rp.decide(p, lambda q: cyc[(q["ci"], q["cj"])]) is None


def test_missing_question_defers():
    p = _prob(3, [_q(0, 1)])               # 0-2 and 1-2 untestable
    assert rp.decide(p, lambda q: 0) is None


def test_cvdp_parse_single_and_multi():
    assert cg.parse_response("text\n```verilog\nmodule a; endmodule\n```", ["rtl/a.sv"]) == \
        {"rtl/a.sv": "module a; endmodule\n"}
    assert cg.parse_response("module a; endmodule", ["rtl/a.sv"]) == {"rtl/a.sv": "module a; endmodule"}
    multi = '{"code": [{"rtl/a.sv": "module a; endmodule"}, {"rtl/b.sv": "module b; endmodule"}]}'
    assert cg.parse_response(multi, ["rtl/a.sv", "rtl/b.sv"]) == \
        {"rtl/a.sv": "module a; endmodule", "rtl/b.sv": "module b; endmodule"}
    assert cg.parse_response("not json", ["rtl/a.sv", "rtl/b.sv"]) is None
    assert cg.strip_think("<think>x\ny</think>\nmodule") == "module"


def test_cvdp_prompt_matches_upstream_shape():
    s = cg.system_prompt(3, None)
    assert s.startswith("You are a helpful assistance.\nConsider that you have a folder structure like the following:\n\n")
    assert "Specification to RTL Translation" in s and "JSON" not in s
    assert "JSON schemas" in cg.system_prompt(2, cg.SCHEMA)


def test_three_judges_unanimous_and_majority(tmp_path):
    import json
    q = {"problem": "p", "spec": "s", "errors": 0,
         "clusters": [{"id": 0, "weight": 2, "passed": True, "mixed": False},
                      {"id": 1, "weight": 4, "passed": False, "mixed": False}],
         "questions": [{"qid": "p:0-1", "ci": 0, "cj": 1, "label_i": "A"}]}
    qf = tmp_path / "q.jsonl"; qf.write_text(json.dumps(q) + "\n")
    files = []
    for name, ans in (("j1", "A"), ("j2", "A"), ("j3", "B")):
        f = tmp_path / f"{name}.jsonl"; f.write_text(json.dumps({"qid": "p:0-1", "model": name, "answer": ans}) + "\n")
        files.append(str(f))
    import argparse, io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rp.cmd_score(argparse.Namespace(questions=str(qf), answers=files, out=None))
    text = out.getvalue()
    row = lambda key: next(l for l in text.splitlines() if l.startswith(f"| {key}"))
    assert row("all 3 judges agree").split("|")[2].strip().startswith("0")   # 2-1 split -> defer
    assert "1/1 (100.0%)" in row("majority of 3 judges")      # 2 of 3 pick the correct minority
