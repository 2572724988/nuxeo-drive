# coding: utf-8
import calendar
import json
from datetime import datetime
from logging import getLogger
from os import getenv
from time import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from dateutil.tz import tzlocal
from nuxeo.exceptions import HTTPError, Unauthorized
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QMessageBox

from ..client.proxy import get_proxy
from ..constants import APP_NAME, STARTUP_PAGE_CONNECTION_TIMEOUT, TOKEN_PERMISSION
from ..engine.activity import Action, FileAction
from ..engine.engine import Engine
from ..engine.workers import Worker
from ..exceptions import (
    FolderAlreadyUsed,
    InvalidDriveException,
    InvalidSSLCertificate,
    NotFound,
    RootAlreadyBindWithDifferentAccount,
    StartupPageConnectionError,
)
from ..notification import Notification
from ..objects import Binder, DocPair
from ..options import Options
from ..translator import Translator
from ..utils import get_device, get_default_nuxeo_drive_folder, guess_server_url

if TYPE_CHECKING:
    from .application import Application  # noqa

__all__ = ("QMLDriveApi",)

log = getLogger(__name__)


class QMLDriveApi(QObject):

    openAuthenticationDialog = pyqtSignal(str, object)
    setMessage = pyqtSignal(str, str)

    def __init__(self, application: "Application") -> None:
        super().__init__()
        self._manager = application.manager
        self.application = application
        self._callback_params: Dict[str, str] = {}

        # Attributes for the web authentication feedback
        self.openAuthenticationDialog.connect(
            self.application._open_authentication_dialog
        )

    def _json_default(self, obj: Any) -> Any:
        if isinstance(obj, Action):
            return self._export_action(obj)
        if isinstance(obj, Engine):
            return self._export_engine(obj)
        if isinstance(obj, Notification):
            return self._export_notification(obj)
        if isinstance(obj, DocPair):
            return self._export_state(obj)
        if isinstance(obj, Worker):
            return self._export_worker(obj)
        return obj

    def _json(self, obj: Any) -> Any:
        # Avoid to fail on non serializable object
        return json.dumps(obj, default=self._json_default)

    def _export_engine(self, engine: Engine) -> Dict[str, Any]:
        if not engine:
            return {}

        bind = engine.get_binder()
        return {
            "uid": engine.uid,
            "type": engine.type,
            "name": engine.name,
            "offline": engine.is_offline(),
            "metrics": engine.get_metrics(),
            "started": engine.is_started(),
            "syncing": engine.is_syncing(),
            "paused": engine.is_paused(),
            "local_folder": engine.local_folder,
            "queue": engine.get_queue_manager().get_metrics(),
            "web_authentication": bind.web_authentication,
            "server_url": bind.server_url,
            "default_ui": engine.wui,
            "ui": engine.force_ui or engine.wui,
            "username": bind.username,
            "need_password_update": bind.pwd_update_required,
            "initialized": bind.initialized,
            "server_version": bind.server_version,
            "threads": self._get_threads(engine),
        }

    def get_date_from_sqlite(self, d: str) -> Optional[datetime]:
        format_date = "%Y-%m-%d %H:%M:%S"
        try:
            return datetime.strptime(str(d.split(".")[0]), format_date)
        except BaseException:
            return None

    def get_timestamp_from_date(self, d: datetime = None) -> int:
        if not d:
            return 0
        return int(calendar.timegm(d.timetuple()))

    def _export_state(self, state: DocPair = None) -> Dict[str, Any]:
        if state is None:
            return {}

        result: Dict[str, Any] = dict(
            state=state.pair_state, last_sync_date="", last_sync_direction="upload"
        )

        # Last sync in sec
        current_time = int(time())
        date_time = self.get_date_from_sqlite(state.last_sync_date)
        sync_time = self.get_timestamp_from_date(date_time)
        if state.last_local_updated or "" > state.last_remote_updated or "":
            result["last_sync_direction"] = "download"
        result["last_sync"] = current_time - sync_time
        if date_time:
            # As date_time is in UTC
            result["last_sync_date"] = Translator.format_datetime(
                date_time + tzlocal()._dst_offset  # type: ignore
            )

        result["name"] = state.local_name
        if state.local_name is None:
            result["name"] = state.remote_name
        result["remote_name"] = state.remote_name
        result["last_error"] = state.last_error
        result["local_path"] = state.local_path
        result["local_parent_path"] = state.local_parent_path
        result["remote_ref"] = state.remote_ref
        result["folderish"] = state.folderish
        result["last_transfer"] = state.last_transfer
        if result["last_transfer"] is None:
            result["last_transfer"] = result["last_sync_direction"]
        result["id"] = state.id
        return result

    def _export_formatted_state(
        self, uid: str, state: DocPair = None
    ) -> Dict[str, Any]:
        engine = self._get_engine(uid)
        if not state or not engine:
            return {}

        result = self._export_state(state)
        result["last_contributor"] = (
            ""
            if state.last_remote_modifier is None
            else engine.get_user_full_name(state.last_remote_modifier, cache_only=True)
        )
        date_time = self.get_date_from_sqlite(state.last_remote_updated)
        result["last_remote_update"] = (
            Translator.format_datetime(date_time) if date_time else ""
        )
        date_time = self.get_date_from_sqlite(state.last_local_updated)
        result["last_local_update"] = (
            Translator.format_datetime(date_time) if date_time else ""
        )
        result["remote_can_update"] = state.remote_can_update
        result["remote_can_rename"] = state.remote_can_rename
        result["last_error_details"] = state.last_error_details or ""
        return result

    def _export_action(self, action: Action) -> Dict[str, Any]:
        result: Dict[str, Any] = dict()
        result["name"] = action.type
        percent = action.get_percent()
        if percent:
            result["percent"] = percent
        if isinstance(action, FileAction):
            result["size"] = action.size
            result["filename"] = action.filename
            result["filepath"] = action.filepath
        return result

    def _export_worker(self, worker: Worker) -> Dict[str, Any]:
        result: Dict[str, Any] = dict()
        action = worker.action
        if action is None:
            result["action"] = None
        else:
            result["action"] = self._export_action(action)
        result["thread_id"] = worker._thread_id
        result["name"] = worker._name
        result["paused"] = worker.is_paused()
        result["started"] = worker.is_started()
        return result

    def _get_threads(self, engine: Engine) -> List[Dict[str, Any]]:
        result = []
        for thread in engine.get_threads():
            result.append(self._export_worker(thread.worker))
        return result

    def _get_engine(self, uid: str) -> Optional[Engine]:
        engines = self._manager.get_engines()
        return engines.get(uid)

    def get_last_files(
        self, uid: str, number: int, direction: str, duration: int = None
    ) -> List[Dict[str, Any]]:
        """ Return the last files transferred (see EngineDAO). """
        engine = self._get_engine(uid)
        result = []
        if engine is not None:
            for state in engine.get_last_files(number, direction, duration):
                result.append(self._export_state(state))
        return result

    @pyqtSlot(str, result=int)
    def get_last_files_count(self, uid: str) -> int:
        """ Return the count of the last files transferred (see EngineDAO). """
        count = 0
        engine = self._get_engine(uid)
        if engine:
            count = engine.get_last_files_count(direction="", duration=60)
        return count

    @pyqtSlot(result=str)
    def get_tracker_id(self) -> str:
        return self._manager.get_tracker_id()

    @pyqtSlot(str)
    def set_language(self, locale: str) -> None:
        try:
            Translator.set(locale)
        except RuntimeError:
            log.exception("Set language error")

    @pyqtSlot(str)
    def trigger_notification(self, id_: str) -> None:
        self.application.hide_systray()
        self._manager.notification_service.trigger_notification(id_)

    @pyqtSlot(str)
    def discard_notification(self, id_) -> None:
        self._manager.notification_service.discard_notification(id_)

    @staticmethod
    def _export_notification(notif: Notification) -> Dict[str, Any]:
        return {
            "level": notif.level,
            "uid": notif.uid,
            "title": notif.title,
            "description": notif.description,
            "discardable": notif.is_discardable(),
            "discard": notif.is_discard(),
            "systray": notif.is_systray(),
            "replacements": notif.get_replacements(),
        }

    def _export_notifications(
        self, notifs: Dict[str, Notification]
    ) -> List[Dict[str, Any]]:
        return [self._export_notification(notif) for notif in notifs.values()]

    @pyqtSlot(str, result=str)
    def get_notifications(self, engine_uid: str) -> str:
        engine_uid = engine_uid
        center = self._manager.notification_service
        notif = self._export_notifications(center.get_notifications(engine_uid))
        return self._json(notif)

    @pyqtSlot(result=str)
    def get_update_channel(self) -> str:
        """ Return the channel of the update. """
        return self._manager.get_update_channel()

    @pyqtSlot(result=str)
    def get_update_status(self) -> str:
        """ Return the status of the update. """
        return self._manager.updater.status

    @pyqtSlot(result=str)
    def get_update_version(self) -> str:
        """ Return the version of the update, if one is available. """
        return self._manager.updater.version

    @pyqtSlot(result=int)
    def get_update_progress(self) -> float:
        """ Return the progress of the update, if one is ingoing. """
        return self._manager.updater.progress

    @pyqtSlot(str)
    def app_update(self, version: str) -> None:
        """ Start the udpate to the specified version. """
        self._manager.updater.update(version)

    @pyqtSlot(str, result=str)
    def get_actions(self, uid: str) -> str:
        engine = self._get_engine(uid)
        result = []
        if engine:
            for count, thread in enumerate(engine.get_threads(), 1):
                action = thread.worker.action
                # The filter should be configurable
                if isinstance(action, FileAction):
                    result.append(self._export_action(action))
                if count == 4:
                    break
        return self._json(result)

    @pyqtSlot(str, result=str)
    def get_threads(self, uid: str) -> str:
        engine = self._get_engine(uid)
        return self._json(self._get_threads(engine) if engine else [])

    @pyqtSlot(str, str)
    def show_metadata(self, uid: str, ref: str) -> None:
        self.application.hide_systray()
        engine = self._get_engine(uid)
        if engine:
            path = engine.local.abspath(ref)
            self.application.show_metadata(path)

    @pyqtSlot(str, result=list)
    def get_unsynchronizeds(self, uid: str) -> List[Dict[str, Any]]:
        result = []
        engine = self._get_engine(uid)
        if engine:
            for conflict in engine.get_dao().get_unsynchronizeds():
                result.append(self._export_formatted_state(uid, conflict))
        return result

    @pyqtSlot(str, result=list)
    def get_conflicts(self, uid: str) -> List[Dict[str, Any]]:
        result = []
        engine = self._get_engine(uid)
        if engine:
            for conflict in engine.get_conflicts():
                result.append(self._export_formatted_state(uid, conflict))
        return result

    @pyqtSlot(str, result=list)
    def get_errors(self, uid: str) -> List[Dict[str, Any]]:
        result = []
        engine = self._get_engine(uid)
        if engine:
            for error in engine.get_errors():
                result.append(self._export_formatted_state(uid, error))
        return result

    @pyqtSlot(bool)
    def set_direct_edit_auto_lock(self, value: bool) -> None:
        self._manager.set_direct_edit_auto_lock(value)

    @pyqtSlot(result=bool)
    def get_direct_edit_auto_lock(self) -> bool:
        return self._manager.get_direct_edit_auto_lock()

    @pyqtSlot(bool, result=bool)
    def set_auto_start(self, value: bool) -> bool:
        return self._manager.set_auto_start(value)

    @pyqtSlot(result=bool)
    def get_auto_start(self) -> bool:
        return self._manager.get_auto_start()

    @pyqtSlot(bool)
    def set_auto_update(self, value: bool) -> None:
        self._manager.set_auto_update(value)

    @pyqtSlot(result=bool)
    def get_auto_update(self) -> bool:
        return self._manager.get_auto_update()

    @pyqtSlot(str)
    def set_update_channel(self, value: str) -> None:
        self._manager.set_update_channel(value)

    @pyqtSlot(result=str)
    def generate_report(self) -> str:
        try:
            return self._manager.generate_report()
        except Exception as e:
            log.exception("Report error")
            return "[ERROR] " + str(e)

    @pyqtSlot(bool)
    def set_tracking(self, value: bool) -> None:
        self._manager.set_tracking(value)

    @pyqtSlot(result=bool)
    def get_tracking(self) -> bool:
        return self._manager.get_tracking()

    @pyqtSlot(str)
    def open_remote_server(self, uid: str) -> None:
        self.application.hide_systray()
        engine = self._get_engine(uid)
        if engine:
            engine.open_remote()

    @pyqtSlot(str)
    def open_report(self, path: str) -> None:
        self._manager.open_local_file(path, select=True)

    @pyqtSlot(str, str)
    def open_local(self, uid: str, path: str) -> None:
        self.application.hide_systray()
        log.trace(f"Opening local file {path!r}")
        if not uid:
            self._manager.open_local_file(path)
        else:
            engine = self._get_engine(uid)
            if engine:
                filepath = engine.local.abspath(path)
                self._manager.open_local_file(filepath)

    @pyqtSlot()
    def open_help(self) -> None:
        self.application.hide_systray()
        self._manager.open_help()

    @pyqtSlot(str)
    def show_conflicts_resolution(self, uid: str) -> None:
        self.application.hide_systray()
        engine = self._get_engine(uid)
        if engine:
            self.application.show_conflicts_resolution(engine)

    @pyqtSlot(str)
    def show_settings(self, page: str) -> None:
        self.application.hide_systray()
        log.debug(f"Show settings on page {page}")
        self.application.show_settings(section=page or None)

    @pyqtSlot()
    def quit(self) -> None:
        try:
            self.application.quit()
        except:
            log.exception("Application exit error")

    @pyqtSlot(result=str)
    def get_version(self) -> str:
        return self._manager.version

    @pyqtSlot(result=str)
    def get_update_url(self) -> str:
        return Options.update_site_url

    @pyqtSlot(str)
    def web_update_token(self, uid: str) -> None:
        try:
            engine = self._get_engine(uid)
            if not engine:
                self.setMessage.emit("CONNECTION_UNKNOWN", "error")
                return
            params = urlencode({"updateToken": True})

            status = self._connect_startup_page(engine.server_url)
            if status >= 400 and status not in (401, 500, 503):
                # We might have to downgrade because the
                # browser login is not available.
                self._manager.updater._force_downgrade()
                return

            url = self._get_authentication_url(engine.server_url)
            if Options.is_frozen:
                url = f"{url}&{params}"
            callback_params = {"engine": uid}
            log.debug(f"Opening login window for token update with URL {url}")
            self.application._open_authentication_dialog(url, callback_params)
        except:
            log.exception(
                "Unexpected error while trying to open web"
                " authentication window for token update"
            )
            self.setMessage.emit("CONNECTION_UNKNOWN", "error")

    def _guess_server_url(self, server_url: str) -> str:
        """Handle invalide SSL certificates when guessing the server URL."""
        try:
            return guess_server_url(server_url, proxy=self._manager.proxy)
        except InvalidSSLCertificate as exc:
            log.critical(exc)
            parts = urlsplit(server_url)
            hostname = parts.netloc or parts.path
            if self.application.accept_unofficial_ssl_cert(hostname):
                Options.ca_bundle = None
                Options.ssl_no_verify = True
                return self._guess_server_url(server_url)
        return ""

    def _get_authentication_url(self, server_url: str) -> str:
        url = self._guess_server_url(server_url)
        if not url:
            raise ValueError("No URL found for Nuxeo server")

        server_url = url

        if not Options.is_frozen:
            return server_url

        params = urlencode(
            {
                "deviceId": self._manager.device_id,
                "applicationName": APP_NAME,
                "permission": TOKEN_PERMISSION,
                "deviceDescription": get_device(),
                "forceAnonymousLogin": "true",
                "useProtocol": "true",
            }
        )

        # Handle URL parameters
        parts = urlsplit(server_url)
        path = f"{parts.path}/{Options.browser_startup_page}".replace("//", "/")

        params = f"{parts.query}&{params}" if parts.query else params
        return urlunsplit((parts.scheme, parts.netloc, path, params, parts.fragment))

    # Settings section

    @pyqtSlot(result=str)
    def default_nuxeo_drive_folder(self) -> str:
        return get_default_nuxeo_drive_folder()

    @pyqtSlot(result=str)
    def default_server_url_value(self) -> str:
        """Make daily job better for our developers :)"""
        return getenv("NXDRIVE_TEST_NUXEO_URL", "")

    @pyqtSlot(str)
    def unbind_server(self, uid: str) -> None:
        self._manager.unbind_engine(uid)

    @pyqtSlot(str)
    def filters_dialog(self, uid: str) -> None:
        engine = self._get_engine(uid)
        if engine:
            self.application.show_filters(engine)

    def _bind_server(
        self,
        local_folder: str,
        url: str,
        username: str,
        password: Optional[str],
        name: Optional[str],
        **kwargs: Any,
    ) -> None:
        # Remove any parameters from the original URL
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))

        if name == "":
            name = None
        binder = Binder(
            username=username,
            password=password,
            token=kwargs.get("token"),
            no_check=False,
            no_fscheck=not kwargs.get("check_fs", True),
            url=url,
        )
        log.debug(f"Binder is : {binder.url}/{binder.username}")
        engine = self._manager.bind_engine(
            self._manager._get_default_server_type(),
            local_folder,
            name,
            binder,
            starts=False,
        )

        # Display the filters window to let the user choose what to sync
        self.filters_dialog(engine.uid)
        self.setMessage.emit("CONNECTION_SUCCESS", "success")

    @pyqtSlot(str, str, str, str, str)
    def bind_server(
        self,
        local_folder: str,
        url: str,
        username: str,
        password: str = None,
        name: str = None,
        **kwargs: Any,
    ) -> None:
        server_url = self._guess_server_url(url)
        if not server_url:
            self.setMessage.emit("CONNECTION_ERROR", "error")
            return

        try:
            return self._bind_server(
                local_folder, server_url, username, password, name, **kwargs
            )
        except RootAlreadyBindWithDifferentAccount as e:
            # Ask for the user
            values = [e.username, e.url]
            msgbox = QMessageBox(
                QMessageBox.Question,
                APP_NAME,
                Translator.get("ROOT_USED_WITH_OTHER_BINDING", values),
                QMessageBox.NoButton,
            )
            msgbox.addButton(Translator.get("CONTINUE"), QMessageBox.AcceptRole)
            cancel = msgbox.addButton(Translator.get("CANCEL"), QMessageBox.RejectRole)
            msgbox.exec_()
            if msgbox.clickedButton() == cancel:
                self.setMessage.emit("FOLDER_USED", "error")
                return

            kwargs["check_fs"] = False
            return self.bind_server(
                local_folder, server_url, username, password, name, **kwargs
            )
        except NotFound:
            error = "FOLDER_DOES_NOT_EXISTS"
        except InvalidDriveException:
            error = "INVALID_PARTITION"
        except Unauthorized:
            error = "UNAUTHORIZED"
        except FolderAlreadyUsed:
            error = "FOLDER_USED"
        except HTTPError:
            error = "CONNECTION_ERROR"
        except ConnectionError as e:
            if e.errno == 61:
                error = "CONNECTION_REFUSED"
            else:
                error = "CONNECTION_ERROR"
        except:
            log.exception("Unexpected error")
            # Map error here
            error = "CONNECTION_UNKNOWN"
        self.setMessage.emit(error, "error")

    @pyqtSlot(str, str)
    def web_authentication(self, server_url: str, local_folder: str) -> None:
        # Handle the server URL
        url = self._guess_server_url(server_url)
        if not url:
            self.setMessage.emit("CONNECTION_ERROR", "error")
            return

        parts = urlsplit(url)
        server_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment)
        )

        # Handle the engine
        engine_type = parts.fragment or self._manager._get_default_server_type()

        try:
            # Handle local folder
            if not self._manager.check_local_folder_available(local_folder):
                raise FolderAlreadyUsed()

            # Connect to startup page
            status = self._connect_startup_page(server_url)
            callback_params = {
                "local_folder": local_folder,
                "server_url": server_url,
                "engine_type": engine_type,
            }
            url = self._get_authentication_url(server_url)

            # Server will send a 401 in case of anonymous user configuration
            # Should maybe only check for 404
            if status < 400 or status in (401, 500, 503):
                # Page exists, let's open authentication dialog
                log.debug(
                    f"Web authentication is available on server {server_url}, "
                    f"opening login window with URL {url}"
                )
            else:
                # Startup page is not available
                log.debug(
                    f"Web authentication not available on server {server_url}, "
                    "falling back on basic authentication"
                )
                if Options.is_frozen:
                    # We might have to downgrade because the
                    # browser login is not available.
                    self._manager.updater._force_downgrade()
                    return

            self.openAuthenticationDialog.emit(url, callback_params)
            return
        except FolderAlreadyUsed:
            error = "FOLDER_USED"
        except StartupPageConnectionError:
            error = "CONNECTION_ERROR"
        except:
            log.exception(
                "Unexpected error while trying to open web authentication window"
            )
            error = "CONNECTION_UNKNOWN"
        self.setMessage.emit(error, "error")

    def _connect_startup_page(self, server_url: str) -> int:
        # Take into account URL parameters
        parts = urlsplit(self._guess_server_url(server_url))
        url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                f"{parts.path}/{Options.browser_startup_page}",
                parts.query,
                parts.fragment,
            )
        )
        headers = {
            "X-Application-Name": APP_NAME,
            "X-Device-Id": self._manager.device_id,
            "X-Client-Version": self._manager.version,
            "User-Agent": f"{APP_NAME}/{self._manager.version}",
        }

        log.debug(
            f"Proxy configuration for startup page connection: {self._manager.proxy}"
        )
        try:
            with requests.get(
                url,
                headers=headers,
                proxies=self._manager.proxy.settings(url=url),
                timeout=STARTUP_PAGE_CONNECTION_TIMEOUT,
                verify=Options.ca_bundle or not Options.ssl_no_verify,
            ) as resp:
                status = resp.status_code
        except:
            log.exception(
                f"Error while trying to connect to {APP_NAME}"
                f" startup page with URL {url}"
            )
            raise StartupPageConnectionError()
        else:
            log.debug(f"Status code for {url} = {status}")
            return status

    @pyqtSlot(str, str, result=bool)
    def set_server_ui(self, uid: str, server_ui: str) -> bool:
        log.debug(f"Setting ui to {server_ui}")
        engine = self._get_engine(uid)
        if not engine:
            self.setMessage.emit("CONNECTION_UNKNOWN", "error")
            return False
        engine.set_ui(server_ui)
        return True

    @pyqtSlot(result=str)
    def get_proxy_settings(self) -> str:
        proxy = self._manager.proxy
        result = {
            "config": getattr(proxy, "category", None),
            "pac_url": getattr(proxy, "pac_url", None),
            "url": getattr(proxy, "url", None),
        }
        return self._json(result)

    @pyqtSlot(str, str, str, result=bool)
    def set_proxy_settings(self, config: str, url: str, pac_url: str) -> bool:
        proxy = get_proxy(category=config, url=url, pac_url=pac_url)
        result = self._manager.set_proxy(proxy)
        if result:
            self.setMessage.emit(result, "error")
            return False
        else:
            self.setMessage.emit("PROXY_APPLIED", "success")
            return True

    @pyqtSlot(str, result=bool)
    def has_invalid_credentials(self, uid: str) -> bool:
        engine = self._get_engine(uid)
        return engine.has_invalid_credentials() if engine else False

    # Authentication section

    @pyqtSlot(str, str)
    def handle_token(self, token: str, username: str) -> None:
        if not token:
            error = "CONNECTION_REFUSED"
        elif "engine" in self._callback_params:
            error = self.update_token(token)
        else:
            error = self.create_account(token, username)
        if error:
            self.setMessage.emit(error, "error")

    def create_account(self, token: str, username: str) -> str:
        error = ""
        try:
            local_folder = self._callback_params["local_folder"]
            server_url = (
                self._callback_params["server_url"]
                + "#"
                + self._callback_params["engine_type"]
            )

            log.debug(
                f"Creating new account [{local_folder}, {server_url}, {username}]"
            )

            error = self.bind_server(
                local_folder,
                server_url,
                username,
                password=None,
                token=token,
                name=None,
            )

            log.debug(f"RETURN FROM BIND_SERVER IS: '{error}'")
        except:
            log.exception(
                "Unexpected error while trying to create a new account "
                f"[{local_folder}, {server_url}, {username}]"
            )
            error = "CONNECTION_UNKNOWN"
        finally:
            return error

    def update_token(self, token: str) -> str:
        error = ""
        engine = self._get_engine(self._callback_params["engine"])
        if not engine:
            return ""
        try:
            log.debug(
                "Updating token for account "
                f"[{engine.local_folder}, {engine.server_url}, {engine.remote_user}]"
            )

            engine.update_token(token)
            self.application.set_icon_state("idle")

        except requests.ConnectionError as e:
            log.exception("HTTP Error")
            if e.errno == 61:
                error = "CONNECTION_REFUSED"
            else:
                error = "CONNECTION_ERROR"
        except:
            log.exception(
                "Unexpected error while trying to update token for account "
                f"[{engine.local_folder}, {engine.server_url}, {engine.remote_user}]"
            )
            error = "CONNECTION_UNKNOWN"
        finally:
            return error

    # Systray section

    @pyqtSlot(bool)
    def suspend(self, start: bool) -> None:
        if start:
            self._manager.resume()
        else:
            self._manager.suspend()

    @pyqtSlot(result=bool)
    def is_paused(self) -> bool:
        return self._manager.is_paused()

    @pyqtSlot(str, result=int)
    def get_syncing_count(self, uid: str) -> int:
        count = 0
        engine = self._get_engine(uid)
        if engine:
            count = engine.get_dao().get_syncing_count()
        return count

    @pyqtSlot(str, result=int)
    def get_conflicts_count(self, uid: str) -> int:
        return len(self.get_conflicts(uid))

    @pyqtSlot(str, result=int)
    def get_errors_count(self, uid: str) -> int:
        return len(self.get_errors(uid))

    # Conflicts section

    @pyqtSlot(str, int)
    def resolve_with_local(self, uid: str, state_id: int) -> None:
        engine = self._get_engine(uid)
        if engine:
            engine.resolve_with_local(state_id)

    @pyqtSlot(str, int)
    def resolve_with_remote(self, uid: str, state_id: int) -> None:
        engine = self._get_engine(uid)
        if engine:
            engine.resolve_with_remote(state_id)

    @pyqtSlot(str, int)
    def retry_pair(self, uid: str, state_id: int) -> None:
        engine = self._get_engine(uid)
        if engine:
            engine.retry_pair(state_id)

    @pyqtSlot(str, int, str)
    def ignore_pair(self, uid: str, state_id: int, reason: str = "UNKNOWN") -> None:
        engine = self._get_engine(uid)
        if engine:
            engine.ignore_pair(state_id, reason=reason)

    @pyqtSlot(str, str, str)
    def open_remote(self, uid: str, remote_ref: str, remote_name: str) -> None:
        log.debug(f"Should open this : {remote_name} ({remote_ref})")
        try:
            engine = self._get_engine(uid)
            if engine:
                engine.open_edit(remote_ref, remote_name)
        except OSError:
            log.exception("Remote open error")