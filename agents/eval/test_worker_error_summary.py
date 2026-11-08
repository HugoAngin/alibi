import json

from agents.eval.worker_error_summary import summarise


def test_dynamic_values_and_unknown_trace_content_never_export(tmp_path):
    secret = "DO_NOT_EXPORT_PRIVATE_EVENT"
    traces = [
        f'File "/tmp/source/alibi/connectors/self_supplied.py", line 126, in import_record\n'
        f'alibi.core.ingest.IngestError: import refused: event {secret} has a kind that is not a case kind\n',
        f'File "/tmp/source/alibi/core/ingest.py", line 402, in import_self_supplied\n'
        f'alibi.core.ingest.IngestError: payload failed schema validation: kind: Input should be {secret}; Extra inputs are not permitted\n',
        f'File "/tmp/source/alibi/core/ingest.py", line 402, in import_self_supplied\n'
        f'alibi.core.ingest.IngestError: {secret}\n',
        "",
    ]
    for number, trace in enumerate(traces):
        worker = tmp_path / "capture-workers" / str(number)
        worker.mkdir(parents=True)
        (worker / "metadata.json").write_text(json.dumps({"system": "rules", "case_number": number,
                                                         "failures": ["error"]}))
        (worker / "worker.stderr.log").write_text(trace)
    result = summarise(tmp_path)
    assert secret not in json.dumps(result)
    assert result["failed_groups"] == {"rules": 4}
    assert result["failed_groups_without_stderr"] == {"rules": 1}
    assert result["unclassified_groups"] == {"rules": 1}
    assert len(result["groups"]) == 2
