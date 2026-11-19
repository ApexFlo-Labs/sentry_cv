"""Archived command parsing only: never starts a model or camera."""
import importlib.util
from pathlib import Path

import pytest

SPEC=importlib.util.spec_from_file_location('pixel_parity',Path(__file__).with_name('pixel_conversion_parity.py'))
helper=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(helper)


@pytest.mark.parametrize('envelope',[False,True])
def test_archived_argv_and_command_envelope_are_copied(envelope):
    command=['python3','guard.py','--case','pixel','--','probe.py','--image','sha256:test','--steady-seconds','20']
    result=helper.recorded_command({'command':command} if envelope else command)
    assert result==command and result is not command


@pytest.mark.parametrize('record',[{}, {'command':'not argv'}, [], [1],
    ['python3','probe.py'], {'command':None}])
def test_invalid_or_incomplete_command_records_fail_before_launch(record):
    with pytest.raises(ValueError):helper.recorded_command(record)
