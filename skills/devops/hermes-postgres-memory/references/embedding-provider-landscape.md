# Free-embedding-provider landscape (June 2026 snapshot)

Live probe results and the exact HTTP contracts for the providers in
`~/.hermes/.env`. Re-probe before relying on this — provider policies
change quietly.

## Quick decision tree

```
Need embeddings, want free, want working today?
├── Yes, and you have a KIMI_API_KEY   → kimi provider, bge_m3_embed (1024-dim)
├── Yes, willing to self-host          → ollama_local, bge-m3 (1024-dim)
│                                       or nomic-embed-text (768-dim)
├── No, willing to pay                 → openai text-embedding-3-small (1536-dim)
└── Test / fallback                    → noop (zero vector)
```

## Provider matrix (verified June 2026)

| Provider | Endpoint | Free? | Works? | Dim | Notes |
|---|---|---|---|---|---|
| `kimi` (Moonshot/Kimi) | `https://api.kimi.com/coding/v1/embeddings` | ✅ | ✅ | 1024 | OpenAI-shape. 9+ model-name aliases all return 1024-dim, L2-normalized. `bge_m3_embed` is the BGE-M3 weights, same as Ollama's `bge-m3`. |
| `ollama_cloud` | `https://ollama.com/api/embed` | "free tier" | ❌ | n/a | `/api/tags` returns 40 chat models, zero embed models. `/api/pull nomic-embed-text` → `404 model not found`. `/api/embed` → `401 unauthorized` even with valid key. Ollama Cloud's public catalog is **chat-only**. |
| `ollama_local` | `http://localhost:11434/api/embed` | ✅ | ✅ | model-dependent | Same HTTP contract as ollama_cloud but self-hosted. `ollama pull bge-m3` or `ollama pull nomic-embed-text`. |
| `openai` (not yet wired) | `https://api.openai.com/v1/embeddings` | ❌ ~$0.02/1M tok | ✅ | 1536 | `text-embedding-3-small` is the cost-effective default. |
| `noop` | n/a | ✅ | ✅ | any | Returns a 768/1024-dim zero vector. For tests and as a last-resort fail-safe. |

## Probe recipe (use this to verify before trusting)

Don't guess. Re-probe the live endpoints before recommending a provider —
responses and policies drift.

```bash
# Source creds
set -a; source ~/.hermes/.env; set +a
KEY=$KIMI_API_KEY      # or $OLLAMA_API_KEY

# 1. Does this account have embedding models? (Ollama Cloud)
curl -sS "https://ollama.com/api/tags" \
  -H "Authorization: Bearer $KEY" | python3 -c "
import json, sys
d = json.load(sys.stdin)
embeds = [m for m in d.get('models', [])
          if any(t in m['name'].lower() for t in ('embed','nomic','mxbai','bge','minilm'))]
print('Total models:', len(d.get('models', [])))
print('Embedding models:', len(embeds))
for m in embeds: print(' ', m['name'])
"

# 2. Can we actually embed? (Kimi)
curl -sS "https://api.kimi.com/coding/v1/embeddings" \
  -H "Authorization: Bearer $KIMI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"bge_m3_embed","input":"hello world"}' | python3 -c "
import json, sys
d = json.load(sys.stdin)
emb = d.get('data',[{}])[0].get('embedding', [])
print('dim:', len(emb), 'first 4:', emb[:4])
print('L2 norm (should be ~1.0):', sum(x*x for x in emb)**0.5)
"

# 3. Try multiple model aliases on the same endpoint (Kimi accepts many)
for model in bge_m3_embed bge-m3 bge-large bge-large-en nomic-embed-text \
            text-embedding-3-small text-embedding-v1 embedding-2; do
  dim=$(curl -sS "https://api.kimi.com/coding/v1/embeddings" \
        -H "Authorization: Bearer $KIMI_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$model\",\"input\":\"test\"}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('data',[{}])[0].get('embedding',[])))" 2>/dev/null)
  echo "  $model -> $dim dims"
done
```

## Kimi endpoint details (the live winner)

- **URL**: `https://api.kimi.com/coding/v1/embeddings`
- **Auth**: `Authorization: Bearer $KIMI_API_KEY` (also accepts a `sk-kimi-`-prefixed legacy Moonshot key with the right `KIMI_BASE_URL` override, but the coding endpoint works for both)
- **Request**:
  ```json
  {"model": "bge_m3_embed", "input": "your text here"}
  ```
  `input` may be a string or a list of strings (batch). For batch, response
  is a list of vectors, one per input.
- **Response** (OpenAI-shape):
  ```json
  {
    "object": "list",
    "model": "bge_m3_embed",
    "data": [
      {"index": 0, "embedding": [0.0403, 0.0370, -0.0289, ...]}
    ]
  }
  ```
- **Dim**: 1024 for all model aliases. **L2-normalized** (norm = 1.0).
- **Rate limit**: free-tier per-key RPM. On hit, response is
  `{"vectors": null, "base_resp": {"status_code": 1002, "status_msg": "rate limit exceeded(RPM)"}}`.
  *Note the shape is non-OpenAI* — `vectors: null` with `base_resp` rather
  than `data`. The embedder must handle both shapes; the kimi shape lives
  in the catch-all response parser, not the primary OpenAI-shape path.

## What does NOT work (don't re-test)

- **`api.moonshot.cn/v1/embeddings`** — 401 with `KIMI_API_KEY`. The
  Moonshot China endpoint requires a separate key.
- **`api.moonshot.ai/v1/embeddings`** — 401 with `KIMI_API_KEY`. Same reason.
- **`api.kimi.com/coding/v1/embeddings` with a `sk-kimi-` key** — works.
  The `KIMI_BASE_URL` env override in `~/.hermes/.env` was the giveaway:
  ```
  # KIMI_BASE_URL=https://api.kimi.com/coding/v1  # Default for sk-kimi- keys
  ```
- **`OLLAMA_API_KEY` against any Ollama Cloud embed endpoint** — the key
  authenticates fine for chat models, but the embed API path is either
  not provisioned (404 on `/api/pull`) or unauthorized (401 on
  `/api/embed`). Public reports of this in openclaw/ollama GitHub issues
  (openclaw #58457, ollama #16369, ollama #13776) as of Mar–May 2026.

## Self-hosting BGE-M3 later

If Kimi ever becomes rate-limited or paid-only, the same model is on
Ollama:

```bash
docker run -d --name ollama -p 11434:11434 ollama/ollama
docker exec ollama ollama pull bge-m3
```

Then in `.env`:
```
HERMES_EMBED_PROVIDER=ollama_local
HERMES_EMBED_BASE_URL=http://ollama-host:11434
```

**No backfill needed if you stay on BGE-M3** — vectors are bit-compatible
across Kimi-hosted and Ollama-hosted BGE-M3 (same weights, same dim).
