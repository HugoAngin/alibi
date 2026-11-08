from pathlib import Path
import pytest
from agents.eval.replay_pinned_capture import replay_command


def test_replay_accepts_only_original_capture_module_and_output(tmp_path):
    capture = tmp_path / 'capture'
    prefix = 'env ALIBI_EVAL_ENV_FILE=/private/config uv run --frozen python -m agents.eval.run_sealed '
    record = {'command': prefix + '--candidate-ref sealed-candidate-5 --out ' + str(capture)}
    credential, args = replay_command(record, capture)
    assert credential == '/private/config'
    assert args[-1] == str(capture)
    with pytest.raises(ValueError):
        replay_command(record, tmp_path / 'different')
    record['command'] = record['command'].replace('agents.eval.run_sealed', 'other.module')
    with pytest.raises(ValueError):
        replay_command(record, capture)
