from app import evaluator
from app.models import SubmissionReport, Transcript
from tests.conftest import FakeParseResponse
from tests.test_evaluator import clear_answer, make_rubric, model_evaluation


def test_dispositions_change_prompt_not_rubric(fake_client_factory):
    rubric = make_rubric()
    lenient = evaluator._grading_context(rubric, strictness=0)
    strict = evaluator._grading_context(rubric, strictness=2)

    # invariant rules and rubric block identical at every level
    assert lenient[0]["text"] == strict[0]["text"]
    assert lenient[-1]["text"] == strict[-1]["text"]
    assert lenient[-1]["cache_control"] == {"type": "ephemeral"}
    # only the disposition block differs
    assert "BOARD STANDARD" in lenient[1]["text"]
    assert "benefit of the doubt" in lenient[1]["text"].lower()
    assert "STRICT" in strict[1]["text"]
    assert "directive verb" in strict[1]["text"]


def test_strictness_flows_into_evaluation_call(fake_client_factory):
    client = fake_client_factory([FakeParseResponse(model_evaluation())])
    transcript = Transcript(answers=[clear_answer()])

    evaluator.evaluate_submission(client, make_rubric(), transcript, strictness=2)

    system = client.messages.calls[0]["system"]
    assert any("STRICT" in block["text"] for block in system)


def test_unknown_strictness_falls_back_to_board_standard():
    context = evaluator._grading_context(make_rubric(), strictness=99)
    assert "BOARD STANDARD" in context[1]["text"]


def test_report_discloses_strictness():
    rubric = make_rubric()
    report = SubmissionReport.build(rubric, Transcript(answers=[]), [], strictness=2)
    assert report.strictness == 2
    # default stays lenient for callers that don't pass it
    assert SubmissionReport.build(rubric, Transcript(answers=[]), []).strictness == 0
