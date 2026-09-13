"""The MCP endpoint.

Implements the 2025-06-18 Model Context Protocol over Streamable HTTP: a single
path accepting JSON-RPC 2.0 by POST. Only the parts a tool server needs are
implemented - initialize, the initialized notification, ping, tools/list and
tools/call - and unsupported methods answer with a proper "method not found"
rather than a 500.

The distinction that matters, and that implementations routinely get wrong:

* A **protocol** failure is a JSON-RPC ``error`` - unknown method, malformed
  request, unknown tool. The client is at fault and the model should not see it
  as data.
* A **tool** failure is a successful JSON-RPC ``result`` carrying
  ``isError: true``. The model is meant to read it, understand what went wrong
  and try something else. Returning a JSON-RPC error here hides the reason from
  the model and it retries the same broken call forever.

Authentication is a bearer token checked against ``res.users.apikeys`` scoped to
this server, so keys are hashed by Odoo, expire, can be revoked from the user
form, and are tied to a real user whose access rights govern every call.
"""

import json
import logging
import time

from odoo import _, http
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError
from odoo.http import request

from ..models.mcp_settings import MCP_SCOPE

_logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "odoo-mdx-mcp"
SERVER_VERSION = "1.0.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message, data=None):
    body = {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}
    if data is not None:
        body["error"]["data"] = data
    return body


def _text(message, is_error=False, structured=None):
    """A CallToolResult. Structured content is also serialised into the text
    block, which the spec asks for so older clients still see something."""
    payload = {"content": [{"type": "text", "text": message}], "isError": is_error}
    if structured is not None:
        payload["structuredContent"] = structured
        payload["content"].append({
            "type": "text",
            "text": json.dumps(structured, indent=2, sort_keys=True, default=str)[:60000],
        })
    return payload


class MdxMcpController(http.Controller):

    # ------------------------------------------------------------------
    # readonly=False is deliberate. An ``auth="none"`` route is served on a
    # read-only cursor by default, and every call here writes an audit row, so
    # the default would drop the log of exactly the calls worth logging.
    @http.route("/mdx/mcp", type="http", auth="none", methods=["POST", "GET"],
                csrf=False, save_session=False, readonly=False)
    def mcp(self, **kwargs):
        if request.httprequest.method == "GET":
            # The spec allows GET for a server-initiated SSE stream. This server
            # never initiates, so saying so is better than holding a connection.
            return request.make_json_response(
                {"error": "This server does not open server-initiated streams. "
                          "POST JSON-RPC to this path."}, status=405)

        user = self._authenticate()
        if user is None:
            response = request.make_json_response(
                _error(None, INVALID_REQUEST, "Missing or invalid API key."), status=401)
            response.headers["WWW-Authenticate"] = 'Bearer realm="odoo-mcp"'
            return response

        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return request.make_json_response(
                _error(None, PARSE_ERROR, "Invalid JSON."), status=400)

        # A batch is a JSON array; each element is answered independently and
        # notifications contribute nothing to the response.
        if isinstance(body, list):
            replies = [r for r in (self._dispatch(m, user) for m in body) if r is not None]
            return request.make_json_response(replies or [], status=200)

        reply = self._dispatch(body, user)
        if reply is None:
            # A notification gets no body, only an acknowledgement.
            return request.make_response("", status=202)
        return request.make_json_response(reply)

    # ------------------------------------------------------------------
    def _authenticate(self):
        header = request.httprequest.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        key = header[7:].strip()
        if not key:
            return None
        env = request.env(user=1)  # only to reach the key table
        try:
            uid = env["res.users.apikeys"].sudo()._check_credentials(
                scope=MCP_SCOPE, key=key)
        except Exception:  # noqa: BLE001
            return None
        if not uid:
            return None
        return env["res.users"].sudo().browse(uid)

    # ------------------------------------------------------------------
    def _dispatch(self, message, user):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message.")
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method == "notifications/initialized":
            return None
        if isinstance(method, str) and method.startswith("notifications/"):
            return None

        if method == "initialize":
            return _result(request_id, self._initialize(message.get("params") or {}))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, self._tools_list(user))
        if method == "tools/call":
            return self._tools_call(request_id, message.get("params") or {}, user)

        if is_notification:
            return None
        return _error(request_id, METHOD_NOT_FOUND, "Unknown method: %s" % method)

    # ------------------------------------------------------------------
    def _initialize(self, params):
        wanted = params.get("protocolVersion")
        # Echo the client's version when we speak it, otherwise answer with ours
        # and let the client decide whether to continue.
        version = wanted if wanted in SUPPORTED_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Odoo", "version": SERVER_VERSION},
            "instructions": (
                "Tools operate on the Odoo database as the user this API key belongs to, so "
                "their access rights apply. Call odoo_list_models and odoo_describe_fields "
                "before guessing a model or field name. Write tools may be configured to "
                "require human approval: if a call comes back saying it is awaiting "
                "approval, report that to the user rather than retrying."
            ),
        }

    def _tools_list(self, user):
        env = request.env(user=user.id)
        tools = env["mdx.mcp.tool"].sudo().search([])._callable_by(user)
        return {"tools": [tool._as_mcp_tool() for tool in tools]}

    # ------------------------------------------------------------------
    def _tools_call(self, request_id, params, user):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        client_name = (request.httprequest.headers.get("User-Agent") or "")[:80]

        env = request.env(user=user.id)
        Tool = env["mdx.mcp.tool"].sudo()
        tool = Tool.search([("name", "=", name)], limit=1)
        if not tool:
            # Unknown tool is the client's mistake, not the model's: JSON-RPC error.
            return _error(request_id, INVALID_PARAMS, "Unknown tool: %s" % name)

        Call = env["mdx.mcp.call"].sudo()
        base = {
            "tool_id": tool.id, "tool_name": tool.name, "user_id": user.id,
            "client_name": client_name, "model_name": arguments.get("model"),
            "arguments": arguments, "is_write": tool.is_write,
        }

        if not tool._callable_by(user):
            Call._record(status="denied",
                         result_summary="Not permitted for this user's groups.", **base)
            return _result(request_id, _text(
                "You are not permitted to use this tool.", is_error=True))

        if tool.is_write and self._approval_required(env, tool):
            approval = self._park(env, tool, arguments, user, client_name)
            Call._record(status="pending", approval_id=approval.id,
                         result_summary="Parked for approval.", **base)
            return _result(request_id, _text(
                "This change needs a person to approve it. It has been queued in Odoo as "
                "request #%s and nothing has been modified. Tell the user it is awaiting "
                "approval; do not retry." % approval.id, is_error=True))

        started = time.time()
        try:
            # The tool record is server configuration and stays in sudo: an agent
            # has no business reading the registry that governs it. The env passed
            # in is the key's user, and every data operation inside execute() runs
            # through it, so Odoo's access rules remain the real limit.
            text, structured, touched = tool.execute(env, arguments)
        except (AccessError, AccessDenied) as error:
            Call._record(status="denied", result_summary=str(error)[:200],
                         duration_ms=int((time.time() - started) * 1000), **base)
            return _result(request_id, _text(str(error), is_error=True))
        except (UserError, ValidationError) as error:
            Call._record(status="error", result_summary=str(error)[:200],
                         duration_ms=int((time.time() - started) * 1000), **base)
            return _result(request_id, _text(str(error), is_error=True))
        except Exception as error:  # noqa: BLE001
            _logger.exception("mdx_mcp_server: %s failed", tool.name)
            Call._record(status="error", result_summary=str(error)[:200],
                         duration_ms=int((time.time() - started) * 1000), **base)
            # Deliberately not the traceback: the model does not need our stack.
            return _result(request_id, _text(
                "The tool failed: %s" % error, is_error=True))

        Call._record(status="ok", result_summary=text[:200], records_touched=touched,
                     duration_ms=int((time.time() - started) * 1000), **base)
        return _result(request_id, _text(text, structured=structured))

    # ------------------------------------------------------------------
    def _approval_required(self, env, tool):
        if tool.requires_approval:
            return True
        return bool(env["ir.config_parameter"].sudo().get_param(
            "mdx_mcp_server.require_approval_all_writes"))

    def _park(self, env, tool, arguments, user, client_name):
        """Queue the call, with a dry-run preview when the tool offers one."""
        preview = ""
        try:
            dry_args = dict(arguments, dry_run=True)
            # Same split: config in sudo, data as the requesting user, so the
            # preview shows what THEY would be able to change, not what root could.
            text, structured, _touched = tool.execute(env, dry_args)
            preview = "%s\n\n%s" % (text, json.dumps(structured, indent=2, default=str))
        except Exception as error:  # noqa: BLE001
            preview = _("Could not produce a preview: %s") % error
        return env["mdx.mcp.approval"].sudo().create({
            "summary": "%s on %s" % (tool.name, arguments.get("model") or "?"),
            "tool_id": tool.id,
            "requested_by_id": user.id,
            "client_name": client_name,
            "model_name": arguments.get("model"),
            "arguments": json.dumps(arguments, indent=2, sort_keys=True, default=str),
            "preview": preview[:20000],
        })
