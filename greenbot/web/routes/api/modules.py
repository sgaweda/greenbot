import logging

from flask_restful import Resource
from flask_restful import reqparse

import greenbot.modules
import greenbot.utils
import greenbot.web.utils
from greenbot.managers.adminlog import AdminLogManager
from greenbot.managers.db import DBManager
from greenbot.models.module import Module
from greenbot.models.sock import SocketClientManager
from greenbot.modules.base import ModuleType
from greenbot.utils import find

log = logging.getLogger(__name__)


def validate_module(module_id):
    module = find(lambda m: m.ID == module_id, greenbot.modules.available_modules)

    if module is None:
        return False

    return module.MODULE_TYPE not in (ModuleType.TYPE_ALWAYS_ENABLED,)


class APIModuleToggle(Resource):
    def __init__(self):
        super().__init__()

        self.post_parser = reqparse.RequestParser()
        self.post_parser.add_argument("new_state", required=True)

    @greenbot.web.utils.requires_level(500)
    def post(self, row_id, **options):
        args = self.post_parser.parse_args()

        try:
            new_state = int(args["new_state"])
        except (ValueError, KeyError):
            return {"error": "Invalid `new_state` parameter."}, 400

        with DBManager.create_session_scope() as db_session:
            row = db_session.query(Module).filter_by(id=row_id).one_or_none()

            if not row:
                return {"error": "Module with this ID not found"}, 404

            if validate_module(row_id) is False:
                return {"error": "cannot modify module"}, 400

            row.enabled = True if new_state == 1 else False
            db_session.commit()
            payload = {"id": row.id, "new_state": row.enabled}
            AdminLogManager.post(
                "Module toggled",
                options["user"].discord_id,
                "Enabled" if row.enabled else "Disabled",
                row.id,
            )
            SocketClientManager.send("module.update", payload)
            return {"success": "successful toggle", "new_state": new_state}


def init(api):
    api.add_resource(APIModuleToggle, "/modules/toggle/<row_id>")
