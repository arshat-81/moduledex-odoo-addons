{
    # 55 listings already say "MCP server". The gap is not another server: it is
    # everything above the protocol - writes, approval, and a record of what an
    # agent actually did. The title says which half this is.
    "name": "Odoo MCP Server & Audit",
    "version": "19.0.1.0.0",
    "summary": "Let Claude, ChatGPT and Cursor work in Odoo over MCP - with per-tool "
               "permissions, an approval queue for writes, and every call logged",
    "description": """
Odoo MCP Server & Audit
=======================

Connect an AI assistant to Odoo over the Model Context Protocol, on Community or
Enterprise, and keep a grip on what it does.

A real MCP server
    Implements the 2025-06-18 protocol over Streamable HTTP: initialize,
    tools/list, tools/call and ping, with JSON-RPC error semantics done
    properly - protocol failures are JSON-RPC errors, tool failures come back as
    isError so the model can read them and adjust.

Tools that can write, safely
    Search, read, aggregate, inspect fields and list models, plus create, write
    and delete. Every tool is a record: switch it off, restrict it to groups,
    cap how many rows it may touch, or mark it as needing approval.

An approval queue for anything that changes data
    A tool marked for approval does not execute. It parks the call, tells the
    model it is awaiting a human, and waits for someone to approve or reject it
    in Odoo. The agent gets a clear answer either way.

Dry run
    Write tools accept dry_run. The call returns exactly what it would have
    changed, touching nothing.

Every call on the record
    Who, which key, which tool, the arguments, how many records were touched,
    how long it took, and what came back. This is the log you need when someone
    asks what the AI did last Tuesday.

Secrets stay secret
    Field masking is on by default for the places credentials live, so a model
    that asks for ir.config_parameter values or password columns gets a mask
    rather than your API keys.

Authentication is Odoo's own
    Keys are res.users.apikeys scoped to this server: hashed the way Odoo
    hashes them, expiring, revocable, and tied to a real user whose access
    rights still apply to every call.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    "license": "OPL-1",
    "price": 149.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    "depends": ["base", "web"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/mcp_tool_data.xml",
        "views/mcp_tool_views.xml",
        "views/mcp_call_views.xml",
        "views/mcp_approval_views.xml",
        "views/mcp_settings_views.xml",
        "views/mcp_menus.xml",
    ],
    "application": True,
    "installable": True,
}
