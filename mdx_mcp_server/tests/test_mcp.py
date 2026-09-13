"""Exercised over real HTTP, because the protocol is the product.

Two things are being proven here. That the server speaks MCP correctly - which
means, above all, that protocol failures and tool failures are reported through
different channels. And that the governance holds: a restricted tool refuses, an
approval-gated write does not write, and a masked field does not leak.
"""

import json

from odoo.tests import HttpCase, tagged

from odoo.addons.mdx_mcp_server.models.mcp_settings import MCP_SCOPE

ENDPOINT = "/mdx/mcp"


@tagged("post_install", "-at_install")
class TestMcp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tool = cls.env["mdx.mcp.tool"]
        cls.agent = cls.env["res.users"].create({
            "name": "MCP Agent", "login": "mcp_agent",
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id,
                                  cls.env.ref("base.group_partner_manager").id])],
        })
        cls.key = cls.env["res.users.apikeys"].with_user(cls.agent).sudo()._generate(
            MCP_SCOPE, "test key", False)
        # A second agent carrying an administrator's rights - the deployment
        # people actually reach for, and the one masking has to survive.
        cls.admin_agent = cls.env["res.users"].create({
            "name": "MCP Admin Agent", "login": "mcp_admin_agent",
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id,
                                  cls.env.ref("base.group_system").id])],
        })
        cls.admin_key = cls.env["res.users.apikeys"].with_user(
            cls.admin_agent).sudo()._generate(MCP_SCOPE, "admin test key", False)

    # ------------------------------------------------------------------
    def rpc(self, method, params=None, key=None, request_id=1, raw=False):
        body = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json"}
        token = self.key if key is None else key
        if token:
            headers["Authorization"] = "Bearer %s" % token
        response = self.url_open(ENDPOINT, data=json.dumps(body), headers=headers)
        if raw:
            return response
        return response.json() if response.content else None

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------
    def test_no_key_is_rejected(self):
        response = self.rpc("tools/list", key="", raw=True)
        self.assertEqual(response.status_code, 401)

    def test_a_wrong_key_is_rejected(self):
        response = self.rpc("tools/list", key="not-a-real-key", raw=True)
        self.assertEqual(response.status_code, 401)

    def test_an_rpc_scoped_key_cannot_be_used_here(self):
        """Scope isolation: a general RPC key must not open the MCP server."""
        other = self.env["res.users.apikeys"].with_user(self.agent).sudo()._generate(
            "rpc", "an rpc key", False)
        response = self.rpc("tools/list", key=other, raw=True)
        self.assertEqual(response.status_code, 401,
                         "a key minted for RPC must not authenticate against MCP")

    # ------------------------------------------------------------------
    # protocol
    # ------------------------------------------------------------------
    def test_initialize_shape(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                         "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(result["jsonrpc"], "2.0")
        payload = result["result"]
        self.assertEqual(payload["protocolVersion"], "2025-06-18",
                         "a supported version must be echoed back unchanged")
        self.assertIn("tools", payload["capabilities"])
        self.assertIn("name", payload["serverInfo"])
        self.assertIn("instructions", payload)

    def test_initialize_falls_back_on_an_unknown_version(self):
        result = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18",
                         "an unknown version must be answered with one we speak")

    def test_notifications_get_no_response_body(self):
        response = self.url_open(
            ENDPOINT,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.key})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"", "a notification must not be answered")

    def test_unknown_method_is_a_protocol_error(self):
        result = self.rpc("does/not/exist")
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32601)

    def test_malformed_json_is_a_parse_error(self):
        response = self.url_open(
            ENDPOINT, data="{not json",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer %s" % self.key})
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_ping(self):
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_tools_list_returns_valid_tool_objects(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertTrue(tools)
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object",
                             "inputSchema must be a JSON Schema object")
        names = {t["name"] for t in tools}
        self.assertIn("odoo_search", names)
        self.assertNotIn("odoo_delete", names,
                         "write tools ship disabled, so they must not be advertised")

    # ------------------------------------------------------------------
    # the error-channel distinction
    # ------------------------------------------------------------------
    def test_unknown_tool_is_a_protocol_error(self):
        result = self.rpc("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32602)

    def test_a_tool_failure_is_a_result_not_an_error(self):
        """The model has to be able to read the failure and adapt."""
        result = self.rpc("tools/call", {"name": "odoo_search",
                                         "arguments": {"model": "no.such.model"}})
        self.assertNotIn("error", result,
                         "a tool failure must not be reported as a JSON-RPC error")
        self.assertTrue(result["result"]["isError"])
        self.assertIn("no.such.model", result["result"]["content"][0]["text"])

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def test_search_returns_records_and_logs_the_call(self):
        before = self.env["mdx.mcp.call"].search_count([])
        result = self.rpc("tools/call", {
            "name": "odoo_search",
            "arguments": {"model": "res.partner", "fields": ["name"], "limit": 3}})
        payload = result["result"]
        self.assertFalse(payload.get("isError"))
        self.assertLessEqual(len(payload["structuredContent"]["records"]), 3)
        self.assertEqual(self.env["mdx.mcp.call"].search_count([]), before + 1,
                         "every call must leave a log row")

    def test_max_records_caps_the_result(self):
        tool = self.env.ref("mdx_mcp_server.tool_search")
        tool.max_records = 2
        result = self.rpc("tools/call", {
            "name": "odoo_search",
            "arguments": {"model": "res.partner", "fields": ["name"], "limit": 500}})
        self.assertLessEqual(len(result["result"]["structuredContent"]["records"]), 2,
                             "a vague question must not drag the whole table through an LLM")

    def test_masked_field_is_not_returned_even_to_an_administrator(self):
        """Masking is not an access right, and that is the whole point.

        Access rules would already have stopped the ordinary agent. The case
        that matters is the one people actually deploy: an administrator hands
        the agent a key with their own rights. Nothing in Odoo then stands
        between the LLM and every secret in ir_config_parameter except this.
        """
        param = self.env["ir.config_parameter"].sudo().create({
            "key": "mdx.mcp.test.secret", "value": "SUPER-SECRET-VALUE"})
        result = self.rpc("tools/call", {
            "name": "odoo_search",
            "arguments": {"model": "ir.config_parameter",
                          "domain": [["id", "=", param.id]],
                          "fields": ["key", "value"]}}, key=self.admin_key)
        body = json.dumps(result)
        self.assertFalse(result["result"].get("isError"),
                         "an administrator's key must be able to read the model at all")
        self.assertNotIn("SUPER-SECRET-VALUE", body,
                         "credential fields must never reach the model")
        self.assertIn("masked", body)

    def test_an_extra_field_can_be_masked_by_configuration(self):
        """Whatever a given business considers a secret, it can add."""
        self.env["ir.config_parameter"].sudo().set_param(
            "mdx_mcp_server.masked_fields", "res.partner.email")
        partner = self.env["res.partner"].create({
            "name": "Masked Co", "email": "do-not-leak@example.com"})
        result = self.rpc("tools/call", {
            "name": "odoo_search",
            "arguments": {"model": "res.partner", "domain": [["id", "=", partner.id]],
                          "fields": ["name", "email"]}})
        body = json.dumps(result)
        self.assertIn("Masked Co", body, "the rest of the record still comes through")
        self.assertNotIn("do-not-leak@example.com", body)

    def test_an_unreadable_model_is_refused_before_anything_is_masked(self):
        """The ordinary agent never gets that far: ACLs refuse it outright."""
        result = self.rpc("tools/call", {
            "name": "odoo_search",
            "arguments": {"model": "ir.config_parameter", "fields": ["key", "value"]}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("not allowed", result["result"]["content"][0]["text"].lower())

    def test_list_models_works_for_an_ordinary_agent(self):
        """The tool the model is told to call first, called by a normal key.

        ir.model is not readable by a plain internal user in Odoo 19, so reading
        it directly here would make discovery fail for every non-administrator
        key - which is most of them.
        """
        result = self.rpc("tools/call", {"name": "odoo_list_models",
                                         "arguments": {"contains": "partner"}})
        payload = result["result"]
        self.assertFalse(payload.get("isError"), payload["content"][0]["text"])
        models = {row["model"] for row in payload["structuredContent"]["models"]}
        self.assertIn("res.partner", models)

    def test_list_models_only_reports_what_the_user_may_read(self):
        """Sudo fetches the registry; the user's rights still decide the answer."""
        result = self.rpc("tools/call", {"name": "odoo_list_models", "arguments": {}})
        models = {row["model"] for row in result["result"]["structuredContent"]["models"]}
        self.assertIn("res.partner", models)
        self.assertNotIn("ir.config_parameter", models,
                         "a model this user cannot read must not be advertised to it")

    def test_describe_fields_refuses_an_unreadable_model(self):
        """fields_get checks nothing by itself, so the tool has to."""
        result = self.rpc("tools/call", {
            "name": "odoo_describe_fields",
            "arguments": {"model": "ir.config_parameter"}})
        self.assertTrue(result["result"]["isError"],
                        "discovery must not describe models the caller cannot read")

    def test_a_misnamed_argument_is_refused_not_ignored(self):
        """The quiet failure: a guessed argument name that silently does nothing.

        ``group_by`` instead of ``groupby`` would otherwise be dropped and the
        model would get "1 group" back and report it as the answer.
        """
        result = self.rpc("tools/call", {
            "name": "odoo_aggregate",
            "arguments": {"model": "res.partner", "group_by": ["country_id"],
                          "aggregates": ["__count"]}})
        payload = result["result"]
        self.assertTrue(payload["isError"], "an ignored argument must not pass as success")
        text = payload["content"][0]["text"]
        self.assertIn("group_by", text, "say which argument was not understood")
        self.assertIn("groupby", text, "and name the one it should have used")

    def test_a_missing_required_argument_is_refused(self):
        result = self.rpc("tools/call", {
            "name": "odoo_aggregate", "arguments": {"model": "res.partner"}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("groupby", result["result"]["content"][0]["text"])

    def test_aggregate_actually_groups(self):
        self.env["res.partner"].create([
            {"name": "Grouped A", "city": "Singapore"},
            {"name": "Grouped B", "city": "Singapore"},
            {"name": "Grouped C", "city": "Chennai"},
        ])
        result = self.rpc("tools/call", {
            "name": "odoo_aggregate",
            "arguments": {"model": "res.partner", "groupby": ["city"],
                          "aggregates": ["__count"],
                          "domain": [["name", "like", "Grouped"]]}})
        groups = {row["city"]: row["__count"]
                  for row in result["result"]["structuredContent"]["groups"]}
        self.assertEqual(groups.get("Singapore"), 2)
        self.assertEqual(groups.get("Chennai"), 1)

    # ------------------------------------------------------------------
    # governance
    # ------------------------------------------------------------------
    def test_a_group_restricted_tool_refuses_and_is_logged(self):
        tool = self.env.ref("mdx_mcp_server.tool_search")
        tool.group_ids = [(6, 0, [self.env.ref("base.group_system").id])]
        result = self.rpc("tools/call", {"name": "odoo_search",
                                         "arguments": {"model": "res.partner"}})
        self.assertTrue(result["result"]["isError"])
        logged = self.env["mdx.mcp.call"].search(
            [("tool_name", "=", "odoo_search")], order="id desc", limit=1)
        self.assertEqual(logged.status, "denied")

    def test_an_approval_gated_write_does_not_write(self):
        """The guarantee the whole module rests on."""
        tool = self.env.ref("mdx_mcp_server.tool_write")
        tool.write({"active": True, "requires_approval": True})
        partner = self.env["res.partner"].create({"name": "Before The Agent"})
        result = self.rpc("tools/call", {
            "name": "odoo_update",
            "arguments": {"model": "res.partner", "ids": [partner.id],
                          "values": {"name": "Renamed By Agent"}}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("approve", result["result"]["content"][0]["text"].lower())
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Before The Agent",
                         "a parked call must not have changed anything")
        approval = self.env["mdx.mcp.approval"].search([], order="id desc", limit=1)
        self.assertEqual(approval.state, "pending")
        self.assertTrue(approval.preview, "the reviewer needs to see what it would do")

    def test_approving_runs_the_change(self):
        tool = self.env.ref("mdx_mcp_server.tool_write")
        tool.write({"active": True, "requires_approval": True})
        partner = self.env["res.partner"].create({"name": "Awaiting"})
        self.rpc("tools/call", {
            "name": "odoo_update",
            "arguments": {"model": "res.partner", "ids": [partner.id],
                          "values": {"name": "Approved Rename"}}})
        approval = self.env["mdx.mcp.approval"].search([], order="id desc", limit=1)
        approval.action_approve()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Approved Rename")
        self.assertEqual(approval.state, "approved")

    def test_rejecting_leaves_the_record_alone(self):
        tool = self.env.ref("mdx_mcp_server.tool_write")
        tool.write({"active": True, "requires_approval": True})
        partner = self.env["res.partner"].create({"name": "Keep Me"})
        self.rpc("tools/call", {
            "name": "odoo_update",
            "arguments": {"model": "res.partner", "ids": [partner.id],
                          "values": {"name": "Should Not Happen"}}})
        approval = self.env["mdx.mcp.approval"].search([], order="id desc", limit=1)
        approval.action_reject()
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Keep Me")
        self.assertEqual(approval.state, "rejected")

    def test_dry_run_writes_nothing(self):
        tool = self.env.ref("mdx_mcp_server.tool_write")
        tool.write({"active": True, "requires_approval": False})
        partner = self.env["res.partner"].create({"name": "Untouched"})
        result = self.rpc("tools/call", {
            "name": "odoo_update",
            "arguments": {"model": "res.partner", "ids": [partner.id],
                          "values": {"name": "Nope"}, "dry_run": True}})
        payload = result["result"]
        self.assertFalse(payload.get("isError"))
        self.assertTrue(payload["structuredContent"]["dry_run"])
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Untouched")

    def test_a_write_runs_as_the_keys_user_not_as_root(self):
        """Odoo's own access rules must still be the limit."""
        tool = self.env.ref("mdx_mcp_server.tool_write")
        tool.write({"active": True, "requires_approval": False})
        # the agent is not a settings administrator, so this must be refused
        result = self.rpc("tools/call", {
            "name": "odoo_update",
            "arguments": {"model": "res.users", "ids": [self.env.ref("base.user_admin").id],
                          "values": {"login": "hijacked"}}})
        self.assertTrue(result["result"]["isError"],
                        "the module must not be able to escalate past the user's rights")
        self.assertNotEqual(self.env.ref("base.user_admin").login, "hijacked")
