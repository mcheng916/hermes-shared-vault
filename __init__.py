"""shared-vault: recall relevant Markdown-vault sections before every turn.

A pre_llm_call hook runs vault retrieval as a subprocess so a slow or broken
index can never block a user turn (5s budget, silent skip on trouble).
All configuration comes from plugin settings (plugins.entries.shared-vault.settings):
vault_path, roots, embed_url, embed_model, aliases_path, rrf_k, min_similarity,
hook_limit -- see README.md.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name('vault.py')


def register(ctx):
    def setting(key):
        try:
            return str(ctx.get_config(key, default='') or '').strip()
        except Exception:
            return ''

    def recall(user_message='', **kwargs):
        if not isinstance(user_message, str) or not user_message.strip():
            return None
        root = setting('vault_path')
        if not root:
            return None  # retrieval off until a vault is configured
        env = dict(os.environ, SHARED_VAULT_ROOT=root)
        for key, var in (('roots', 'SHARED_VAULT_ROOTS'),
                         ('embed_url', 'SHARED_VAULT_EMBED_URL'),
                         ('embed_model', 'SHARED_VAULT_EMBED_MODEL'),
                         ('aliases_path', 'SHARED_VAULT_ALIASES'),
                         ('rrf_k', 'SHARED_VAULT_RRF_K'),
                         ('min_similarity', 'SHARED_VAULT_MIN_SIMILARITY'),
                         ('hook_limit', 'SHARED_VAULT_HOOK_LIMIT')):
            value = setting(key)
            if value:
                env[var] = value
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), 'hermes-hook'],
                input=json.dumps({'extra': {'user_message': user_message}}),
                text=True, capture_output=True, timeout=5, env=env,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # retrieval must never block the turn
        return None

    ctx.register_hook('pre_llm_call', recall)
