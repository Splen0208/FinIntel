import subprocess
from pathlib import Path
from test_frontend_contract import _node, SIMPLE_JS


def test_home_race_and_submit_handler():
    harness = Path(__file__).with_name('frontend_home_contract.mjs')
    result = subprocess.run([_node(), str(harness), str(SIMPLE_JS)], capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
