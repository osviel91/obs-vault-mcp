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

mcp = FastMCP(
    "curator-context-mcp",
    instructions=(
        "RAG-lite context tool. Una pregunta -> contexto curado por bucket "
        "(heurísticas, decisiones, contradicciones, MOCs, obsoletas/baja confianza). "
        "Read-only: solo llama al reader via MCP-HTTP y postprocesa. No inventa."
    ),
    version="0.1.1",
)


class ContextError(ValueError):
    pass


# ponytail: cliente JSON-RPC mínimo sobre streamable HTTP del MCP del reader.
# Sin lib MCP client externa: initialize + tools/call es todo lo que necesitamos.
class ReaderClient:
    def __init__(self, url: str, timeout: float) -> None:
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(self._url, json=payload, headers=self._headers())
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data: "):
                    return json.loads(line[6:])
            raise ContextError("SSE response without data event")
        return resp.json()

    def initialize(self) -> None:
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "curator-context-mcp", "version": "0.1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self._post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
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
    # Fallback: content[0].text puede venir doble-serializado.
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
    # ponytail: el reader puede devolver lista, dict con 'results' o dict con 'hits'.
    if isinstance(payload, list):
        return [h for h in payload if isinstance(h, dict)]
    if isinstance(payload, dict):
        for key in ("results", "hits", "items", "matches"):
            if key in payload and isinstance(payload[key], list):
                return [h for h in payload[key] if isinstance(h, dict)]
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
        bucket = "mocs" if is_moc else None
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


def _maybe_normalize_scores(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # ponytail: el score hybrid (RRF) del reader no siempre está en [0,1].
    # Si el max > 1, dividimos por max para comparar contra umbral_similitud.
    # Si el reader ya normaliza, este paso es identidad.
    scores = [h.get("score") or 0.0 for h in hits]
    if not scores:
        return hits
    mx = max(scores)
    if mx > 1.0:
        for h in hits:
            if h.get("score") is not None:
                h["_score_norm"] = h["score"] / mx
    else:
        for h in hits:
            h["_score_norm"] = h.get("score") or 0.0
    return hits


def _build_summary(
    umbral: float,
    sobre_umbral: int,
    total: int,
    buckets_count: dict[str, int],
    top: dict[str, Any] | None,
) -> str:
    if sobre_umbral == 0:
        return (
            f"sin contexto suficiente; 0 hits sobre umbral {umbral} "
            f"(pool={total})"
        )
    bc = ",".join(f"{k}={v}" for k, v in buckets_count.items() if v)
    top_str = "sin top"
    if top:
        top_str = f"top: {top['path']} (score {top['score_ponderado']:.2f})"
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
        "caps": {"mocs": CAP_MOCS, "decisiones": CAP_DECISIONES, "obsoletas": CAP_OBSOLETAS},
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
    decisiones, contradicciones, obsoletas/baja confianza). Devuelve un dict
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
        "reader_payload_keys": [],
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
        "obsoletas_o_baja_confianza": [],
        "metricas": metricas,
    }

    client = ReaderClient(READER_MCP_URL, READER_TIMEOUT)
    try:
        try:
            client.initialize()
        except (httpx.HTTPError, ContextError) as exc:
            metricas["error"] = f"reader initialize failed: {exc}"
            metricas["error_index_not_ready"] = True
            empty["summary"] = "reader no responde a initialize; reintenta en unos segundos"
            return empty

        # Guard: ¿el reader está queryable?
        try:
            status_raw = client.call_tool("get_index_status", {})
            status = _parse_tool_text(status_raw)
            queryable = bool(status.get("queryable", True)) if isinstance(status, dict) else True
        except (httpx.HTTPError, ContextError):
            queryable = True  # ponytail: si la llamada falla, asumimos queryable
        metricas["reader_queryable"] = queryable
        if not queryable:
            metricas["error"] = "reader index not queryable yet"
            metricas["error_index_not_ready"] = True
            empty["summary"] = "reader aún indexando; reintenta en unos segundos"
            return empty

        # Una sola llamada search.
        search_raw = client.call_tool("search", {
            "query": pregunta,
            "limit": SEARCH_POOL,
            "mode": "hybrid",
        })
        payload = _parse_tool_text(search_raw)
        if isinstance(payload, dict) and not isinstance(payload.get("results"), list):
            # Capturar keys para debug si el reader cambia el shape.
            metricas["reader_payload_keys"] = list(payload.keys())
        hits = _extract_hits(payload)
        metricas["total_hits"] = len(hits)
    finally:
        client.close()

    # Normalizar score por max si el reader devuelve RRF no normalizado.
    _maybe_normalize_scores(hits)

    # Clasificar + filtrar por umbral.
    classified: list[dict[str, Any]] = []
    buckets_descartados = 0
    for h in hits:
        path = h.get("path") or h.get("Path") or ""
        snippet = h.get("content") or h.get("snippet") or ""
        bucket = classify(path, snippet)
        if bucket is None:
            buckets_descartados += 1
            continue
        score_norm = h.get("_score_norm", 0.0)
        if score_norm < umbral_similitud:
            continue
        weight = BUCKET_WEIGHTS.get(bucket, 1.0)
        classified.append({
            "path": path,
            "title": h.get("title") or h.get("heading") or "",
            "heading": h.get("heading", ""),
            "score_original": h.get("score", 0.0),
            "score_ponderado": score_norm * weight,
            "snippet": snippet,
            "bucket": bucket,
            "_score_norm": score_norm,
        })

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
        "obsoletas_o_baja_confianza": CAP_OBSOLETAS,
    }
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in caps}
    for h in classified:
        b = h["bucket"]
        if b in buckets and len(buckets[b]) < caps[b]:
            buckets[b].append(h)

    buckets_count = {k: len(v) for k, v in buckets.items() if v}
    top = classified[0] if classified else None
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
        "mocs_relevantes": buckets["mocs"],
        "heuristicas": buckets["heuristicas"],
        "decisiones": buckets["decisiones"],
        "contradicciones": buckets["contradicciones"],
        "obsoletas_o_baja_confianza": buckets["obsoletas_o_baja_confianza"],
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
    _maybe_normalize_scores(fixtures)
    assert fixtures[0]["_score_norm"] == 0.9  # ya en [0,1]: identidad
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
    print("ok: classify + dedup + boost + double-serialize fix")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        _demo()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))