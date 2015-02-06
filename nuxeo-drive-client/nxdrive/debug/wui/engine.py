'''
@author: Remi Cattiau
'''
from nxdrive.wui.dialog import WebDialog
from nxdrive.wui.dialog import WebDriveApi


class DebugDriveApi(WebDriveApi):
    def __init__(self, dlg, application):
        super(DebugDriveApi, self).__init__(dlg, application)


class EngineDialog(WebDialog):
    '''
    classdocs
    '''

    def __init__(self, application):
        '''
        Constructor
        '''
        super(EngineDialog, self).__init__(application, "engines.html",
                                                 api=DebugDriveApi(self, application), title="Nuxeo Drive - Engines")