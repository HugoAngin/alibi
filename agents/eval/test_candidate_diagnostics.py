import json

from agents.eval.candidate_diagnostics import classify_failure, from_stderr


def test_import_error_is_typed_without_revealing_message_or_case_values(tmp_path):
    secret = "PRIVATE_CASE_VALUE_DO_NOT_EXPORT"
    text = (f'File "/private/{secret}/alibi/connectors/self_supplied.py", line 126, in import_record\n'
            f'alibi.core.ingest.IngestError: import refused: event {secret} has a kind that is not a case kind\n')
    path = tmp_path / "stderr"
    path.write_text(text)
    result = from_stderr(path, failure="error", returncode=1)
    assert result == {"stage": "import_mapping", "code": "self_supplied_import_refused",
                      "exception": "IngestError", "child_returncode": 1}
    assert secret not in json.dumps(result)


def test_timeout_is_not_misclassified_by_a_previous_traceback():
    result = classify_failure("ValueError: private payload", failure="timeout", returncode=-9)
    assert result["stage"] == "deadline" and result["exception"] == "unknown"


def test_unknown_exception_and_frame_names_are_not_exported():
    result = classify_failure('File "/alibi/PRIVATE.py", line 1, in PRIVATE\nPRIVATEError: PRIVATE\n',
                              failure="error", returncode=1)
    assert "PRIVATE" not in json.dumps(result)
    assert result["code"] == "candidate_process_exit"
