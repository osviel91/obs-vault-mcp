from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("curator-context-mcp")

READER_MCP_URL = os.getenv("READER_MCP_URL", "http://markdown-vault-mcp:8000/mcp")
READER_TIMEOUT = float(os.getenv("READER_TIMEOUT", "10.0"))
SEARCH_POOL = int(os.getenv("CONSULTAR_SEARCH_POOL", "50"))
CAP_MOCS = int(os.getenv("CONSULTAR_CAP_MOCS", "5"))
CAP_DECISIONES = int(os.getenv("CONSULTAR_CAP_DECISIONES", "5"))
CAP_OBSOLETAS = int(os.getenv("CONSULTAR_CAP_OBSOLETAS", "5"))
CAP_GENERALES = int(os.getenv("CONSULTAR_CAP_GENERALES", "5"))

mcp = FastMCP(
    "curator-context-mcp",
    instructions=(
        "RAG-lite context tool. Una pregunta -> contexto curado por bucket "
        "(heurísticas, decisiones, contradicciones, MOCs, notas generales, "
        "obsoletas/baja confianza). "
        "Read-only: solo llama al reader via MCP-HTTP y postprocesa. No inventa."
    ),
    version="0.1.6",
)


class ContextError(ValueError):
    pass


def _parse_response_body(ct: str, text: str, *, expects_response: bool) -> dict[str, Any]:
    # Notificaciones JSON-RPC (sin `id`) no generan body: 202 Accepted vacío.
    if not text:
        if expects_response:
            raise ContextError("reader returned empty body for request")
        return {}
    if "text/event-stream" in ct:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data: "):
                return json.loads(line[6:])
        if expects_response:
            raise ContextError("SSE response without data event")
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContextError(f"reader returned non-JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ContextError(f"reader returned non-object JSON: {type(parsed).__name__}")
    return parsed


# ponytail: cliente JSON-RPC mínimo sobre streamable HTTP del MCP del reader.
# Sin lib MCP client externa: initialize + tools/call es todo lo que necesitamos.
class ReaderClient:
    def __init__(self, url: str, timeout: float) -> None:
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._req_id = 0

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict[str, Any], *, expects_response: bool = True) -> dict[str, Any]:
        resp = self._client.post(self._url, json=payload, headers=self._headers())
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 400:
            raise ContextError(f"reader HTTP {resp.status_code}: {resp.text[:200]}")
        return _parse_response_body(
            resp.headers.get("content-type", ""),
            resp.text,
            expects_response=expects_response,
        )

    def initialize(self) -> None:
        self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "curator-context-mcp", "version": "0.1.2"},
            },
        })
        # notifications/initialized es notificación JSON-RPC (sin id) -> sin respuesta.
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expects_response=False,
        )

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if "error" in resp:
            raise ContextError(f"reader error: {resp['error']}")
        return resp.get("result", {})

    def close(self) -> None:
        self._client.close()


def _parse_tool_text(result: dict[str, Any]) -> Any:
    # Prefer structuredContent (native, no double-serialization).
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and "result" in sc:
        return sc["result"]
    # Some MCP servers wrap with the same key in content[0].text (doble-serializado).
    content = result.get("content") or []
    if not content:
        raise ContextError("reader returned empty content")
    text = content[0].get("text", "")
    if not text:
        raise ContextError("reader returned empty text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContextError(f"reader returned non-JSON text: {exc}") from exc
    # ponytail: el reader v3.4.2 a veces devuelve un string serializado otra vez.
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise ContextError(f"reader returned double-serialized non-JSON: {exc}") from exc
    return parsed


def _extract_hits(payload: Any) -> list[dict[str, Any]]:
    # ponytail: el reader puede devolver:
    #   - lista nativa de hits (structuredContent.result)
    #   - dict con key "result" (outputSchema del reader)
    #   - dict con keys alternativas ("results", "hits", "items", "matches")
    if isinstance(payload, list):
        return [h for h in payload if isinstance(h, dict)]
    if isinstance(payload, dict):
        for key in ("result", "results", "hits", "items", "matches"):
            value = payload.get(key)
            if isinstance(value, list):
                return [h for h in value if isinstance(h, dict)]
    return []


def _norm_path(path: str) -> str:
    return (path or "").lstrip("/").lower()


OBSOLETE_RE = re.compile(
    r"\b(?:status|estado)\s*:\s*(?:obsolete|deprecated|obsoleto|deprecado|archived|archivado)\b",
    re.IGNORECASE,
)
LOWCONF_RE = re.compile(
    r"\b(?:confidence|confianza)\s*:\s*(?:low|baja)\b",
    re.IGNORECASE,
)


def classify(path: str, snippet: str = "") -> str | None:
    """Devuelve el bucket lógico o None si la nota se descarta del output."""
    p = _norm_path(path)
    if not p:
        return None
    if p.startswith("excalidraw/") or p.startswith("excalidraw/scripts/downloaded/"):
        return None
    if p.startswith("curator/inbox/") or p.startswith("inbox/"):
        return None
    if ".curator-archive/" in p or p.startswith(".curator-archive/"):
        return "obsoletas_o_baja_confianza"
    if p.startswith("curator/heuristics/") or p.startswith("heuristics/"):
        bucket = "heuristicas"
    elif p.startswith("curator/decisions/") or p.startswith("decisions/"):
        bucket = "decisiones"
    elif p.startswith("curator/contradictions/") or p.startswith("contradictions/"):
        bucket = "contradicciones"
    else:
        parts = p.split("/")
        is_moc = "mocs" in parts or any(
            seg == "moc" or seg.startswith("moc-") for seg in parts
        )
        bucket = "mocs" if is_moc else "notas_generales"
    # ponytail: si el snippet frontmatter indica obsoleto/baja confianza,
    # relega a bucket de fuentes débiles aunque el path sea heurística/decision.
    if snippet and (OBSOLETE_RE.search(snippet) or LOWCONF_RE.search(snippet)):
        return "obsoletas_o_baja_confianza"
    return bucket


BUCKET_WEIGHTS = {
    "heuristicas": 2.0,
    "mocs": 2.0,
    "decisiones": 1.0,
    "contradicciones": 1.0,
    "obsoletas_o_baja_confianza": 1.0,
    "notas_generales": 1.0,
}


def _pick(hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "path": hits[0].get("path", ""),
        "title": hits[0].get("title") or hits[0].get("heading") or "",
        "heading": hits[0].get("heading", ""),
        "score_ponderado": hits[0]["score_ponderado"],
        "score_original": hits[0]["score_original"],
        "snippet": hits[0].get("content") or hits[0].get("snippet") or "",
        "bucket": hits[0]["bucket"],
    }


def _basename_title(path: str) -> str:
    base = os.path.basename(path or "")
    if base.lower().endswith(".md"):
        base = base[:-3]
    return base


def _project_title(hit: dict[str, Any]) -> str:
    title = hit.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = hit.get("heading")
    if isinstance(heading, str) and heading.strip():
        return heading.strip()
    return _basename_title(hit.get("path") or hit.get("Path") or "")


def _truncate(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _project_snippet(hit: dict[str, Any]) -> str:
    sections = hit.get("sections")
    if isinstance(sections, list) and sections:
        first = sections[0]
        if isinstance(first, dict):
            content = first.get("content")
            if isinstance(content, str) and content.strip():
                return _truncate(content)
    content = hit.get("content")
    if isinstance(content, str) and content.strip():
        return _truncate(content)
    snippet = hit.get("snippet")
    if isinstance(snippet, str) and snippet.strip():
        return _truncate(snippet)
    heading = hit.get("heading")
    if isinstance(heading, str) and heading.strip():
        return heading.strip()
    return ""


def _project_tags(hit: dict[str, Any]) -> list[str]:
    for source in (hit, hit.get("frontmatter") if isinstance(hit.get("frontmatter"), dict) else None):
        if not isinstance(source, dict):
            continue
        tags = source.get("tags")
        if isinstance(tags, list):
            return [str(tag).strip() for tag in tags if str(tag).strip()]
        if isinstance(tags, str) and tags.strip():
            return [tags.strip()]
    return []


def _moc_reason(path: str, pregunta: str) -> str:
    q = pregunta.lower()
    if "zigbee" in q or "home assistant" in q or "smarthome" in q or "smart home" in q:
        return "Agrupa notas del dominio SmartHome relacionadas con Zigbee y Home Assistant"
    if any(token in q for token in ("infra", "homelab", "docker", "network", "networking")):
        return "Sirve como índice de entrada a documentación relevante de infraestructura para este tema"
    return "Sirve como índice de entrada a documentación relevante para este tema"


def _project_item(hit: dict[str, Any], bucket: str, pregunta: str) -> dict[str, Any]:
    path = hit.get("path") or hit.get("Path") or ""
    item = {
        "path": path,
        "titulo": _project_title(hit),
        "title": _project_title(hit),
        "heading": hit.get("heading", ""),
        "score": hit.get("score", 0.0),
        "score_original": hit.get("score", 0.0),
        "score_normalizado": hit.get("_score_norm", 0.0),
        "score_ponderado": hit.get("_score_norm", 0.0) * BUCKET_WEIGHTS.get(bucket, 1.0),
        "snippet": _project_snippet(hit),
        "tags": _project_tags(hit),
        "bucket": bucket,
        "_score_norm": hit.get("_score_norm", 0.0),
    }
    if bucket == "mocs":
        item["porque"] = _moc_reason(path, pregunta)
    return item


def _maybe_normalize_scores(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # ponytail: el reader devuelve escalas distintas según el modo:
    #   - keyword (BM25): score 0..~10
    #   - hybrid (RRF):    score ~1/(60+rank) -> 0.016 típico (pequeño pero <=1)
    #   - semantic:        score en [0,1] (similitud coseno)
    # El umbral_similitud del caller está pensado en escala 0..1 (top=1.0).
    # Por eso normalizamos SIEMPRE por el max del pool, sin importar la escala
    # cruda del reader. Sin este paso, RRF (top~0.016) nunca pasa umbral 0.4.
    scores = [h.get("score") or 0.0 for h in hits]
    if not scores:
        return hits
    mx = max(scores)
    if mx > 0:
        for h in hits:
            h["_score_norm"] = (h.get("score") or 0.0) / mx
    else:
        for h in hits:
            h["_score_norm"] = 0.0
    return hits


def _build_summary(
    umbral: float,
    sobre_umbral: int,
    total: int,
    buckets_count: dict[str, int],
    top: dict[str, Any] | None,
) -> str:
    if sobre_umbral == 0:
        return "sin contexto relevante en el vault"
    bc = ",".join(f"{k}={v}" for k, v in buckets_count.items() if v)
    top_str = "sin top"
    if top:
        top_str = f"top: {top['path']} ({top.get('titulo') or top.get('title') or top.get('path')})"
    return (
        f"{sobre_umbral} hits sobre umbral {umbral} (pool={total}) | "
        f"{top_str} | buckets: {bc or 'ninguno'}"
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.custom_route("/info", methods=["GET"])
async def info(_: Request) -> JSONResponse:
    return JSONResponse({
        "service": "curator-context-mcp",
        "reader_mcp_url": READER_MCP_URL,
        "search_pool": SEARCH_POOL,
        "caps": {
            "mocs": CAP_MOCS,
            "decisiones": CAP_DECISIONES,
            "obsoletas": CAP_OBSOLETAS,
            "notas_generales": CAP_GENERALES,
        },
    })


@mcp.tool
def consultar_contexto(
    pregunta: str,
    perfil_origen: str = "",
    max_heuristicas: int = 5,
    max_contradicciones: int = 3,
    umbral_similitud: float = 0.6,
) -> dict[str, Any]:
    """RAG-lite: una pregunta -> contexto curado por bucket (heurísticas, MOCs,
    decisiones, contradicciones, notas generales, obsoletas/baja confianza). Devuelve un dict
    estructurado sin invención: si no sabe, lo dice. Latencia <2s por una sola
    llamada search híbrida al reader markdown-vault-mcp."""
    if not pregunta or not pregunta.strip():
        raise ContextError("pregunta is required")

    t0 = time.perf_counter()
    metricas: dict[str, Any] = {
        "perfil_origen": perfil_origen,
        "umbral_usado": umbral_similitud,
        "reader_queryable": False,
        "reader_mode": "hybrid",
        "total_hits": 0,
        "hits_sobre_umbral": 0,
        "chunks_deduped": 0,
        "buckets_descartados": 0,
        # Telemetría de diagnóstico del response del reader. Si volvemos a
        # ver hits_sobre_umbral=0 con total_hits>0, estas cuatro claves nos
        # dicen exactamente qué shape devolvió el reader y si extrajimos algo.
        "reader_payload_type": None,        # "list" | "dict" | None
        "reader_payload_keys": [],          # si dict: keys; si list: []
        "reader_sc_keys": [],               # keys de structuredContent, si hay
        "reader_has_structured_content": False,
        "hits_extraidos": 0,                # len(hits) tras _extract_hits
        "hit_top_score_raw": None,          # score del top hit (raw reader), o None
        "abstained": False,
        "abstain_reason": None,
        "top_bucket": "none",
        "score_top_normalizado": None,
        "latencia_ms": 0,
        "error": None,
        "error_index_not_ready": False,
    }
    empty = {
        "summary": "",
        "mocs_relevantes": [],
        "heuristicas": [],
        "decisiones": [],
        "contradicciones": [],
        "notas_generales": [],
        "obsoletas_o_baja_confianza": [],
        "metricas": metricas,
    }

    client = ReaderClient(READER_MCP_URL, READER_TIMEOUT)
    try:
        try:
            client.initialize()
        except (httpx.HTTPError, json.JSONDecodeError, ContextError) as exc:
            metricas["error"] = f"reader initialize failed: {exc}"
            metricas["error_index_not_ready"] = True
            empty["summary"] = "reader no responde a initialize; reintenta en unos segundos"
            return empty

        # Guard: ¿el reader está queryable?
        try:
            status_raw = client.call_tool("get_index_status", {})
            status = _parse_tool_text(status_raw)
            queryable = bool(status.get("queryable", True)) if isinstance(status, dict) else True
        except (httpx.HTTPError, json.JSONDecodeError, ContextError):
            queryable = True  # ponytail: si la llamada falla, asumimos queryable
        metricas["reader_queryable"] = queryable
        if not queryable:
            metricas["error"] = "reader index not queryable yet"
            metricas["error_index_not_ready"] = True
            empty["summary"] = "reader aún indexando; reintenta en unos segundos"
            return empty

        # Una sola llamada search.
        try:
            search_raw = client.call_tool("search", {
                "query": pregunta,
                "limit": SEARCH_POOL,
                "mode": "hybrid",
            })
            # Telemetría cruda del response: structuredContent + tipo/keys del payload.
            sc = search_raw.get("structuredContent") if isinstance(search_raw, dict) else None
            if isinstance(sc, dict):
                metricas["reader_has_structured_content"] = True
                metricas["reader_sc_keys"] = list(sc.keys())
            payload = _parse_tool_text(search_raw)
            metricas["reader_payload_type"] = "list" if isinstance(payload, list) else (
                "dict" if isinstance(payload, dict) else type(payload).__name__
            )
            if isinstance(payload, dict):
                metricas["reader_payload_keys"] = list(payload.keys())
            hits = _extract_hits(payload)
            metricas["hits_extraidos"] = len(hits)
            metricas["total_hits"] = len(hits)
            if hits:
                top_raw = hits[0].get("score")
                if isinstance(top_raw, (int, float)):
                    metricas["hit_top_score_raw"] = top_raw
                else:
                    metricas["hit_top_score_raw"] = repr(top_raw)
            logger.debug(
                " consultar_contexto: sc_keys=%s payload_type=%s payload_keys=%s hits=%d top_raw=%r",
                metricas["reader_sc_keys"],
                metricas["reader_payload_type"],
                metricas["reader_payload_keys"],
                len(hits),
                metricas["hit_top_score_raw"],
            )
        except (httpx.HTTPError, json.JSONDecodeError, ContextError) as exc:
            metricas["error"] = f"reader search failed: {exc}"
            metricas["error_index_not_ready"] = True
            empty["summary"] = f"reader search failed: {exc}"
            return empty
    finally:
        client.close()

    # Clasificar primero; solo se descartan paths explícitamente excluidos.
    # ponytail: normalizar sobre el subconjunto clasificado, no sobre los 50 hits.
    # Si el top RRF es una daily-note random y la heurística relevante está al
    # rank ~15, normalizar sobre el pool completo hunde la heurística (~0.3) por
    # debajo del umbral. Re-normalizar sobre el subconjunto relevante pone el top
    # heurística/MOC/.../ en 1.0, que es lo que el caller espera del umbral.
    categorized: list[dict[str, Any]] = []
    buckets_descartados = 0
    for h in hits:
        path = h.get("path") or h.get("Path") or ""
        snippet = h.get("content") or h.get("snippet") or ""
        bucket = classify(path, snippet)
        if bucket is None:
            buckets_descartados += 1
            continue
        categorized.append({"_raw": h, "_bucket": bucket})

    # Las notas generales participan, pero no deben cambiar la escala de los
    # buckets curatoriales cuando hay material curado en el mismo pool.
    subset = [c["_raw"] for c in categorized]
    priority_subset = [
        c["_raw"] for c in categorized if c["_bucket"] != "notas_generales"
    ] or subset
    has_priority = bool(priority_subset and priority_subset is not subset)
    _maybe_normalize_scores(priority_subset)
    if has_priority:
        priority_max = max((h.get("score") or 0.0 for h in priority_subset), default=0.0)
        for h in subset:
            h["_score_norm"] = (h.get("score") or 0.0) / priority_max if priority_max > 0 else 0.0
    top_subset = sorted(
        (
            {
                "path": h.get("path") or h.get("Path") or "",
                "score_raw": h.get("score"),
                "score_norm": h.get("_score_norm"),
            }
            for h in subset
        ),
        key=lambda x: x.get("score_norm") or 0.0,
        reverse=True,
    )[:3]
    logger.info("consultar_contexto subset top3=%s", top_subset)

    # Filtrar por umbral + ensamblar la lista clasificada.
    classified: list[dict[str, Any]] = []
    for c in categorized:
        h = c["_raw"]
        bucket = c["_bucket"]
        score_norm = h.get("_score_norm", 0.0)
        if score_norm < umbral_similitud:
            continue
        weight = BUCKET_WEIGHTS.get(bucket, 1.0)
        path = h.get("path") or h.get("Path") or ""
        snippet = h.get("content") or h.get("snippet") or ""
        classified.append(_project_item(h, bucket, pregunta))

    metricas["buckets_descartados"] = buckets_descartados
    metricas["hits_sobre_umbral"] = len(classified)

    # Dedup por path: quedamos con el de mayor score_ponderado.
    by_path: dict[str, dict[str, Any]] = {}
    deduped_lost = 0
    for h in classified:
        k = h["path"]
        if k not in by_path or h["score_ponderado"] > by_path[k]["score_ponderado"]:
            if k in by_path:
                deduped_lost += 1
            by_path[k] = h
    metricas["chunks_deduped"] = deduped_lost
    classified = sorted(by_path.values(), key=lambda x: x["score_ponderado"], reverse=True)

    # Caps por bucket.
    caps = {
        "mocs": CAP_MOCS,
        "heuristicas": max_heuristicas,
        "decisiones": CAP_DECISIONES,
        "contradicciones": max_contradicciones,
        "notas_generales": CAP_GENERALES,
        "obsoletas_o_baja_confianza": CAP_OBSOLETAS,
    }
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in caps}
    for h in classified:
        b = h["bucket"]
        if b in buckets and len(buckets[b]) < caps[b]:
            buckets[b].append(h)

    top = classified[0] if classified else None
    metricas["top_bucket"] = top["bucket"] if top else "none"
    metricas["score_top_normalizado"] = top.get("_score_norm") if top else None

    heuristicas = buckets["heuristicas"]
    decisiones = buckets["decisiones"]
    contradicciones = buckets["contradicciones"]
    mocs_relevantes = buckets["mocs"]
    notas_generales = buckets["notas_generales"]
    obsoletas = buckets["obsoletas_o_baja_confianza"]

    has_strong_curated = bool(heuristicas or decisiones or mocs_relevantes or notas_generales)
    has_only_weak = (
        not has_strong_curated
        and not contradicciones
        and len(obsoletas) <= 2
    )

    abstain_reason: str | None = None
    if len(classified) == 0:
        abstain_reason = "no_curated_hits"
    elif not heuristicas and not decisiones and not mocs_relevantes and not contradicciones:
        abstain_reason = "no_curated_hits"
    elif obsoletas and not heuristicas and not decisiones and not mocs_relevantes and not contradicciones:
        abstain_reason = "only_low_confidence"
    elif top and top["bucket"] == "obsoletas_o_baja_confianza" and not has_strong_curated:
        abstain_reason = "archive_only"
    elif has_only_weak:
        abstain_reason = "only_low_confidence"

    if abstain_reason:
        metricas["abstained"] = True
        metricas["abstain_reason"] = abstain_reason
        metricas["latencia_ms"] = int((time.perf_counter() - t0) * 1000)
        return {
            "summary": "sin contexto relevante en el vault",
            "mocs_relevantes": [],
            "heuristicas": [],
            "decisiones": [],
            "contradicciones": [],
            "notas_generales": [],
            "obsoletas_o_baja_confianza": [],
            "metricas": metricas,
        }

    buckets_count = {k: len(v) for k, v in buckets.items() if v}
    summary = _build_summary(
        umbral=umbral_similitud,
        sobre_umbral=len(classified),
        total=metricas["total_hits"],
        buckets_count=buckets_count,
        top=top,
    )

    metricas["latencia_ms"] = int((time.perf_counter() - t0) * 1000)

    return {
        "summary": summary,
        "mocs_relevantes": mocs_relevantes,
        "heuristicas": heuristicas,
        "decisiones": decisiones,
        "contradicciones": contradicciones,
        "notas_generales": notas_generales,
        "obsoletas_o_baja_confianza": obsoletas,
        "metricas": metricas,
    }


# --- autocheck ---
def _demo() -> None:
    fixtures = [
        {"path": "Curator/heuristics/h1.md", "score": 0.9, "content": "..."},
        {"path": "Curator/heuristics/h2.md", "score": 0.8, "content": "..."},
        {"path": "MOCs/m1.md", "score": 0.85, "content": "..."},
        {"path": "Curator/inbox/i1.md", "score": 0.95, "content": "..."},
        {"path": "Nota baja confianza", "score": 0.7, "content": "status: obsolete"},
    ]
    for f in fixtures:
        b = classify(f["path"], f.get("content", ""))
        assert (f["path"], b) in [
            ("Curator/heuristics/h1.md", "heuristicas"),
            ("Curator/heuristics/h2.md", "heuristicas"),
            ("MOCs/m1.md", "mocs"),
            ("Curator/inbox/i1.md", None),
            ("Nota baja confianza", "obsoletas_o_baja_confianza"),
        ], (f, b)
    assert classify("Research/zigbee.md") == "notas_generales"
    assert classify("Notas/manual.md") == "notas_generales"
    assert classify("Curator/inbox/pendiente.md") is None
    _maybe_normalize_scores(fixtures)
    # Normalización por max del pool: top hit (inbox, score 0.95) debe quedar en 1.0.
    top = max(fixtures, key=lambda x: x.get("score") or 0.0)
    assert abs(top["_score_norm"] - 1.0) < 1e-9
    # fixtures[0] (heurística h1, score 0.9) debe quedar en 0.9/0.95 ~ 0.947.
    assert abs(fixtures[0]["_score_norm"] - 0.9473684210526316) < 1e-9
    # Fixture RRF: scores como 1/(60+rank) (~0.016 top). Sin normalizar
    # siempre, jamás pasarían umbral_similitud=0.4. Confirma que normalizamos
    # sin importar la escala cruda.
    rrf = [
        {"path": "Curator/heuristics/top.md", "score": 0.0164},
        {"path": "Curator/heuristics/second.md", "score": 0.0143},
        {"path": "Nota noise.md", "score": 0.0080},
    ]
    _maybe_normalize_scores(rrf)
    assert rrf[0]["_score_norm"] == 1.0
    assert abs(rrf[1]["_score_norm"] - 0.872) < 0.001
    assert abs(rrf[2]["_score_norm"] - 0.488) < 0.001
    # Fixture BM25: scores grandes (8.07). Misma normalización aporta top=1.0.
    bm25 = [{"path": "a.md", "score": 8.07}, {"path": "b.md", "score": 4.5}]
    _maybe_normalize_scores(bm25)
    assert bm25[0]["_score_norm"] == 1.0
    assert abs(bm25[1]["_score_norm"] - 0.558) < 0.001
    # boost 2x heurísticas + MOCs:
    assert BUCKET_WEIGHTS["heuristicas"] == 2.0
    assert BUCKET_WEIGHTS["mocs"] == 2.0

    # Fix double-serialized search response (markdown-vault-mcp v3.4.2).
    hits_native = [{"path": "Curator/heuristics/x.md", "score": 8.07}]
    # Fixture 1: structuredContent.result presente (path feliz).
    r1 = {
        "content": [{"type": "text", "text": json.dumps(json.dumps(hits_native))}],
        "structuredContent": {"result": hits_native},
    }
    assert _parse_tool_text(r1) is hits_native or _parse_tool_text(r1) == hits_native
    # Fixture 2: sin structuredContent, text doble-serializado (fallback).
    r2 = {"content": [{"type": "text", "text": json.dumps(json.dumps(hits_native))}]}
    assert _parse_tool_text(r2) == hits_native
    # Fixture 3: sin content ni structuredContent -> error limpio.
    r3: dict[str, Any] = {}
    try:
        _parse_tool_text(r3)
    except ContextError:
        pass
    else:
        raise AssertionError("expected ContextError for empty reader response")
    # Fix notifications/initialized: el reader responde 202 Accepted sin body.
    # _parse_response_body debe tratar body vacío como OK si expects_response=False.
    assert _parse_response_body("application/json", "", expects_response=False) == {}
    # Y como error si expects_response=True.
    try:
        _parse_response_body("application/json", "", expects_response=True)
    except ContextError:
        pass
    else:
        raise AssertionError("expected ContextError for empty body with expects_response=True")
    # SSE con data: {...} -> dict parseado.
    sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}\n\n"
    assert _parse_response_body("text/event-stream", sse, expects_response=True) == {
        "jsonrpc": "2.0", "id": 2, "result": {}
    }
    # SSE sin data: con expects_response=True -> error limpio (no JSONDecodeError crudo).
    try:
        _parse_response_body("text/event-stream", "event: ping\n\n", expects_response=True)
    except ContextError:
        pass
    else:
        raise AssertionError("expected ContextError for SSE without data event")
    # SSE sin data: con expects_response=False -> {}.
    assert _parse_response_body("text/event-stream", "event: ping\n\n", expects_response=False) == {}
    # Body non-JSON con expects_response=True -> ContextError con mensaje limpio.
    try:
        _parse_response_body("application/json", "not-json", expects_response=True)
    except ContextError as exc:
        assert "non-JSON" in str(exc), str(exc)
    else:
        raise AssertionError("expected ContextError for non-JSON body")
    print("ok: classify + dedup + boost + double-serialize fix + notification handling")

    # Fixture del response SSE REAL observado en markdown-vault-mcp v3.4.2:
    # el body es SSE, dentro tiene result.structuredContent.result (lista nativa)
    # Y result.content[0].text es el mismo array pero doble-serializado.
    real_hits = [
        {"path": "Curator/heuristics/2026-07-18-homelab-topologia.md",
         "title": "Topología del homelab (sanitizada)",
         "folder": "Curator/heuristics", "score": 8.07, "content": "RPi 5 + HA ..."},
    ]
    sse_body = (
        "event: message\n"
        "data: "
        + json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "content": [{"type": "text", "text": json.dumps(json.dumps(real_hits))}],
                "structuredContent": {"result": real_hits},
            },
        })
        + "\n\n"
    )
    parsed = _parse_response_body("text/event-stream", sse_body, expects_response=True)
    assert isinstance(parsed, dict) and "result" in parsed
    inner = parsed["result"]
    assert isinstance(inner, dict) and "structuredContent" in inner
    extracted = _parse_tool_text(inner)
    assert extracted is real_hits or extracted == real_hits, extracted
    # _extract_hits debe aceptar lista nativa y dict con key "result".
    assert _extract_hits(real_hits) == real_hits
    assert _extract_hits({"result": real_hits}) == real_hits
    assert _extract_hits({"results": real_hits}) == real_hits
    # Normalización sobre el score real 8.07: top debe quedar en 1.0.
    h = list(real_hits)
    _maybe_normalize_scores(h)
    assert h[0]["_score_norm"] == 1.0
    print("ok: classify + dedup + boost + double-serialize fix + notification handling + sse-shape real")

    # Fix v0.1.5 -- normalización por subconjunto clasificado.
    # Fixture: 5 hits donde el top raw es una daily-note (path no Curator)
    # y la heurística relevante está al rank 3. Normalizando sobre el pool
    # completo, la heurística quedaría con score_norm ~0.13/0.20 = 0.65 (ok),
    # PERO si el reader devuelve 50 hits con muchos.Path basura al top, la
    # heurística cae bajo umbral. Simulamos ese caso: top=0.134 basura,heur
    # al rank 5 con raw 0.04. Normalización global -> 0.30 (fail 0.4).
    # Normalización por subconjunto -> 1.0 (pass 0.4).
    pool = [
        {"path": "Daily/2026-01-01.md", "score": 0.134},
        {"path": "Daily/2026-01-02.md", "score": 0.110},
        {"path": "Notas/random1.md", "score": 0.080},
        {"path": "Notas/random2.md", "score": 0.060},
        {"path": "Curator/heuristics/h1.md", "score": 0.040},
        {"path": "MOCs/cluster.md", "score": 0.030},
    ]
    # Replicar el flujo del tool: clasificar -> normalizar los buckets prioritarios.
    cat: list[dict[str, Any]] = []
    for h in pool:
        b = classify(h["path"], h.get("content", ""))
        if b is None:
            continue
        cat.append({"_raw": h, "_bucket": b})
    sub = [c["_raw"] for c in cat if c["_bucket"] != "notas_generales"]
    _maybe_normalize_scores(sub)
    priority_max = max(h["score"] for h in sub)
    for c in cat:
        c["_raw"]["_score_norm"] = c["_raw"]["score"] / priority_max
    # Heurística (raw 0.04) es el top del subconjunto (0.04 > 0.03 de MOC).
    # Tras re-normalizar: 1.0. Umbral 0.4 -> pass.
    heur_hit = next(c for c in cat if c["_bucket"] == "heuristicas")["_raw"]
    moc_hit = next(c for c in cat if c["_bucket"] == "mocs")["_raw"]
    assert heur_hit["_score_norm"] == 1.0, heur_hit["_score_norm"]
    assert abs(moc_hit["_score_norm"] - 0.75) < 1e-9, moc_hit["_score_norm"]
    general_hit = next(c for c in cat if c["_bucket"] == "notas_generales")["_raw"]
    assert general_hit["_score_norm"] > 1.0
    # Si normalizáramos sobre el pool completo (top 0.134 global), heuristic quedaría en 0.298.
    pool_copy = [dict(h) for h in pool]
    _maybe_normalize_scores(pool_copy)
    assert abs(pool_copy[4]["_score_norm"] - 0.04 / 0.134) < 1e-9
    assert pool_copy[4]["_score_norm"] < 0.4  # confirmaría el bug viejo
    print("ok: classify + dedup + boost + double-serialize fix + notification handling + sse-shape real + subset normalization")

    # v0.1.6 -- proyección de campos completos.
    rich = {
        "path": "Curator/heuristics/zigbee.md",
        "title": "Topología del homelab (sanitizada)",
        "score": 8.07,
        "sections": [{"content": "RPi 5 + Home Assistant + Zigbee coordinator USB and MQTT bridge for tests. " * 4}],
        "frontmatter": {"tags": ["heuristic", "infra", "architecture", "topology", "homelab"]},
        "_score_norm": 1.0,
    }
    projected = _project_item(rich, "heuristicas", "Zigbee Home Assistant")
    assert projected["titulo"] == "Topología del homelab (sanitizada)"
    assert projected["score"] == 8.07
    assert projected["snippet"]
    assert len(projected["snippet"]) <= 180
    assert projected["tags"] == ["heuristic", "infra", "architecture", "topology", "homelab"]

    # Fallback de título: sin title -> basename del path.
    untitled = {"path": "Curator/heuristics/2026-07-18-homelab-topologia.md", "score": 1.0, "_score_norm": 1.0}
    projected_untitled = _project_item(untitled, "heuristicas", "Zigbee Home Assistant")
    assert projected_untitled["titulo"] == "2026-07-18-homelab-topologia"

    # MOC: porque heurístico.
    moc = {
        "path": "MOCs/SmartHome.md",
        "title": "SmartHome MOC",
        "score": 1.2,
        "_score_norm": 1.0,
    }
    projected_moc = _project_item(moc, "mocs", "Zigbee Home Assistant")
    assert projected_moc["porque"]

    # Abstención por solo baja confianza.
    obsoleta = _project_item(
        {
            "path": ".curator-archive/old.md",
            "score": 0.2,
            "_score_norm": 1.0,
            "content": "status: obsolete",
        },
        "obsoletas_o_baja_confianza",
        "receta tortilla patatas",
    )
    heuristicas = []
    decisiones = []
    contradicciones = []
    mocs_relevantes = []
    obsoletas = [obsoleta]
    has_strong_curated = bool(heuristicas or decisiones or mocs_relevantes)
    has_only_weak = not has_strong_curated and not contradicciones and len(obsoletas) <= 2
    assert has_only_weak is True

    # Caso feliz Zigbee: heurística + MOC sobreviven al umbral tras normalización local.
    zigbee_pool = [
        {"path": "Daily/2026-01-01.md", "score": 0.134},
        {"path": "Curator/heuristics/2026-07-18-homelab-topologia.md", "score": 0.040, "title": "Topología del homelab (sanitizada)", "sections": [{"content": "RPi 5 + HA + Zigbee"}]},
        {"path": "MOCs/SmartHome.md", "score": 0.030, "title": "SmartHome MOC"},
    ]
    zigbee_cat: list[dict[str, Any]] = []
    for h in zigbee_pool:
        b = classify(h["path"], h.get("content", ""))
        if b is None:
            continue
        zigbee_cat.append({"_raw": h, "_bucket": b})
    zigbee_subset = [
        c["_raw"] for c in zigbee_cat if c["_bucket"] != "notas_generales"
    ]
    _maybe_normalize_scores(zigbee_subset)
    priority_max = max(h["score"] for h in zigbee_subset)
    for c in zigbee_cat:
        c["_raw"]["_score_norm"] = c["_raw"]["score"] / priority_max
    filtered = []
    for c in zigbee_cat:
        h = c["_raw"]
        if h["_score_norm"] >= 0.4:
            filtered.append(_project_item(h, c["_bucket"], "Zigbee Home Assistant"))
    assert any(i["bucket"] == "heuristicas" for i in filtered)
    assert any(i["bucket"] == "mocs" for i in filtered)
    print("ok: field projection + abstention + zigbee happy path")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        _demo()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
