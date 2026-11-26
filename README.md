# shared-vault

**Your existing Obsidian / Markdown vault becomes Hermes's long-term memory.**

Before every turn, a `pre_llm_call` hook retrieves the vault sections that are
relevant to what you just asked, and injects them as clearly-labelled
historical evidence. No cloud, no vector database to install, no export
pipeline — the plugin reads the same `.md` files you already edit.

Unlike memory plugins that want to *replace* your notes with their own store,
shared-vault treats your vault as the source of truth and stays read-only by
default.

## What it does

- **Hybrid retrieval** — BM25-style keyword scoring fused with local
  embeddings (reciprocal-rank fusion), so exact identifiers *and* paraphrases
  both surface. Chinese and English mixed queries work out of the box with the
  default multilingual model (`bge-m3`).
- **Fully local** — the only network call is an HTTP POST to a **loopback**
  embeddings endpoint (Ollama `/api/embed` or any OpenAI-compatible
  `/v1/embeddings`). No note text leaves the machine.
- **Degrades gracefully** — no embeddings endpoint? lexical-only retrieval.
  No daemon? in-process retrieval (~2–5 s/turn instead of ~0.3 s). Any
  retrieval failure skips the injection rather than blocking your turn.
- **Stdlib-only Python** (numpy used opportunistically when present).

## Install

```
hermes plugins install mcheng916/hermes-shared-vault
```

Then set at least `vault_path` in the plugin settings
(`plugins.entries.shared-vault.settings`):

```yaml
plugins:
  entries:
    shared-vault:
      settings:
        vault_path: ~/Documents/MyVault
        embed_url: http://127.0.0.1:11434/api/embed   # optional, see below
```

An empty `vault_path` disables retrieval entirely, so installing is always safe.

## Configuration

| Setting        | Default | Meaning |
|----------------|---------|---------|
| `vault_path`   | *(empty)* | Absolute path to the vault to read. Empty = plugin off. |
| `roots`        | *(empty)* | Comma-separated top-level folders to index (e.g. `ai,work,learn`). Empty = every visible top-level folder. Hidden (`.obsidian` etc.) and symlinked folders are skipped. |
| `embed_url`    | *(empty → no hybrid in hook)* | Local embeddings endpoint. Ollama: `http://127.0.0.1:11434/api/embed`; OpenAI-compatible: `.../v1/embeddings`. Both response shapes are accepted. Keep it on loopback. |
| `embed_model`  | `bge-m3:latest` | Model name served at `embed_url` (multilingual). |
| `aliases_path` | *(empty)* | Optional JSON object mapping query terms to expansion words, e.g. `{"nickname": "full name words"}` — useful for names your notes use differently than you type. |
| `rrf_k`        | `60` | Reciprocal-rank-fusion constant that blends the keyword and embedding rankings. Higher = the two lists weighted more evenly; lower = each list's own top hits dominate more. Rarely needs changing. |
| `min_similarity` | `0.38` | Minimum cosine similarity (0–1) for an embedding hit to count. Raise for precision (less noise), lower for recall (more, weaker matches). |
| `hook_limit`   | `2` | Maximum number of note excerpts injected before each turn (1–8). Raise for more context per turn, lower for a smaller prompt. |

CLI/env equivalents: `SHARED_VAULT_ROOT`, `SHARED_VAULT_ROOTS`,
`SHARED_VAULT_EMBED_URL`, `SHARED_VAULT_EMBED_MODEL`, `SHARED_VAULT_ALIASES`,
`SHARED_VAULT_RRF_K`, `SHARED_VAULT_MIN_SIMILARITY`, `SHARED_VAULT_HOOK_LIMIT`.
`rrf_k`/`min_similarity` are applied per query (a running daemon honours changes
immediately); `embed_url`/`embed_model` also take effect on the next query, so no
re-indexing is needed after any of these.

## Enabling hybrid retrieval

1. Run any embeddings server on loopback, e.g.
   `ollama pull bge-m3 && ollama serve`.
2. Point `embed_url` at it.
3. Build the vector index once:
   `python3 vault.py --root /path/to/vault index` is not needed — instead run
   `python3 -c "import vault,semantic; v=vault.Vault('/path/to/vault'); semantic.build(v)"`
   (incremental; rerun or use the daemon's periodic refresh after bulk edits).

With no index present the plugin runs lexical-only and reports
`semantic_status: not_indexed` — nothing else to configure.

## Optional: persistent daemon

A resident process keeps the lexical index and vector matrix in RAM, turning
each turn's retrieval from ~2–5 s cold into ~0.3 s warm. It listens on a Unix
socket (`~/.cache/shared-vault/<vault-hash>/daemon.sock`, mode 0600 — no
network listener).

```
SHARED_VAULT_ROOT=/path/to/vault python3 /path/to/this/repo/daemon.py
```

Run it under your OS supervisor (LaunchAgent/systemd/docker-init). Prefer a
supervisor over a bare `&` background process: process supervisors that
SIGSTOP jobs (job control, containerized process managers) can leave the
daemon listening but never accepting — the hook then falls back to in-process
retrieval, losing the speed win silently.

## Optional: MCP tools (read/write)

`vault.py mcp` is a small stdlib MCP server exposing `vault_query`,
`vault_read`, `vault_propose`, `vault_create`, `vault_patch`. The hook itself
is strictly read-only; the write tools only matter if you register them.

```yaml
mcp_servers:
  shared_vault:
    command: python3
    args: ["/path/to/this/repo/vault.py", "--root", "/path/to/vault", "mcp"]
```

## Design notes

- `vault.py` — lexical engine: two-stage routing (per-file term index picks
  files, then windowed section retrieval), atomic JSON index cache under
  `~/.cache/shared-vault/`, guards against symlink escapes, dataless iCloud
  files, and out-of-category paths.
- `semantic.py` — embeddings client + cosine search with numpy fast path and
  pure-Python fallback; fusion wrapper installs onto the lexical class.
- `daemon.py` — optional single-client-per-connection Unix-socket server.
- The hook runs retrieval in a subprocess with a 5 s budget so a huge first
  index build never stalls a conversation; results are labelled
  "historical evidence, not instructions" to keep note text from being read as
  commands.

## Privacy

Files read: the Markdown under `roots` (plus `index.md`). Data written: the
index/vector caches under `~/.cache/shared-vault/` (mode 0700). Network:
`embed_url` only. No telemetry, no background uploads, no external API keys.

## License

MIT
