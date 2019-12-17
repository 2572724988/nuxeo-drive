"""
The Direct Transfer feature.
"""
from collections import deque
from dataclasses import dataclass
from logging import getLogger
from operator import attrgetter
from pathlib import Path
from typing import List, Optional, Set, TYPE_CHECKING

from nuxeo.models import Document

from .exceptions import DirectTransferDuplicateFoundError
from .utils import get_tree_list

if TYPE_CHECKING:
    from .client.remote_client import Remote  # noqa


log = getLogger(__name__)


@dataclass
class DirectTransfer:
    """A Direct Transfer item."""

    local_path: Path
    remote_path: str
    is_file: bool
    file_size: int = 0
    remote_ref: Optional[str] = None
    # status: TransferStatus
    uploaded_size: int = 0
    uploaded: bool = False
    chunk_size: int = 0
    replace_blob: bool = False
    batch_id: Optional[str] = None
    mpu_id: Optional[str] = None
    uid: Optional[int] = None


class DirectTransferManager:
    """Direct Transfer manager.
    This is the core of the feature, separated from Nuxeo Drive.
    It is used to create a new Direct Transfer, keep track of the progression,
    pause/resume transfers and it is connected to a (beautiful) custom QML component.
    """

    def __init__(
        self, remote: Optional["Remote"] = None, uid: Optional[int] = None
    ) -> None:
        self.remote = remote
        self.uid = uid
        self.engine_uid = uid

        self.is_started = False
        self.transfers: List[DirectTransfer] = []

        # The thread-safe queue
        self._iterator: deque = deque()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}<uid={self.uid!r}"
            f", paths={len(self.transfers)}"
            f", is_completed={self.is_completed!r}"
            f", is_started={self.is_started!r}"
            f", progress={self.progress:.1f}"
            f", size={self.size}"
            f", uploaded={self.uploaded}"
            ">"
        )

    @property
    def is_completed(self) -> bool:
        """Return True if all transfers are done."""
        return self.progress >= 100.0

    @property
    def partitions(self) -> List[DirectTransfer]:
        """Return a list of paths to upload, sorted by remote path > file type > local path."""
        return sorted(
            self.transfers,
            key=(
                attrgetter("remote_path"),
                attrgetter("is_file"),
                attrgetter("local_path"),
            ),
        )

    @property
    def progress(self) -> float:
        """Overall progression."""
        try:
            return self.uploaded * 100 / self.size
        except ZeroDivisionError:
            return 0.0

    @property
    def size(self) -> int:
        """Overall files size to upload."""
        return sum(t.file_size for t in self.transfers)

    @property
    def uploaded(self) -> int:
        """Overall uploaded size."""
        return sum(t.uploaded_size for t in self.transfers)

    def add(self, local_path: Path, remote_path: str) -> Optional[DirectTransfer]:
        """Add a *local_path* to upload inside the given *remote_path*.
        Return the DirectTransfer item.
        """
        if self.already_added(local_path):
            log.debug(f"{local_path!r} is already part of the upload list")
            return None

        try:
            is_file = local_path.is_file()
            size = local_path.stat().st_size if is_file else 0
        except OSError:
            log.warning(f"Skipping errored {local_path!r}", exc_info=True)
            return None

        transfer = DirectTransfer(local_path, remote_path, is_file, size)
        self.transfers.append(transfer)
        return transfer

    def add_all(self, local_paths: Set[Path], remote_path: str) -> None:
        """Recursively add *local_paths* inside the given *remote_path*."""
        for local_path in sorted(local_paths):
            self.add(local_path, remote_path)

            if local_path.is_dir():
                tree = get_tree_list(local_path, remote_path)
                for computed_remote_path, path in sorted(tree):
                    self.add(path, computed_remote_path)

    def already_added(self, local_path: Path) -> bool:
        """Return True if a given *local_path* is already planned for upload."""
        return bool(self.transfers) and any(
            t.local_path == local_path for t in self.transfers
        )

    def __iter__(self) -> "DirectTransferManager":
        """The whole class is an iterator.
        It is used to io iterate over transfer to handle:

            >>> for transfer in self:
            ...     upload(transfer)

        """
        # Add transfers into the queue
        self._iterator.clear()
        self._iterator.extend(self.partitions)

        return self

    def __next__(self) -> DirectTransfer:
        """Get the next transfer to handle for a transfer. See __iter__()."""
        try:
            return self._iterator.popleft()
        except IndexError:
            raise StopIteration()

    def reset(self, uid: Optional[int] = None):
        """Reset data."""
        if self.is_started:
            log.warning("Cannot reset as there are ongoing transfers")
            return

        self._iterator.clear()
        self.uid = uid
        self.transfers = []

    def start(self):
        """Start transfers."""
        if self.is_started:
            log.warning("Direct Transfer already running ... ")
            return

        if not self.transfers:
            log.info("Nothing to transfer")
            return

        self.is_started = True
        try:
            for transfer in self:
                self.upload(transfer, self.engine_uid)
        finally:
            self.is_started = False

    def stop(self):
        """Stop transfers."""
        if not self.is_started:
            return

        self.is_started = False

    def upload(self, transfer: DirectTransfer, engine_uid: str):
        """Upload a given file to the given folderish document on the server.

        Note about possible duplicate creation via a race condition client <-> server.
        Given the local *file* with the path "$HOME/some-folder/subfolder/file.odt",
        the file name is "file.odt".

        Scenario:
            - Step 1: local, check for a doc with the path name "file.odt" => nothing returned, continuing;
            - Step 2: server, a document with a path name set to "file.odt" is created;
            - Step 3: local, create the document with the path name "file.odt".

        Even if the elapsed time between steps 1 and 3 is really short, it may happen.

        What can be done to prevent such scenario is not on the Nuxeo Drive side but on the server one.
        For now, 2 options are possible but not qualified to be done in a near future:
            - https://jira.nuxeo.com/browse/NXP-22959;
            - create a new operation `Document.GetOrCreate` that ensures atomicity.
        """
        local_path = transfer.local_path
        remote_path = transfer.remote_path
        name = local_path.name
        is_file = local_path.is_file()
        doc: Optional[Document] = None

        log.info(f"Direct Transfer of {local_path!r} into {remote_path!r}")

        if transfer.remote_ref:
            # The remote ref is set, so it means either the file has already been uploaded,
            # either a previous upload failed: the document was created, or not, and it has
            # a blob attached, or not. In any cases, we need to ensure the user can upload
            # without headhache.
            doc = self.remote.get_document_or_none(uid=transfer.remote_ref)

        if not doc:
            # We need to handle possbile duplicates based on the file name and
            # the destination folder on the server.
            # Note: using this way may still result in duplicates:
            #  - the user created 2 documents with the same name on Web-UI or another way
            #  - the user then deleted the 1st document
            #  - the other document has a path like "name.TIMESTAMP"
            # So then Drive will not see that document as a duplicate because it will check
            # a path with "name" only.

            # If we really want to avoid that situation, we should use that commented code:
            """
            # Note that it would be too much effort for the server, we do not want that!
            for child in self.documents.get_children(path=parent_path):
                # It is OK to have a folder and a file with the same name,
                # but not 2 files or 2 folders with the same name
                if child.title == file.name:
                    local_is_dir = file.isdir()
                    remote_is_dir = "Folderish" in child["facets"]
                    if local_is_dir is remote_is_dir:
                        # Duplicate found!
                        doc = child
                        transfer.remote_ref = doc.uid
                        break
            """

            doc = self.remote.get_document_or_none(path=f"{remote_path}/{name}")
            if doc:
                transfer.remote_ref = doc.uid

        if not transfer.replace_blob and doc and doc.properties.get("file:content"):
            # The document already exists and has a blob attached. Ask the user what to do.
            raise DirectTransferDuplicateFoundError(local_path, doc)

        if not doc:
            # Create the document on the server
            nature = "File" if is_file else "Folder"
            doc = self.documents.create(
                Document(name=name, type=nature, properties={"dc:title": name}),
                parent_path=remote_path,
            )
            transfer.remote_ref = doc.uid

        # If the path is a folder, there is no more work to do
        if not is_file:
            return

        # Save the transfer in the database remote UID in case next steps fails
        transfer.remote_ref

        # Upload the blob and attach it to the document
        self.remote.upload(
            local_path,
            engine_uid=engine_uid,
            document=transfer.remote_ref,
            command="Blob.AttachOnDocument",
            xpath="file:content",
            void_op=True,
        )
