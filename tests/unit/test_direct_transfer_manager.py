"""
Test the Direct transfer manager.
"""
from pathlib import Path

import pytest
from nxdrive.direct_transfer import DirectTransfer, DirectTransferManager


@pytest.fixture(scope="session")
def resources():
    return Path(__file__).parent.parent / "resources"


def test_no_paths():
    dt_manager = DirectTransferManager()
    assert repr(dt_manager)

    assert dt_manager.uid is None
    assert not dt_manager.transfers
    assert dt_manager.size == 0
    assert dt_manager.uploaded == 0
    assert dt_manager.progress == 0.0
    assert not dt_manager.is_completed


def test_one_path(tmp):
    path = tmp()
    path.write_text("0" * 1024)

    dt_manager = DirectTransferManager()
    dt_manager.add(path, "/remote/path")
    assert repr(dt_manager)

    transfers = dt_manager.transfers
    assert len(transfers) == 1
    transfer = transfers[0]
    assert isinstance(transfer, DirectTransfer)
    assert transfer.local_path == path
    assert transfer.remote_path == "/remote/path"
    assert transfer.is_file
    assert transfer.file_size == 1024
    assert transfer.uploaded_size == 0
    assert not transfer.uploaded
    assert transfer.chunk_size == 0
    assert transfer.batch_id is None
    assert transfer.mpu_id is None
    assert transfer.uid is None

    assert dt_manager.size == 1024
    assert dt_manager.uploaded == 0
    assert dt_manager.progress == 0.0
    assert not dt_manager.is_completed


def test_several_paths(resources):
    paths = [resources]
    dt_manager = DirectTransferManager()
    dt_manager.add_all(paths, "/remote/path")
    assert repr(dt_manager)
    transfers = dt_manager.transfers

    # No duplicate possible
    dt_manager.add_all(paths, "/remote/path")
    assert transfers == dt_manager.transfers

    transfers = dt_manager.transfers
    assert len(transfers) >= len(paths)

    # Ensure there is at least one folder in the list
    for transfer in dt_manager.transfers:
        if not transfer.is_file:
            break
    else:
        assert 0, "At least one folder is required."

    assert dt_manager.size > 0
    assert dt_manager.uploaded == 0
    assert dt_manager.progress == 0.0
    assert not dt_manager.is_completed


def test_partitions(resources):
    paths = [resources]
    dt_manager = DirectTransferManager()
    dt_manager.add_all(paths, "/remote/path")

    partitions = dt_manager.partitions
    assert isinstance(partitions, list)
    assert len(partitions) == len(dt_manager.transfers)

    partition_1 = partitions[0]
    assert len(partition_1) == 3
    assert isinstance(partition_1[0], str)
    assert isinstance(partition_1[1], bool)
    assert isinstance(partition_1[2], Path)


def test_start(resources):
    def func(*args, **kwargs):
        print(args, kwargs)

    paths = [resources]
    dt_manager = DirectTransferManager(func=func)
    dt_manager.add_all(paths, "/remote/path")

    dt_manager.start()

    assert 0
