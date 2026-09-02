"""FastAPI application: REST API, phone sensor WebSocket, UI events WebSocket,
static dashboard. Single process; Ollama is the only heavy dependency at rest.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent.loop import run_chat
from .agent.tools import ToolContext
from .config import AppConfig, EmbeddingConfig, ProviderConfig, SttConfig, load_config
from .pipeline.worker import PipelineWorker
from .providers.base import ProviderError
from .providers.factory import make_embedder, make_provider
from .providers.ollama_provider import OllamaProvider
from .providers.openai_compat import OpenAICompatProvider
from .runs.manager import Run, RunManager
from .security.pairing import PairingManager
from .server.sensor import SensorHub
from .stt import FasterWhisperStt, SttUnavailable
from .utils import iso, lan_address_candidates, pairing_uri, utcnow

STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    """Everything the routes need; one instance per server process."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.run_manager = RunManager(cfg)
        self.pairing = PairingManager(cfg.server.data_dir)
        self.vision: OllamaProvider | OpenAICompatProvider | None = None
        self.reasoning: OllamaProvider | OpenAICompatProvider | None = None
        self.embedder: Any | None = None
        self.stt: FasterWhisperStt | None = None
        self.current_run: Run | None = None
        self.worker: PipelineWorker | None = None
        self.sensor: SensorHub | None = None
        self.ui_clients: set[WebSocket] = set()
        self.paused = False
        self.relay_channel_id: str | None = None
        self.relay_connected = False
        self.relay_status = "disabled"
        self._make_providers()

    # ----------------------------------------------------------- providers

    def _make_providers(self) -> None:
        self.vision = make_provider(self.cfg.vision, self.cfg.ollama_url)
        self.reasoning = make_provider(self.cfg.reasoning, self.cfg.ollama_url)
        try:
            self.embedder = make_embedder(self.cfg.embeddings, self.cfg.ollama_url)
        except ValueError:
            self.embedder = None
        self.stt = FasterWhisperStt(
            model=self.cfg.stt.model, device=self.cfg.stt.device,
            compute_type=self.cfg.stt.compute_type,
        ) if self.cfg.stt.enabled else None

    async def apply_model_config(self, stage: str, data: dict[str, Any]) -> None:
        target: ProviderConfig = getattr(self.cfg, stage)
        allowed = {"kind", "model", "base_url", "api_key", "num_ctx", "keep_alive", "num_gpu",
                   "enable_thinking", "temperature", "repeat_penalty"}
        for key, value in data.items():
            if key in allowed and value is not None:
                if key == "num_ctx":
                    value = int(value)
                elif key == "num_gpu":
                    value = int(value) if str(value).strip() else None
                elif key == "keep_alive" and isinstance(value, str):
                    value = int(value.strip()) if value.strip() in {"-1", "0"} else value.strip()
                setattr(target, key, value)
        old = getattr(self, stage)
        self._make_providers()
        if old is not None:
            await old.aclose()
        # A running pipeline picks up the new vision provider immediately.
        if self.worker is not None and stage == "vision":
            self.worker.vision = self.vision
        save_config_toml(self.cfg)

    def apply_embeddings_config(self, data: dict[str, Any]) -> None:
        allowed = {"enabled", "kind", "model", "keep_alive"}
        for key, value in data.items():
            if key in allowed and value is not None:
                setattr(self.cfg.embeddings, key, value)
        self._make_providers()
        if self.worker is not None:
            self.worker.embedder = self.embedder
        save_config_toml(self.cfg)

    # ------------------------------------------------------------ run state

    def run_state(self) -> str:
        if self.current_run is None:
            return "idle"
        if self.paused:
            return "paused"
        if self.sensor and self.sensor.connected:
            return "running"
        return "degraded"  # run active, phone offline: state survives

    def cloud_used(self) -> dict[str, bool]:
        return {
            "vision": self.cfg.vision.is_cloud(),
            "reasoning": self.cfg.reasoning.is_cloud(),
        }

    async def status_snapshot(self) -> dict[str, Any]:
        vs = await self.vision.status() if self.vision else None
        rs = await self.reasoning.status() if self.reasoning else None
        return {
            "time": iso(),
            "run_state": self.run_state(),
            "cloud_used": self.cloud_used(),
            "run": {
                "id": self.current_run.id,
                "name": self.current_run.meta.get("name"),
                "created_at": self.current_run.meta.get("created_at"),
                "stats": self.current_run.store.stats(),
            } if self.current_run else None,
            "sensor": self.sensor.sensors.to_dict() if self.sensor else None,
            "relay": {
                "configured": bool(self.cfg.server.relay_url),
                "connected": self.relay_connected,
                "status": self.relay_status,
                "channel_id": self.relay_channel_id,
            },
            "pipeline": self.worker.status.to_dict() if self.worker else None,
            "intake": self.worker.intake.stats.to_dict() if self.worker else None,
            "models": {
                "vision": vs.to_dict() if vs else None,
                "reasoning": rs.to_dict() if rs else None,
            },
            "embeddings": {
                "enabled": self.embedder is not None,
                "model": self.cfg.embeddings.model,
                "worker_state": self.worker.status.embeddings_enabled if self.worker else None,
            },
            "stt": {
                "enabled": self.stt is not None,
                "model": self.cfg.stt.model,
            },
            "config": {
                "vision": _cfg_dict(self.cfg.vision),
                "reasoning": _cfg_dict(self.cfg.reasoning),
                "ollama_url": self.cfg.ollama_url,
            },
        }

    # ---------------------------------------------------------- ui events

    async def broadcast_ui(self, message: dict[str, Any] | None = None) -> None:
        payload = json.dumps(message or {"type": "status", "state": self.run_state()},
                             ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.ui_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ui_clients.discard(ws)


def _cfg_dict(p: ProviderConfig) -> dict[str, Any]:
    out = {
        "kind": p.kind, "model": p.model, "num_ctx": p.num_ctx,
        "keep_alive": p.keep_alive, "num_gpu": p.num_gpu, "enable_thinking": p.enable_thinking,
        "temperature": p.temperature,
        "repeat_penalty": p.repeat_penalty,
    }
    if p.is_cloud():
        out["base_url"] = p.base_url
        out["has_api_key"] = bool(p.api_key)
    return out


def save_config_toml(cfg: AppConfig) -> None:
    """Persist current config to config.toml next to the package root."""
    def prov(p: ProviderConfig) -> str:
        lines = [f'kind = "{p.kind}"', f'model = "{p.model}"']
        if p.is_cloud():
            lines.append(f'base_url = "{p.base_url}"')
            lines.append(f'api_key = "{p.api_key}"')
        lines += [
            f"num_ctx = {p.num_ctx}",
            (f"keep_alive = {p.keep_alive}" if isinstance(p.keep_alive, (int, float))
             else f'keep_alive = "{p.keep_alive}"'),
            *([f"num_gpu = {p.num_gpu}"] if p.num_gpu is not None else []),
            f"enable_thinking = {str(p.enable_thinking).lower()}",
            f"temperature = {p.temperature}",
            *([f"repeat_penalty = {p.repeat_penalty}"] if p.repeat_penalty is not None else []),
        ]
        return "\n".join(lines)

    content = f"""# Visual Memory Agent configuration (auto-saved)
ollama_url = "{cfg.ollama_url}"

[vision]
{prov(cfg.vision)}

[reasoning]
{prov(cfg.reasoning)}

[pipeline]
change_mad_threshold = {cfg.pipeline.change_mad_threshold}
change_hash_threshold = {cfg.pipeline.change_hash_threshold}
intake_queue_capacity = {cfg.pipeline.intake_queue_capacity}
min_interval_ms = {cfg.pipeline.min_interval_ms}
max_interval_ms = {cfg.pipeline.max_interval_ms}
save_frames = "{cfg.pipeline.save_frames}"
media_max_side = {cfg.pipeline.media_max_side}
media_jpeg_quality = {cfg.pipeline.media_jpeg_quality}
media_retention_minutes = {cfg.pipeline.media_retention_minutes}
media_budget_bytes = {cfg.pipeline.media_budget_bytes}
voice_note_context_minutes = {cfg.pipeline.voice_note_context_minutes}

[embeddings]
enabled = {str(cfg.embeddings.enabled).lower()}
kind = "{cfg.embeddings.kind}"
model = "{cfg.embeddings.model}"
keep_alive = {cfg.embeddings.keep_alive if isinstance(cfg.embeddings.keep_alive, (int, float)) else f'"{cfg.embeddings.keep_alive}"'}

[stt]
enabled = {str(cfg.stt.enabled).lower()}
model = "{cfg.stt.model}"
compute_type = "{cfg.stt.compute_type}"
device = "{cfg.stt.device}"
language = {f'"{cfg.stt.language}"' if cfg.stt.language else '""'}

[server]
host = "{cfg.server.host}"
port = {cfg.server.port}
relay_url = "{cfg.server.relay_url}"
relay_reg_token = "{cfg.server.relay_reg_token}"
"""
    (Path(__file__).resolve().parent.parent / "config.toml").write_text(content, encoding="utf-8")


# --------------------------------------------------------------------- app

state: AppState | None = None


def get_state() -> AppState:
    assert state is not None, "app state not initialised"
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    cfg = load_config()
    state = AppState(cfg)
    state.sensor = SensorHub(state)
    app.state.vma = state

    async def status_loop():
        while True:
            await asyncio.sleep(5)
            await state.broadcast_ui()

    task = asyncio.create_task(status_loop(), name="ui-status-loop")
    # Relay client (M4): started only when configured; never blocks startup.
    relay_task = None
    if cfg.server.relay_url:
        try:
            from .server.relay_client import RelayClient
            relay_task = asyncio.create_task(RelayClient(state).run(), name="relay-client")
        except Exception as exc:  # relay is optional infrastructure
            print(f"[relay] failed to start: {exc}")
    yield
    task.cancel()
    if relay_task:
        relay_task.cancel()
    # Shutdown is one of the sanctioned unload moments.
    for prov in (state.vision, state.reasoning):
        if isinstance(prov, OllamaProvider):
            try:
                await prov.unload()
            except Exception:
                pass
        if prov is not None:
            try:
                await prov.aclose()
            except Exception:
                pass
    if state.worker:
        await state.worker.stop()
    if state.current_run:
        state.current_run.close()


app = FastAPI(title="Visual Memory Agent", lifespan=lifespan)


# ------------------------------------------------------------- basic routes

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"ok": "true", "time": iso()}


# --------------------------------------------------------------- pairing

@app.get("/api/pairing/code")
async def new_pairing_code(force: bool = False) -> dict[str, Any]:
    st = get_state()
    code = None if force else st.pairing.current_code()
    return {"code": code or st.pairing.new_code(), "expires_in_s": 600}


@app.get("/api/pairing/info")
async def pairing_info() -> dict[str, Any]:
    st = get_state()
    port = st.cfg.server.port
    _, ranked = _lan_address_candidates()
    return {
        "local_url": f"http://localhost:{port}",
        "lan_urls": [f"http://{address}:{port}" for address in ranked],
        "port": port,
    }


@app.get("/api/pairing/qr.svg")
async def pairing_qr_svg() -> Response:
    """QR encoding vma://pair?host=ip:port&code=CODE — the Android app handles
    the deep link (scannable with the phone's built-in camera app)."""
    import io

    import qrcode
    from qrcode.image.svg import SvgPathImage

    st = get_state()
    port = st.cfg.server.port
    _, ranked = _lan_address_candidates()
    host = f"{ranked[0]}:{port}" if ranked else f"localhost:{port}"
    code = st.pairing.current_code() or st.pairing.new_code()
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(pairing_uri(host, code))
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/pair")
async def pair_device(body: dict[str, Any]) -> dict[str, Any]:
    st = get_state()
    result = st.pairing.pair(str(body.get("code", "")), str(body.get("device_name", "")))
    if result is None:
        raise HTTPException(401, "invalid, expired or used pairing code")
    return result


@app.get("/api/devices")
async def list_devices() -> list[dict[str, Any]]:
    return get_state().pairing.list_devices()


@app.delete("/api/devices/{device_id}")
async def revoke_device(device_id: str) -> dict[str, Any]:
    if not get_state().pairing.revoke(device_id):
        raise HTTPException(404, "device not found")
    return {"revoked": device_id}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return await get_state().status_snapshot()


# -------------------------------------------------------------------- runs

@app.post("/api/runs")
async def create_run(body: dict[str, Any]) -> dict[str, Any]:
    st = get_state()
    if st.current_run is not None:
        raise HTTPException(409, "a run is already active; stop it first")
    name = str(body.get("name") or f"run_{iso()}").strip()
    device = body.get("device") or {}
    run = st.run_manager.create_run(name, device=device)
    run.meta["started_at"] = iso()
    run.save_metadata()
    run.store.add_device_event("run_start", {"device": device})
    st.current_run = run
    assert st.vision is not None
    st.worker = PipelineWorker(
        st.vision, run.store, st.cfg.pipeline, run.dir,
        device_id=str((device or {}).get("device_id", "unknown")),
        embedder=st.embedder,
    )
    st.worker.start()
    await st.broadcast_ui()
    await st.sensor.push_status()  # a phone that connected before the run learns it now
    return {"run_id": run.id, "name": name}


@app.post("/api/runs/current/stop")
async def stop_run() -> dict[str, Any]:
    st = get_state()
    if st.current_run is None:
        raise HTTPException(404, "no active run")
    if st.worker:
        await st.worker.stop()
    run = st.current_run
    run.close()
    st.current_run = None
    st.worker = None
    await st.broadcast_ui()
    await st.sensor.push_status()
    return {"stopped": run.id}


@app.post("/api/runs/current/pause")
async def pause_run(body: dict[str, Any]) -> dict[str, Any]:
    st = get_state()
    st.paused = bool(body.get("paused", True))
    await st.broadcast_ui()
    await st.sensor.push_status()
    return {"paused": st.paused}


# ------------------------------------------------- authenticated commands
# Mobile -> desktop remote commands reuse the device-token pairing auth (no
# second auth system) and an explicit allowlist. No shell, no arbitrary
# desktop execution — every command maps to an existing sandboxed action.

COMMAND_ALLOWLIST = {"get_status", "pause", "resume", "stop_run", "append_note", "chat",
                     "get_observations", "get_observation_image", "list_runs"}


def _resolve_command_run(st: AppState, args: dict[str, Any]):
    """Target run for a command: args.run_id (any past run) or the active one."""
    run_id = str(args.get("run_id") or "").strip()
    if run_id:
        run = st.run_manager.open_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id!r} not found")
        return run
    if st.current_run is None:
        raise LookupError("no active run (or pass run_id)")
    return st.current_run


async def execute_command(st: AppState, command: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one allowlisted command. Raises PermissionError/ValueError/LookupError."""
    command = str(command or "").strip()
    if command not in COMMAND_ALLOWLIST:
        raise ValueError(
            f"unknown command {command!r}; allowed: {sorted(COMMAND_ALLOWLIST)}"
        )
    if command == "get_status":
        snap = await st.status_snapshot()
        return {
            "run_state": snap["run_state"],
            "run": {"id": snap["run"]["id"], "name": snap["run"]["name"]} if snap["run"] else None,
            "paused": st.paused,
            "sensor_connected": bool(st.sensor and st.sensor.connected),
        }
    if command == "pause":
        st.paused = True
        await st.broadcast_ui()
        await st.sensor.push_status()
        return {"paused": True}
    if command == "resume":
        st.paused = False
        await st.broadcast_ui()
        await st.sensor.push_status()
        return {"paused": False}
    if command == "stop_run":
        if st.current_run is None:
            raise LookupError("no active run")
        if st.worker:
            await st.worker.stop()
        run = st.current_run
        run.close()
        st.current_run = None
        st.worker = None
        await st.broadcast_ui()
        await st.sensor.push_status()
        return {"stopped": run.id}
    if command == "append_note":
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("append_note requires text")
        if st.current_run is None:
            raise LookupError("no active run")
        note_id = st.current_run.store.add_note(text, author="user")
        return {"note_id": note_id}
    if command == "chat":
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("chat requires text")
        if st.reasoning is None:
            raise LookupError("no reasoning provider")
        run = _resolve_command_run(st, args)
        ctx = ToolContext(run=run, vision=st.vision, embedder=st.embedder)
        outcome = await run_chat(st.reasoning, ctx, text)
        return {"answer": outcome.answer, "tool_calls": [t["name"] for t in outcome.tool_trace]}
    if command == "list_runs":
        runs = st.run_manager.list_runs()
        return {"runs": [
            {
                "run_id": r["run_id"],
                "name": r.get("name"),
                "created_at": r.get("created_at"),
                "ended_at": r.get("ended_at"),
                "size_mb": round((r.get("size_bytes") or 0) / 1e6, 1),
                "active": bool(st.current_run and st.current_run.id == r["run_id"]),
            }
            for r in runs
        ]}
    if command == "get_observations":
        run = _resolve_command_run(st, args)
        limit = max(1, min(int(args.get("limit") or 10), 100))
        out = []
        for obs in reversed(run.store.recent_observations(limit=limit)):
            frame = run.store.frame_by_id(obs["frame_id"]) if obs.get("frame_id") else None
            payload = obs.get("payload") or {}
            out.append({
                "id": obs["id"],
                "ts": obs["ts"],
                "local": obs.get("local_ts"),
                "kind": obs.get("kind"),
                "summary": (obs.get("summary") or "")[:160],
                "importance": obs.get("importance"),
                "media": frame.get("path") if frame else None,
                "links": len(payload.get("linked_ids") or []),
            })
        return {"run_id": run.id, "observations": out}
    if command == "get_observation_image":
        run = _resolve_command_run(st, args)
        obs_id = int(args.get("observation_id") or 0)
        obs = run.store.get_observation(obs_id)
        if obs is None:
            raise LookupError(f"observation {obs_id} not found")
        frame = run.store.frame_by_id(obs["frame_id"]) if obs.get("frame_id") else None
        media_rel = frame.get("path") if frame else None
        if not media_rel:
            raise LookupError("observation has no retained image")
        path = run.media_path(media_rel)
        if path is None:
            raise LookupError("media file missing on disk")
        data = path.read_bytes()
        if len(data) > 6_000_000:
            raise ValueError("image too large to send")
        import base64 as _b64
        return {"observation_id": obs_id, "summary": (obs.get("summary") or "")[:160],
                "image_b64": _b64.b64encode(data).decode("ascii")}
    raise ValueError(f"unknown command {command!r}")  # pragma: no cover


@app.post("/api/command")
async def command_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    """Authenticated mobile/desktop-remote command channel."""
    st = get_state()
    device_id = st.pairing.verify_token(str(body.get("token") or ""))
    if device_id is None:
        raise HTTPException(401, "invalid or revoked device token")
    try:
        result = await execute_command(st, str(body.get("command") or ""),
                                       body.get("args") or {})
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "device_id": device_id, "result": result}


@app.post("/api/models/embeddings")
async def set_embeddings_config(body: dict[str, Any]) -> dict[str, Any]:
    st = get_state()
    st.apply_embeddings_config(body)
    await st.broadcast_ui()
    return {"ok": True, "embeddings": {
        "enabled": st.embedder is not None, "model": st.cfg.embeddings.model,
    }}


@app.post("/api/models/embeddings/preload")
async def preload_embeddings() -> dict[str, Any]:
    """Warm the embedding model so the first committed observation doesn't pay
    the load cost. Cheap: one tiny embed call."""
    st = get_state()
    if st.embedder is None:
        raise HTTPException(503, "embeddings disabled in config")
    try:
        await st.embedder.embed(["preload"])
    except ProviderError as exc:
        raise HTTPException(502, str(exc))
    return {"preloaded": st.cfg.embeddings.model}


@app.post("/api/config/pipeline")
async def set_pipeline_config(body: dict[str, Any]) -> dict[str, Any]:
    """Storage policy (what images to keep, how small, how long, budget cap).

    Values apply immediately to the running worker; nothing is backfilled to
    media already on disk except by the budget eviction/retention sweeps.
    """
    st = get_state()
    allowed = {
        "save_frames": str,
        "media_max_side": int,
        "media_jpeg_quality": int,
        "media_retention_minutes": int,
        "media_budget_bytes": int,
    }
    applied: dict[str, Any] = {}
    for key, cast in allowed.items():
        if key in body and body[key] is not None:
            value = cast(body[key])
            if key == "save_frames" and value not in ("none", "important", "all"):
                raise HTTPException(400, "save_frames must be none|important|all")
            if key == "media_max_side" and not (64 <= value <= 10_000):
                raise HTTPException(400, "media_max_side out of range")
            if key == "media_jpeg_quality" and not (10 <= value <= 100):
                raise HTTPException(400, "media_jpeg_quality out of range")
            if key in ("media_retention_minutes", "media_budget_bytes") and value < 0:
                raise HTTPException(400, f"{key} must be >= 0 (0 = unlimited)")
            setattr(st.cfg.pipeline, key, value)
            applied[key] = value
    if st.worker is not None:
        st.worker.cfg = st.cfg.pipeline
    save_config_toml(st.cfg)
    return {"ok": True, "applied": applied}


# ------------------------------------------------------------------- voice

@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request, send_to_agent: bool = False) -> dict[str, Any]:
    """Push-to-talk: upload a short recording (raw audio body), get a local
    CPU transcription. Continuous listening does not exist by design;
    recordings are processed in memory and never written to disk.
    """
    st = get_state()
    if st.stt is None:
        raise HTTPException(503, "STT disabled in config ([stt] enabled = true to opt in)")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "empty audio upload")
    try:
        result = await st.stt.transcribe(audio, language=st.cfg.stt.language)
    except SttUnavailable as exc:
        raise HTTPException(503, str(exc))
    out: dict[str, Any] = {"transcription": result.to_dict()}
    if send_to_agent and result.text:
        if st.current_run is None or st.reasoning is None:
            out["agent_error"] = "no active run"
        else:
            ctx = ToolContext(run=st.current_run, vision=st.vision, embedder=st.embedder)
            try:
                outcome = await run_chat(st.reasoning, ctx, result.text)
                out["agent_answer"] = outcome.answer
            except ProviderError as exc:
                out["agent_error"] = str(exc)
    return out


# ------------------------------------------------------- phone voice notes

async def _commit_voice_note(st: AppState, result: Any, *, source: str = "push_to_talk") -> dict[str, Any]:
    """Turn a transcription into a memory observation linked to the timeline.

    A voice note is an ordinary committed observation (kind='voice'): it is
    embedded once, joins FTS/semantic search, and — like every observation —
    is never merged or deleted for being similar to anything else. Links are
    additive metadata: temporal neighbors (± window) plus semantic neighbors
    (cosine >= 0.45) recorded in payload['linked_ids'].

    `source` distinguishes manual push-to-talk notes from the phone's opt-in
    continuous-audio segments ("continuous").
    """
    from datetime import timedelta

    from .store.db import vec_to_bytes

    run = st.current_run
    assert run is not None
    now = utcnow()
    ts = iso(now)
    window = max(0, int(st.cfg.pipeline.voice_note_context_minutes))
    temporal_ids = [
        o["id"] for o in run.store.observations_in_range(
            iso(now - timedelta(minutes=window)), iso(now + timedelta(minutes=window)),
            limit=200,
        )
    ]
    semantic_ids: list[int] = []
    vec = None
    if st.embedder is not None and result.text.strip():
        try:
            vectors = await st.embedder.embed([result.text[:1200]])
            vec = vectors[0]
            semantic_ids = [
                s["id"] for s in run.store.semantic_search(vec, limit=6)
                if s.get("similarity", 0.0) >= 0.45
            ]
        except Exception as exc:  # links are best-effort; the note still commits
            run.store.add_metric("voice_note_embed_error", 1, {"error": str(exc)[:200]})
    linked_ids = sorted(set(temporal_ids) | set(semantic_ids))
    obs_id = run.store.add_observation(
        frame_id=None,
        ts=ts,
        kind="voice",
        scene="voice_note",
        summary=result.text[:200],
        payload={
            "kind": "voice_note",
            "source": source,
            "transcript": result.text,
            "language": result.language,
            "duration_s": result.duration_s,
            "stt_model": result.model,
            "elapsed_ms": result.elapsed_ms,
            "linked_ids": linked_ids,
            "link_counts": {"temporal": len(temporal_ids), "semantic": len(semantic_ids)},
            "audio_retained": False,
        },
        importance=2,
        importance_reason="user voice note",
        provider="stt",
        model=result.model,
        latency_ms=result.elapsed_ms,
    )
    if vec is not None:
        run.store.set_observation_embedding(obs_id, len(vec), st.embedder.model, vec_to_bytes(vec))
    return {"observation_id": obs_id, "links": linked_ids,
            "link_counts": {"temporal": len(temporal_ids), "semantic": len(semantic_ids)}}


@app.post("/api/voice/note")
async def voice_note(request: Request, source: str = "push_to_talk") -> dict[str, Any]:
    """Phone push-to-talk voice note: transcribe locally, commit as an
    observation linked to nearby/similar observations. Requires a token from
    a paired device when sent with one; audio is memory-only. The opt-in
    continuous-audio mode sends source="continuous" 30s segments."""
    st = get_state()
    token = request.headers.get("x-vma-token") or ""
    if token and st.pairing.verify_token(token) is None:
        raise HTTPException(401, "invalid or revoked device token")
    if st.stt is None:
        raise HTTPException(503, "STT disabled in config ([stt] enabled = true to opt in)")
    if st.current_run is None:
        raise HTTPException(409, "no active run to attach the voice note to")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "empty audio upload")
    try:
        result = await st.stt.transcribe(audio, language=st.cfg.stt.language)
    except SttUnavailable as exc:
        raise HTTPException(503, str(exc))
    if not result.text:
        return {"saved": False, "reason": "nothing recognized"}
    note = await _commit_voice_note(st, result, source=source if source in ("push_to_talk", "continuous") else "push_to_talk")
    await st.broadcast_ui()
    return {"saved": True, "transcript": result.text[:400], **note}


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    return get_state().run_manager.list_runs()


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    run = _open_or_404(run_id)
    return {
        "meta": run.meta,
        "stats": run.store.stats(),
        "recent_observations": run.store.recent_observations(limit=50),
        "reports": run.store.list_reports(),
    }


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    st = get_state()
    if st.current_run and st.current_run.id == run_id:
        raise HTTPException(409, "stop the active run before deleting it")
    if not st.run_manager.delete_run(run_id):
        raise HTTPException(404, "run not found")
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/observations")
async def run_observations(run_id: str, limit: int = 100, min_importance: int = 0) -> list[dict[str, Any]]:
    run = _open_or_404(run_id)
    obs = [o for o in run.store.recent_observations(limit=500)
           if (o.get("importance") or 0) >= min_importance]
    return obs[-limit:]


@app.get("/api/runs/{run_id}/media/{rel_path:path}")
async def run_media(run_id: str, rel_path: str) -> FileResponse:
    """Serve a stored frame image; path traversal is rejected."""
    run = _open_or_404(run_id)
    path = get_state().run_manager.run_media_path(run, rel_path)
    if path is None:
        raise HTTPException(404, "media not found")
    return FileResponse(path, media_type="image/jpeg")


@app.delete("/api/runs/{run_id}/media")
async def wipe_run_media(run_id: str) -> dict[str, Any]:
    """Privacy control: delete all retained images; observations/text remain
    (and are documented as still potentially sensitive on their own)."""
    run = _open_or_404(run_id)
    count = run.store.delete_all_media_rows()
    media_dir = run.dir / "media"
    if media_dir.exists():
        for f in media_dir.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass
    await get_state().broadcast_ui()
    return {"deleted_files": count, "note": "observation text was NOT deleted"}


@app.get("/api/runs/{run_id}/files/{rel_path:path}")
async def run_file(run_id: str, rel_path: str) -> FileResponse:
    """Serve text files (reports/exports) from the run dir."""
    run = _open_or_404(run_id)
    path = get_state().run_manager.run_media_path(run, rel_path)
    if path is None or path.suffix not in (".md", ".txt", ".json"):
        raise HTTPException(404, "file not found")
    return FileResponse(path, media_type="text/markdown")


def _open_or_404(run_id: str) -> Run:
    run = get_state().run_manager.open_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


# ------------------------------------------------------------------ models

@app.get("/api/ollama/models")
async def ollama_models() -> list[dict[str, Any]]:
    st = get_state()
    if st.vision is None:
        raise HTTPException(503, "no provider")
    try:
        return await st.vision.installed_models()
    except ProviderError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/models/status")
async def models_status() -> dict[str, Any]:
    st = get_state()
    vs = await st.vision.status() if st.vision else None
    rs = await st.reasoning.status() if st.reasoning else None
    return {"vision": vs.to_dict() if vs else None,
            "reasoning": rs.to_dict() if rs else None}


@app.post("/api/models/{stage}")
async def set_model(stage: str, body: dict[str, Any]) -> dict[str, Any]:
    if stage not in ("vision", "reasoning"):
        raise HTTPException(404, "stage must be vision|reasoning")
    st = get_state()
    try:
        await st.apply_model_config(stage, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await st.broadcast_ui()
    return {"ok": True, "config": _cfg_dict(getattr(st.cfg, stage))}


@app.post("/api/models/{stage}/preload")
async def preload_model(stage: str) -> dict[str, Any]:
    prov = _stage_provider(stage)
    try:
        await prov.preload()
    except ProviderError as exc:
        raise HTTPException(502, str(exc))
    return {"preloaded": stage}


@app.post("/api/models/{stage}/unload")
async def unload_model(stage: str) -> dict[str, Any]:
    prov = _stage_provider(stage)
    try:
        await prov.unload()
    except ProviderError as exc:
        raise HTTPException(502, str(exc))
    await get_state().broadcast_ui()
    return {"unloaded": stage}


def _stage_provider(stage: str):
    st = get_state()
    prov = getattr(st, stage)
    if prov is None:
        raise HTTPException(503, "provider not initialised")
    return prov


def _lan_address_candidates() -> tuple[str | None, list[str]]:
    """Thin wrapper so route code and tests share one implementation."""
    return lan_address_candidates()


# --------------------------------------------------------------- websockets

@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket) -> None:
    st = get_state()
    await st.sensor.handle(ws, transport="lan")


@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket) -> None:
    st = get_state()
    await ws.accept()
    st.ui_clients.add(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_ui_message(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        st.ui_clients.discard(ws)


async def _handle_ui_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    st = get_state()
    mtype = msg.get("type")

    if mtype == "chat":
        run_id = msg.get("run_id")
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        run = st.current_run if (st.current_run and st.current_run.id == run_id) \
            else st.run_manager.open_run(str(run_id))
        if run is None:
            await ws.send_text(json.dumps({"type": "chat_error", "message": "run not found"}))
            return
        ctx = ToolContext(run=run, vision=st.vision, embedder=st.embedder)
        try:
            outcome = await run_chat(
                st.reasoning, ctx, text,
                on_delta=lambda piece: _ws_send(ws, {"type": "chat_delta", "text": piece}),
                on_status=lambda s: _ws_send(ws, {"type": "chat_status", "status": s}),
            )
            await _ws_send(ws, {
                "type": "chat_done",
                "answer": outcome.answer,
                "tool_trace": outcome.tool_trace,
                "iterations": outcome.iterations,
                "total_ms": outcome.total_ms,
            })
            await st.broadcast_ui()
        except ProviderError as exc:
            await _ws_send(ws, {"type": "chat_error", "message": str(exc)})

    elif mtype == "generate_report":
        run_id = msg.get("run_id")
        kind = str(msg.get("kind") or "chronological")
        run = st.run_manager.open_run(str(run_id)) if run_id else st.current_run
        if run is None:
            await _ws_send(ws, {"type": "chat_error", "message": "run not found"})
            return
        ctx = ToolContext(run=run, vision=st.vision, embedder=st.embedder)
        instruction = (
            f"Generate a {kind} report for this run using the observation store. "
            "Retrieve the data you need, then call create_report with a well-structured "
            "markdown document (headings, timeline, locations, issues found). "
            "Use get_run_stats and distinguish no-change frames from processing-error frames; never describe an error "
            "frame as a scene that the vision model successfully evaluated. "
            f"Title it appropriately for a {kind} report. Confirm the saved path when done."
        )
        try:
            outcome = await run_chat(
                st.reasoning, ctx, instruction,
                on_status=lambda s: _ws_send(ws, {"type": "chat_status", "status": s}),
            )
            await _ws_send(ws, {"type": "report_done", "answer": outcome.answer,
                                "reports": run.store.list_reports()})
        except ProviderError as exc:
            await _ws_send(ws, {"type": "chat_error", "message": str(exc)})


async def _ws_send(ws: WebSocket, obj: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(obj, ensure_ascii=False, default=str))


# ------------------------------------------------------------------ static

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run("vma.app:app", host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()
