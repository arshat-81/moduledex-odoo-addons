import logging

_logger = logging.getLogger(__name__)

VALID_OLLAMA_MODELS = {
    "gpt-oss:20b",
    "gemma4:31b",
    "nemotron-3-super",
    "gpt-oss:120b",
    "qwen3-coder:480b",
    "qwen3.5",
    "deepseek-v4-flash",
    "glm-5.1",
    "kimi-k2.6",
    "minimax-m3",
    "custom",
}
FALLBACK_MODEL = "gpt-oss:20b"
PARAM_KEY = "ai_module_migrator.ollama_model"


def migrate(cr, version):
    """Reset stale ollama_model param values that are no longer valid selections.

    The 1.0.0 → 1.0.1 upgrade changed the Ollama Cloud model list from local
    model names (llama3.3, qwen3:32b, …) to the correct cloud names
    (gpt-oss:20b, qwen3.5, …).  Any instance that had saved an old value will
    crash with a ValueError on the settings view onchange.  This script resets
    the param to the new default so the view loads cleanly.
    """
    if not version:
        return

    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        (PARAM_KEY,),
    )
    row = cr.fetchone()
    if row is None:
        return  # param not set — nothing to do

    current = row[0]
    if current not in VALID_OLLAMA_MODELS:
        cr.execute(
            "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
            (FALLBACK_MODEL, PARAM_KEY),
        )
        _logger.info(
            "ai_module_migrator: reset ollama_model param from %r to %r",
            current,
            FALLBACK_MODEL,
        )
