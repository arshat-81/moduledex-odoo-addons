import json
import logging
import socket
import time
import urllib.error
import urllib.request

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AiModuleMigratorProviderService(models.AbstractModel):
    _name = "ai.module.migrator.provider.service"
    _description = "Odoo Module Upgrade AI Provider Service"

    @api.model
    def provider_selection(self):
        return [
            ("openai", "OpenAI"),
            ("gemini", "Google Gemini"),
            ("anthropic", "Anthropic Claude"),
            ("groq", "Groq"),
            ("deepseek", "DeepSeek"),
            ("ollama", "Ollama Cloud"),
            ("openai_compatible", "OpenAI-Compatible"),
        ]

    @api.model
    def generate_text(self, prompt, provider=False, model=False, endpoint=False, api_key=False, max_tokens=False, temperature=False):
        provider = provider or self._param("ai_module_migrator.provider") or "gemini"
        self._assert_data_consent()
        endpoint, model, api_key = self._credentials(provider, endpoint=endpoint, model=model, api_key=api_key)
        if not api_key:
            raise UserError(_("Configure an API key for %s before generating an AI migration report.") % provider)
        _logger.info(
            "[AI_MIGRATOR_DEBUG] generate_text START provider=%s model=%s endpoint=%s prompt_chars=%s max_tokens=%s temperature=%s timeout=%s",
            provider, model, endpoint, len(prompt or ""), max_tokens, temperature, self._request_timeout(),
        )
        t0 = time.monotonic()
        try:
            if provider == "gemini":
                result = self._generate_gemini(prompt, endpoint, model, api_key, max_tokens, temperature)
            elif provider == "anthropic":
                result = self._generate_anthropic(prompt, endpoint, model, api_key, max_tokens, temperature)
            elif provider in ("groq", "deepseek", "ollama", "openai_compatible"):
                result = self._generate_chat_completion(provider, prompt, endpoint, model, api_key, max_tokens, temperature)
                if provider == "ollama":
                    self.env["ai.module.migrator.usage.log"].log_call("ollama", model=model)
            else:
                result = self._generate_openai(prompt, endpoint, model, api_key, max_tokens, temperature)
            _logger.info(
                "[AI_MIGRATOR_DEBUG] generate_text DONE provider=%s elapsed=%.2fs response_chars=%s",
                provider, time.monotonic() - t0, len(result or ""),
            )
            return result
        except Exception:
            _logger.exception(
                "[AI_MIGRATOR_DEBUG] generate_text FAILED provider=%s elapsed=%.2fs",
                provider, time.monotonic() - t0,
            )
            raise

    @api.model
    def test_connection(self, provider=False, model=False, endpoint=False, api_key=False):
        provider = provider or self._param("ai_module_migrator.provider") or "gemini"
        endpoint, model, api_key = self._credentials(provider, endpoint=endpoint, model=model, api_key=api_key)
        if not model or not api_key:
            return False, _("Missing model or API key.")
        prompt = "Reply only with: connection ok"
        try:
            text = self.generate_text(
                prompt,
                provider=provider,
                model=model,
                endpoint=endpoint,
                api_key=api_key,
                max_tokens=32,
                temperature=0.1,
            )
            return True, _("Connected successfully. Provider response: %s") % (text or _("OK"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")
            return False, _("Connection failed: %s") % (detail[:800] or error)
        except Exception as error:
            return False, _("Connection failed: %s") % error

    @api.model
    def _credentials(self, provider, endpoint=False, model=False, api_key=False):
        provider = provider or "gemini"
        if provider == "gemini":
            endpoint = endpoint or self._param("ai_module_migrator.gemini_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.gemini_model"),
                self._param("ai_module_migrator.gemini_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.gemini_api_key")
        elif provider == "anthropic":
            endpoint = endpoint or self._param("ai_module_migrator.anthropic_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.anthropic_model"),
                self._param("ai_module_migrator.anthropic_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.anthropic_api_key")
        elif provider == "groq":
            endpoint = endpoint or self._param("ai_module_migrator.groq_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.groq_model"),
                self._param("ai_module_migrator.groq_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.groq_api_key")
        elif provider == "deepseek":
            endpoint = endpoint or self._param("ai_module_migrator.deepseek_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.deepseek_model"),
                self._param("ai_module_migrator.deepseek_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.deepseek_api_key")
        elif provider == "ollama":
            endpoint = endpoint or self._param("ai_module_migrator.ollama_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.ollama_model"),
                self._param("ai_module_migrator.ollama_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.ollama_api_key")
        elif provider == "openai_compatible":
            endpoint = endpoint or self._param("ai_module_migrator.compatible_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.compatible_model"),
                self._param("ai_module_migrator.compatible_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.compatible_api_key")
        else:
            endpoint = endpoint or self._param("ai_module_migrator.openai_endpoint") or self._default_endpoint(provider)
            model = model or self._selected_model(
                self._param("ai_module_migrator.openai_model"),
                self._param("ai_module_migrator.openai_custom_model"),
                provider,
            )
            api_key = api_key or self._param_secret("ai_module_migrator.openai_api_key")
        return (endpoint or "").strip(), (model or "").strip(), (api_key or "").strip()

    @api.model
    def _param(self, key):
        return self.env["ir.config_parameter"].sudo().get_param(key)

    @api.model
    def _param_secret(self, key):
        """Like _param, but decrypts values stored via encrypt_secret (API keys)."""
        from .crypto_utils import decrypt_secret  # noqa: PLC0415
        return decrypt_secret(self._param(key))

    @api.model
    def _assert_data_consent(self):
        """Refuse to call any AI provider until an admin has explicitly opted in.

        Every job/file this module migrates is sent to a third-party AI
        provider's API as part of the prompt — source code, file paths, and
        manifest contents leave this server. That must be an explicit,
        recorded choice made in Settings, not an assumption baked into
        clicking a button, so this is enforced here (server-side, on every
        call) rather than only documented in the UI.
        """
        if not self._param("ai_module_migrator.data_consent"):
            raise UserError(_(
                "Before any AI provider can be called, an administrator must confirm, in "
                "Settings → Odoo Module Upgrade AI, that sending source code to a third-party "
                "AI provider is acceptable for this addon."
            ))

    @api.model
    def _selected_model(self, model, custom_model, provider):
        if model == "custom":
            return (custom_model or "").strip() or self._default_model(provider)
        return (model or "").strip() or self._default_model(provider)

    @api.model
    def _default_model(self, provider):
        # These fall-back models are only used when nothing has been configured at
        # all (fresh install, Settings never saved). They intentionally favor the
        # most capable model each provider offers at little/no extra cost over its
        # "fast/instant" tier, because migration correctness (not latency) is what
        # matters here: code that installs wrong on a production database is much
        # more expensive than a slower AI call. Users can still override per-job.
        if provider == "gemini":
            return "gemini-2.5-pro"
        if provider == "anthropic":
            return "claude-sonnet-4-6"
        if provider == "groq":
            return "llama-3.3-70b-versatile"
        if provider == "deepseek":
            return "deepseek-reasoner"
        if provider == "ollama":
            return "gpt-oss:120b"
        if provider == "openai_compatible":
            return "gpt-4.1"
        return "gpt-4.1"

    @api.model
    def _default_endpoint(self, provider):
        if provider == "gemini":
            return "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        if provider == "anthropic":
            return "https://api.anthropic.com/v1/messages"
        if provider == "groq":
            return "https://api.groq.com/openai/v1/chat/completions"
        if provider == "deepseek":
            return "https://api.deepseek.com/v1/chat/completions"
        if provider == "ollama":
            return "https://ollama.com/v1/chat/completions"
        if provider == "openai_compatible":
            return "https://api.openai.com/v1/chat/completions"
        return "https://api.openai.com/v1/responses"

    @api.model
    def _generate_openai(self, prompt, endpoint, model, api_key, max_tokens, temperature):
        payload = {
            "model": model,
            "input": prompt,
            "max_output_tokens": int(max_tokens or self._default_max_tokens()),
            "temperature": self._safe_temperature(temperature),
        }
        request = urllib.request.Request(
            endpoint or self._default_endpoint("openai"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
                "User-Agent": "OdooModuleUpgradeAI/1.0",
            },
            method="POST",
        )
        data = self._send_json_request(request)
        # The Responses API reports truncation in the envelope, not in an error:
        # without this a cut-off file came back looking like a complete answer.
        # The wording matters — _is_token_limit_error() matches on it to retry
        # with a bigger output budget, exactly as the other providers do.
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason") or "unknown"
            if reason == "max_output_tokens":
                raise UserError(_("OpenAI stopped because the response reached the max output tokens limit."))
            raise UserError(_("OpenAI returned an incomplete response (reason: %s).") % reason)
        text = data.get("output_text")
        if not text:
            chunks = []
            for item in data.get("output") or []:
                for content in item.get("content", []):
                    if content.get("type") in ("output_text", "text") and content.get("text"):
                        chunks.append(content["text"])
                    elif content.get("type") == "refusal" and content.get("refusal"):
                        # Pass the refusal through as text instead of returning
                        # nothing: the migration layer detects a non-answer and
                        # re-asks, which it cannot do with a bare "no text" error.
                        chunks.append(content["refusal"])
            text = "\n".join(chunks)
        return self._clean_response_text(text)

    @api.model
    def _generate_gemini(self, prompt, endpoint, model, api_key, max_tokens, temperature):
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self._safe_temperature(temperature),
                "maxOutputTokens": int(max_tokens or self._default_max_tokens()),
            },
        }
        request = urllib.request.Request(
            self._gemini_endpoint(endpoint, model),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )
        data = self._send_json_request(request)
        chunks = []
        blocked_reason = False
        for candidate in data.get("candidates") or []:
            finish_reason = candidate.get("finishReason")
            if finish_reason == "MAX_TOKENS":
                # Keep raising: _is_token_limit_error() matches this wording and
                # retries the call with a larger output budget.
                raise UserError(_("Gemini stopped before completing the response. Finish reason: %s") % finish_reason)
            if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
                # A content block on ordinary Odoo source is almost always
                # spurious, and it is a refusal rather than a transport failure.
                # Report it as text so the caller can re-ask, matching how the
                # OpenAI and Anthropic refusal paths behave.
                blocked_reason = finish_reason
                continue
            for part in (candidate.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    chunks.append(part["text"])
        if not chunks and blocked_reason:
            return _("The model declined to produce this output (finish reason: %s).") % blocked_reason
        return self._clean_response_text("\n".join(chunks))

    @api.model
    def _generate_anthropic(self, prompt, endpoint, model, api_key, max_tokens, temperature):
        payload = {
            "model": model,
            "max_tokens": int(max_tokens or self._default_max_tokens()),
            "temperature": self._safe_temperature(temperature),
            "messages": [{"role": "user", "content": prompt}],
        }
        request = urllib.request.Request(
            endpoint or self._default_endpoint("anthropic"),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        data = self._send_json_request(request)
        if data.get("stop_reason") == "max_tokens":
            raise UserError(_("Claude stopped because the response reached the token limit."))
        chunks = []
        for content in data.get("content") or []:
            if content.get("type") == "text" and content.get("text"):
                chunks.append(content["text"])
        if not chunks and data.get("stop_reason") == "refusal":
            # Same reasoning as the OpenAI refusal branch: report the decline as
            # text so the caller can push back, rather than as "did not return text".
            return _("The model declined to produce this output.")
        return self._clean_response_text("\n".join(chunks))

    @api.model
    def _generate_chat_completion(self, provider, prompt, endpoint, model, api_key, max_tokens, temperature):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._safe_temperature(temperature),
            "max_tokens": int(max_tokens or self._default_max_tokens()),
        }
        request = urllib.request.Request(
            endpoint or self._default_endpoint(provider),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
                "User-Agent": "OdooModuleUpgradeAI/1.0",
            },
            method="POST",
        )
        data = self._send_json_request(request)
        chunks = []
        for choice in data.get("choices") or []:
            if choice.get("finish_reason") == "length":
                raise UserError(_("The provider stopped because the response reached the token limit."))
            content = (choice.get("message") or {}).get("content")
            if content:
                chunks.append(content)
        return self._clean_response_text("\n".join(chunks))

    @api.model
    def _send_json_request(self, request):
        timeout = self._request_timeout()
        payload_size = len(request.data or b"")
        _logger.info(
            "[AI_MIGRATOR_DEBUG] HTTP request START url=%s payload_bytes=%s timeout=%s",
            request.full_url, payload_size, timeout,
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                connected = time.monotonic()
                body = response.read()
                done = time.monotonic()
                _logger.info(
                    "[AI_MIGRATOR_DEBUG] HTTP request DONE status=%s time_to_headers=%.2fs time_reading_body=%.2fs total=%.2fs response_bytes=%s",
                    response.status, connected - t0, done - connected, done - t0, len(body),
                )
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as error:
            _logger.warning(
                "[AI_MIGRATOR_DEBUG] HTTP request HTTPError status=%s elapsed=%.2fs",
                error.code, time.monotonic() - t0,
            )
            detail = error.read().decode("utf-8", errors="ignore")
            raise UserError(_("AI provider error: %s") % (detail[:2000] or error)) from error
        except (TimeoutError, socket.timeout) as error:
            _logger.warning(
                "[AI_MIGRATOR_DEBUG] HTTP request TIMED OUT after elapsed=%.2fs (configured timeout=%ss)",
                time.monotonic() - t0, timeout,
            )
            raise UserError(self._timeout_message(timeout)) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            _logger.warning(
                "[AI_MIGRATOR_DEBUG] HTTP request URLError reason=%r elapsed=%.2fs",
                reason, time.monotonic() - t0,
            )
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise UserError(self._timeout_message(timeout)) from error
            raise UserError(_("Could not reach the AI provider: %s") % reason) from error

    @api.model
    def _clean_response_text(self, text):
        text = (text or "").strip()
        if not text:
            raise UserError(_("The AI provider did not return text."))
        return text

    @api.model
    def _gemini_endpoint(self, endpoint, model):
        endpoint = endpoint or self._default_endpoint("gemini")
        if "{model}" in endpoint:
            # A plain substring replace rather than str.format(): the endpoint
            # is a user-configurable Settings value, and format() raises
            # KeyError/IndexError on any *other* stray '{'/'}' in it (e.g. a
            # fat-fingered custom endpoint), turning a config typo into an
            # unhandled traceback instead of a clean, actionable error.
            return endpoint.replace("{model}", model)
        if endpoint.endswith(":generateContent"):
            return endpoint
        return "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model

    @api.model
    def _default_max_tokens(self):
        value = self._param("ai_module_migrator.max_output_tokens")
        try:
            return max(512, int(value or 34000))
        except ValueError:
            return 34000

    @api.model
    def _request_timeout(self):
        value = self._param("ai_module_migrator.request_timeout")
        try:
            return max(15, min(900, int(value or 600)))
        except ValueError:
            return 600

    @api.model
    def _safe_temperature(self, temperature):
        """Coerce a caller-supplied or config-parameter temperature to a float.

        temperature can arrive here as a raw ir.config_parameter string (see
        job_generate_migration_plan, which passes get_param(...) through
        unconverted) — a manually-edited or corrupted parameter value must
        not turn a plain AI call into an unhandled ValueError.
        """
        if temperature is False or temperature is None or temperature == "":
            return 0.2
        try:
            return float(temperature)
        except (TypeError, ValueError):
            return 0.2

    @api.model
    def _timeout_message(self, timeout):
        return _(
            "AI provider request timed out after %s seconds. "
            "Use a faster model, reduce the prompt/output limits, or increase AI Request Timeout in settings."
        ) % timeout
