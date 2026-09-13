from odoo import _, api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_module_migrator_data_consent = fields.Boolean(
        string="I confirm sending source code to a third-party AI provider is acceptable",
        config_parameter="ai_module_migrator.data_consent",
        help="Every migration job and file this module processes is sent to the AI "
        "provider configured below — including source code, file paths, and manifest "
        "contents — as part of the request. No AI call is made until this is checked.",
    )
    ai_module_migrator_provider = fields.Selection(
        selection=lambda self: self.env["ai.module.migrator.provider.service"].provider_selection(),
        string="AI Provider",
        default="gemini",
        config_parameter="ai_module_migrator.provider",
    )
    ai_module_migrator_allowed_roots = fields.Text(
        string="Allowed Source Roots",
        default=lambda self: self.env["ai.module.migration.job"]._default_allowed_roots_text(),
    )
    ai_module_migrator_community_roots = fields.Text(
        string="Community Addon Roots",
        default=lambda self: self.env["ai.module.migration.job"]._default_community_roots_text(),
    )
    ai_module_migrator_enterprise_roots = fields.Text(
        string="Enterprise Addon Roots",
        default=lambda self: self.env["ai.module.migration.job"]._default_enterprise_roots_text(),
    )
    ai_module_migrator_prompt_max_chars = fields.Integer(
        string="Prompt Character Budget",
        default=90000,
        config_parameter="ai_module_migrator.prompt_max_chars",
    )
    ai_module_migrator_max_file_bytes = fields.Integer(
        string="Max File Read Bytes",
        default=180000,
        config_parameter="ai_module_migrator.max_file_bytes",
    )
    ai_module_migrator_max_output_tokens = fields.Integer(
        string="Max Output Tokens",
        default=34000,
        config_parameter="ai_module_migrator.max_output_tokens",
    )
    ai_module_migrator_temperature = fields.Float(
        string="Temperature",
        default=0.2,
        config_parameter="ai_module_migrator.temperature",
    )
    ai_module_migrator_request_timeout = fields.Integer(
        string="AI Request Timeout",
        # 600, not 240: across three real end-to-end migrations every single
        # failed file (5 of 5) was a 240s read timeout on a larger source file,
        # not a bad AI answer. limit_time_real_cron is already required at 3600,
        # so there is ample headroom.
        default=600,
        config_parameter="ai_module_migrator.request_timeout",
    )
    ai_module_migrator_job_batch_size = fields.Integer(
        string="Task Batch Size",
        default=5,
        config_parameter="ai_module_migrator.job_batch_size",
        help="How many queued background tasks one run of the scheduled action executes. "
             "Each task is one AI file migration, so a large batch makes a single cron run "
             "very long; the watchdog (limit_time_real_cron in odoo.conf) must be raised to "
             "match. Allowed range 1-100.",
    )
    ai_module_migrator_job_max_attempts = fields.Integer(
        string="Task Retry Limit",
        default=3,
        config_parameter="ai_module_migrator.job_max_attempts",
        help="How many times a background task is retried after a crash or provider outage, "
             "with exponential backoff between tries. This is transport-level retrying and is "
             "separate from Max Migration Attempts, which handles an AI that declines the work. "
             "Allowed range 1-10.",
    )
    ai_module_migrator_max_migration_attempts = fields.Integer(
        string="Max Migration Attempts",
        default=3,
        config_parameter="ai_module_migrator.max_migration_attempts",
        help="How many times one file may be sent to the AI provider before giving up. "
             "Extra attempts are used when the provider declines the task (replies SKIP, "
             "refuses, or answers with prose instead of code) and when its output fails "
             "syntax validation. Raise it for weaker or more cautious models; each attempt "
             "is an extra provider call. Allowed range 1-6.",
    )

    ai_module_migrator_openai_api_key = fields.Char(
        string="OpenAI API Key",
        groups="base.group_system",
    )
    ai_module_migrator_openai_endpoint = fields.Char(
        string="OpenAI Endpoint",
        default="https://api.openai.com/v1/responses",
        config_parameter="ai_module_migrator.openai_endpoint",
    )
    ai_module_migrator_openai_model = fields.Selection(
        [
            ("gpt-4.1-mini", "GPT-4.1 Mini"),
            ("gpt-4.1", "GPT-4.1"),
            ("gpt-4.1-nano", "GPT-4.1 Nano"),
            ("gpt-4o-mini", "GPT-4o Mini"),
            ("custom", "Custom Model"),
        ],
        string="OpenAI Model",
        default="gpt-4.1",
        config_parameter="ai_module_migrator.openai_model",
    )
    ai_module_migrator_openai_custom_model = fields.Char(
        string="Custom OpenAI Model",
        config_parameter="ai_module_migrator.openai_custom_model",
    )

    ai_module_migrator_gemini_api_key = fields.Char(
        string="Gemini API Key",
        groups="base.group_system",
    )
    ai_module_migrator_gemini_endpoint = fields.Char(
        string="Gemini Endpoint",
        default="https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        config_parameter="ai_module_migrator.gemini_endpoint",
    )
    ai_module_migrator_gemini_model = fields.Selection(
        [
            ("gemini-2.5-flash", "Gemini 2.5 Flash"),
            ("gemini-2.5-pro", "Gemini 2.5 Pro"),
            ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite"),
            ("gemini-2.0-flash", "Gemini 2.0 Flash"),
            ("gemini-2.0-flash-lite", "Gemini 2.0 Flash-Lite"),
            ("custom", "Custom Model"),
        ],
        string="Gemini Model",
        default="gemini-2.5-pro",
        config_parameter="ai_module_migrator.gemini_model",
    )
    ai_module_migrator_gemini_custom_model = fields.Char(
        string="Custom Gemini Model",
        config_parameter="ai_module_migrator.gemini_custom_model",
    )

    ai_module_migrator_anthropic_api_key = fields.Char(
        string="Claude API Key",
        groups="base.group_system",
    )
    ai_module_migrator_anthropic_endpoint = fields.Char(
        string="Claude Endpoint",
        default="https://api.anthropic.com/v1/messages",
        config_parameter="ai_module_migrator.anthropic_endpoint",
    )
    ai_module_migrator_anthropic_model = fields.Selection(
        [
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-8", "Claude Opus 4.8"),
            ("custom", "Custom Model"),
        ],
        string="Claude Model",
        default="claude-sonnet-4-6",
        config_parameter="ai_module_migrator.anthropic_model",
    )
    ai_module_migrator_anthropic_custom_model = fields.Char(
        string="Custom Claude Model",
        config_parameter="ai_module_migrator.anthropic_custom_model",
    )

    ai_module_migrator_groq_api_key = fields.Char(
        string="Groq API Key",
        groups="base.group_system",
    )
    ai_module_migrator_groq_endpoint = fields.Char(
        string="Groq Endpoint",
        default="https://api.groq.com/openai/v1/chat/completions",
        config_parameter="ai_module_migrator.groq_endpoint",
    )
    ai_module_migrator_groq_model = fields.Selection(
        [
            ("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile"),
            ("llama-3.1-8b-instant", "Llama 3.1 8B Instant"),
            ("openai/gpt-oss-20b", "GPT-OSS 20B"),
            ("openai/gpt-oss-120b", "GPT-OSS 120B"),
            ("custom", "Custom Model"),
        ],
        string="Groq Model",
        default="llama-3.3-70b-versatile",
        config_parameter="ai_module_migrator.groq_model",
    )
    ai_module_migrator_groq_custom_model = fields.Char(
        string="Custom Groq Model",
        config_parameter="ai_module_migrator.groq_custom_model",
    )

    ai_module_migrator_deepseek_api_key = fields.Char(
        string="DeepSeek API Key",
        groups="base.group_system",
    )
    ai_module_migrator_deepseek_endpoint = fields.Char(
        string="DeepSeek Endpoint",
        default="https://api.deepseek.com/v1/chat/completions",
        config_parameter="ai_module_migrator.deepseek_endpoint",
    )
    ai_module_migrator_deepseek_model = fields.Selection(
        [
            ("deepseek-chat", "DeepSeek Chat (V3)"),
            ("deepseek-reasoner", "DeepSeek Reasoner (R1)"),
            ("custom", "Custom Model"),
        ],
        string="DeepSeek Model",
        default="deepseek-reasoner",
        config_parameter="ai_module_migrator.deepseek_model",
    )
    ai_module_migrator_deepseek_custom_model = fields.Char(
        string="Custom DeepSeek Model",
        config_parameter="ai_module_migrator.deepseek_custom_model",
    )

    ai_module_migrator_ollama_api_key = fields.Char(
        string="Ollama API Key",
        groups="base.group_system",
    )
    ai_module_migrator_ollama_endpoint = fields.Char(
        string="Ollama Endpoint",
        default="https://ollama.com/v1/chat/completions",
        config_parameter="ai_module_migrator.ollama_endpoint",
    )
    ai_module_migrator_ollama_model = fields.Selection(
        [
            ("gpt-oss:20b", "GPT-OSS 20B [Level 1 — Free]"),
            ("gemma4:31b", "Gemma 4 31B [Level 2 — Free]"),
            ("nemotron-3-super", "Nemotron 3 Super [Level 2 — Free]"),
            ("gpt-oss:120b", "GPT-OSS 120B [Level 3 — Free, heavy]"),
            ("qwen3-coder:480b", "Qwen3-Coder 480B [Level 3 — Free, heavy]"),
            ("qwen3.5", "Qwen 3.5 [Level 3 — Free, heavy]"),
            ("deepseek-v4-flash", "DeepSeek V4 Flash [Pro+]"),
            ("glm-5.1", "GLM 5.1 [Pro+]"),
            ("kimi-k2.6", "Kimi K2.6 [Pro+]"),
            ("minimax-m3", "MiniMax M3 [Pro+]"),
            ("custom", "Custom Model"),
        ],
        string="Ollama Model",
        default="gpt-oss:120b",
        config_parameter="ai_module_migrator.ollama_model",
    )
    ai_module_migrator_ollama_custom_model = fields.Char(
        string="Custom Ollama Model",
        config_parameter="ai_module_migrator.ollama_custom_model",
    )

    ai_module_migrator_ollama_session_cap = fields.Integer(
        string="Session Capacity (calls / 5h)",
        default=50,
        config_parameter="ai_module_migrator.ollama_session_cap",
        help="Local estimate only. Ollama Cloud does not expose real quota through its API, "
             "so tune this number to match what you see on ollama.com/settings.",
    )
    ai_module_migrator_ollama_weekly_cap = fields.Integer(
        string="Weekly Capacity (calls / 7d)",
        default=300,
        config_parameter="ai_module_migrator.ollama_weekly_cap",
    )
    ai_module_migrator_ollama_session_used = fields.Integer(
        string="Session Calls Used", compute="_compute_ollama_usage", readonly=True
    )
    ai_module_migrator_ollama_session_pct = fields.Float(
        string="Session Usage %", compute="_compute_ollama_usage", readonly=True
    )
    ai_module_migrator_ollama_session_reset_label = fields.Char(
        string="Session Resets", compute="_compute_ollama_usage", readonly=True
    )
    ai_module_migrator_ollama_weekly_used = fields.Integer(
        string="Weekly Calls Used", compute="_compute_ollama_usage", readonly=True
    )
    ai_module_migrator_ollama_weekly_pct = fields.Float(
        string="Weekly Usage %", compute="_compute_ollama_usage", readonly=True
    )
    ai_module_migrator_ollama_weekly_reset_label = fields.Char(
        string="Weekly Resets", compute="_compute_ollama_usage", readonly=True
    )
    # Anchor field for the JS auto-refresh widget. Not stored, never written to —
    # it only exists so a widget can attach to it and poll record.load() while
    # Settings is open, so the usage bars above update without a manual reload.
    ai_module_migrator_live_refresh_ping = fields.Char(string="Live Refresh", store=False)

    @api.model
    def _safe_int(self, value, default):
        """int(value) that falls back to default instead of raising.

        This compute recomputes on every Settings page read, so a corrupted
        or hand-edited ir.config_parameter value (Settings > Technical >
        Parameters) must not be able to break the whole Settings page.
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @api.depends()
    def _compute_ollama_usage(self):
        # No @api.depends triggers: this recomputes every time the field is
        # read fresh (i.e. on every reload/poll), which is what we want here.
        params = self.env["ir.config_parameter"].sudo()
        session_cap = self._safe_int(params.get_param("ai_module_migrator.ollama_session_cap"), 50)
        weekly_cap = self._safe_int(params.get_param("ai_module_migrator.ollama_weekly_cap"), 300)
        stats = self.env["ai.module.migrator.usage.log"].usage_stats(
            "ollama", session_cap=session_cap, weekly_cap=weekly_cap
        )
        for record in self:
            record.ai_module_migrator_ollama_session_used = stats["session_used"]
            record.ai_module_migrator_ollama_session_pct = stats["session_pct"]
            record.ai_module_migrator_ollama_session_reset_label = self._ollama_reset_label(stats["session_reset"])
            record.ai_module_migrator_ollama_weekly_used = stats["weekly_used"]
            record.ai_module_migrator_ollama_weekly_pct = stats["weekly_pct"]
            record.ai_module_migrator_ollama_weekly_reset_label = self._ollama_reset_label(stats["weekly_reset"])

    ai_module_migrator_compatible_api_key = fields.Char(
        string="Compatible API Key",
        groups="base.group_system",
    )
    ai_module_migrator_compatible_endpoint = fields.Char(
        string="Compatible Endpoint",
        default="https://api.openai.com/v1/chat/completions",
        config_parameter="ai_module_migrator.compatible_endpoint",
    )
    ai_module_migrator_compatible_model = fields.Selection(
        [("custom", "Custom Model")],
        string="Compatible Model",
        default="custom",
        config_parameter="ai_module_migrator.compatible_model",
    )
    ai_module_migrator_compatible_custom_model = fields.Char(
        string="Custom Compatible Model",
        default="gpt-4.1",
        config_parameter="ai_module_migrator.compatible_custom_model",
    )

    ai_module_migrator_connection_state = fields.Selection(
        [("not_tested", "Not Tested"), ("connected", "Connected"), ("failed", "Failed")],
        string="Connection Status",
        default="not_tested",
        config_parameter="ai_module_migrator.connection_state",
        readonly=True,
    )
    ai_module_migrator_last_test = fields.Datetime(
        string="Last Test",
        config_parameter="ai_module_migrator.last_test",
        readonly=True,
    )
    ai_module_migrator_connection_message = fields.Char(
        string="Connection Message",
        config_parameter="ai_module_migrator.connection_message",
        readonly=True,
    )

    ROOT_CONFIG_FIELDS = {
        "ai_module_migrator_allowed_roots": "ai_module_migrator.allowed_roots",
        "ai_module_migrator_community_roots": "ai_module_migrator.community_roots",
        "ai_module_migrator_enterprise_roots": "ai_module_migrator.enterprise_roots",
    }

    # Handled outside the standard config_parameter mechanism (which stores
    # field values as plain text) so these can be encrypted at rest instead.
    API_KEY_FIELDS = {
        "ai_module_migrator_openai_api_key": "ai_module_migrator.openai_api_key",
        "ai_module_migrator_gemini_api_key": "ai_module_migrator.gemini_api_key",
        "ai_module_migrator_anthropic_api_key": "ai_module_migrator.anthropic_api_key",
        "ai_module_migrator_groq_api_key": "ai_module_migrator.groq_api_key",
        "ai_module_migrator_deepseek_api_key": "ai_module_migrator.deepseek_api_key",
        "ai_module_migrator_ollama_api_key": "ai_module_migrator.ollama_api_key",
        "ai_module_migrator_compatible_api_key": "ai_module_migrator.compatible_api_key",
    }

    @api.model
    def get_values(self):
        from .crypto_utils import decrypt_secret  # noqa: PLC0415
        values = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        migration_job = self.env["ai.module.migration.job"]
        defaults = {
            "ai_module_migrator_allowed_roots": migration_job._default_allowed_roots_text(),
            "ai_module_migrator_community_roots": migration_job._default_community_roots_text(),
            "ai_module_migrator_enterprise_roots": migration_job._default_enterprise_roots_text(),
        }
        for field_name, param_name in self.ROOT_CONFIG_FIELDS.items():
            values[field_name] = params.get_param(param_name) or defaults[field_name]
        for field_name, param_name in self.API_KEY_FIELDS.items():
            values[field_name] = decrypt_secret(params.get_param(param_name)) or ""
        return values

    @api.model
    def _ollama_reset_label(self, reset_dt):
        if not reset_dt:
            return _("No recent usage")
        now = fields.Datetime.now()
        if reset_dt <= now:
            return _("Resetting now")
        delta = reset_dt - now
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return _("Resets in %(days)dd %(hours)dh") % {"days": days, "hours": hours}
        if hours:
            return _("Resets in %(hours)dh %(minutes)dm") % {"hours": hours, "minutes": minutes}
        return _("Resets in %(minutes)dm") % {"minutes": minutes}

    def set_values(self):
        from .crypto_utils import encrypt_secret  # noqa: PLC0415
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        for field_name, param_name in self.ROOT_CONFIG_FIELDS.items():
            params.set_param(param_name, self[field_name] or "")
        for field_name, param_name in self.API_KEY_FIELDS.items():
            value = self[field_name]
            if value:
                params.set_param(param_name, encrypt_secret(value))
            else:
                params.set_param(param_name, "")

    def action_ai_module_migrator_test_connection(self):
        self.ensure_one()
        provider = self.ai_module_migrator_provider or "gemini"
        endpoint, model, api_key = self._current_provider_values(provider)
        ok, message = self.env["ai.module.migrator.provider.service"].test_connection(
            provider=provider,
            endpoint=endpoint,
            model=model,
            api_key=api_key,
        )
        now = fields.Datetime.now()
        state = "connected" if ok else "failed"
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("ai_module_migrator.connection_state", state)
        params.set_param("ai_module_migrator.last_test", fields.Datetime.to_string(now))
        params.set_param("ai_module_migrator.connection_message", message)
        self.write({
            "ai_module_migrator_connection_state": state,
            "ai_module_migrator_last_test": now,
            "ai_module_migrator_connection_message": message,
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Odoo Module Upgrade AI"),
                "message": message,
                "type": "success" if ok else "danger",
                "sticky": False,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _current_provider_values(self, provider):
        service = self.env["ai.module.migrator.provider.service"]
        endpoint = getattr(self, "ai_module_migrator_%s_endpoint" % self._field_provider_key(provider), False)
        api_key = getattr(self, "ai_module_migrator_%s_api_key" % self._field_provider_key(provider), False)
        model_field = getattr(self, "ai_module_migrator_%s_model" % self._field_provider_key(provider), False)
        custom_model = getattr(self, "ai_module_migrator_%s_custom_model" % self._field_provider_key(provider), False)
        model = service._selected_model(model_field, custom_model, provider)
        return endpoint, model, api_key

    @api.model
    def _field_provider_key(self, provider):
        if provider == "openai_compatible":
            return "compatible"
        return provider
