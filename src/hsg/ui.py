"""交互式 Web UI：在浏览器里选参数、看选题池/生成记录、点按钮生成。

是 CLI 的薄封装——读 config/topics/history（复用 hsg 既有模块），
把用户在界面上选的参数覆盖到 cfg，起后台线程跑 pipeline.run，
日志通过 SSE 实时推到前端。

启动：python -m hsg.ui  或  run.bat ui
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

from .config import Config, ApiKeys, assert_text_provider, load_config
from . import history as history_mod
from . import topics as topics_mod

log = logging.getLogger("hsg.ui")

# ---- 日志队列：pipeline 跑的时候把日志推进来，SSE 从这里读 ----
_log_queue: queue.Queue[str] = queue.Queue(maxsize=2000)


class _QueueHandler(logging.Handler):
    """把日志记录推进 queue，供 SSE 实时读取。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _log_queue.put_nowait(msg)
        except queue.Full:
            pass  # 队列满了丢掉旧的日志，不阻塞 pipeline


def _install_log_handler() -> None:
    """给 hsg 的 logger 挂上 QueueHandler，让 pipeline 日志能推到前端。"""
    hsg_logger = logging.getLogger("hsg")
    h = _QueueHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s",
                                     datefmt="%H:%M:%S"))
    hsg_logger.addHandler(h)
    hsg_logger.setLevel(logging.INFO)


# ---- 任务状态 ----
_task: dict = {"running": False, "error": None, "result": None, "started": None}

# 静态文件目录（index.html 在这里）
_STATIC_DIR = Path(__file__).resolve().parent / "ui_static"

app = Flask(__name__, static_folder=None)


def _load_cfg() -> Config:
    """加载 config.yaml。每次请求都重新加载——用户可能改过配置。"""
    return load_config(None)


# ================================================================ API
@app.route("/")
def index():
    return send_from_directory(str(_STATIC_DIR), "index.html")


@app.route("/api/config")
def api_config():
    """返回当前配置 + 可选项（类型列表、模型选项、音色参考）。"""
    cfg = _load_cfg()
    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
    types = user.merged_types() if user else topics_mod.STORY_TYPES
    series = user.series_names() if user else []
    return json.dumps({
        "story": {
            "angle_mode": str(cfg.story.get("angle_mode") or "small"),
            "chapters": int(cfg.story.get("chapters", 6)),
            "seconds_per_chapter": int(cfg.story.get("seconds_per_chapter", 68)),
            "target_total_seconds": int(cfg.story.get("target_total_seconds", 420)),
            "chars_per_second": float(cfg.story.get("chars_per_second", 4.70)),
        },
        "llm": {
            "provider": str(cfg.llm.get("provider") or "deepseek"),
            "outline_provider": str(cfg.llm.get("outline_provider") or cfg.llm.get("provider") or "deepseek"),
        },
        "verify": {"provider": str(cfg.verify.get("provider") or "deepseek")},
        "tts": {
            "provider": str(cfg.tts.get("provider") or "minimax"),
            "voice_id": str(cfg.tts.get("minimax", {}).get("voice_id", "audiobook_male_1")),
            "speed": float(cfg.tts.get("minimax", {}).get("speed", 1.1)),
        },
        "types": types,
        "series": series,
        # 参考音色（不调 API，用户可手填别的）
        "voice_refs": [
            {"id": "audiobook_male_1", "desc": "系统音色·有声书男声（默认）"},
            {"id": "male-qn-jingying", "desc": "系统音色·资讯播报男声"},
            {"id": "hsg_story_v2", "desc": "克隆音色（需先克隆）"},
        ],
        "providers": {"llm": ["deepseek", "minimax"], "tts": ["minimax", "edge"]},
    }, ensure_ascii=False, indent=2)


@app.route("/api/topics")
def api_topics():
    """返回选题池：系列条目（按系列分组）+ 非系列选题。"""
    cfg = _load_cfg()
    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
    if not user:
        return json.dumps({"series": {}, "standalone": [], "series_desc": {}}, ensure_ascii=False)
    mode = str(cfg.story.get("angle_mode") or "small")

    # 系列条目按系列分组
    series_data: dict[str, list] = {}
    for t in user.episodes():  # 所有系列条目
        s = t.series or ""
        if s not in series_data:
            series_data[s] = []
        done = history_mod.is_used(cfg, t.title)
        series_data[s].append({
            "ep": t.ep, "title": t.title, "type": t.type or "",
            "desc": t.desc or "", "done": done,
        })

    # 非系列选题
    standalone = []
    for t in user.items(mode):
        done = history_mod.is_used(cfg, t.title)
        standalone.append({
            "title": t.title, "type": t.type or "",
            "desc": t.desc or "", "done": done,
        })

    return json.dumps({
        "series": series_data,
        "series_desc": dict(user.series),
        "standalone": standalone,
    }, ensure_ascii=False, indent=2)


@app.route("/api/history")
def api_history():
    """返回生成记录。"""
    cfg = _load_cfg()
    recs = history_mod.load(cfg)
    return json.dumps(recs, ensure_ascii=False, indent=2)


@app.route("/api/series/<name>/progress")
def api_series_progress(name: str):
    """返回系列进度（done/total）。"""
    cfg = _load_cfg()
    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
    if not user:
        return json.dumps({"done": 0, "total": 0}, ensure_ascii=False)
    eps = user.episodes(name)
    recs = history_mod.load(cfg)
    done_set = {str(r.get("title") or "") for r in recs}
    done = sum(1 for e in eps if e.title in done_set)
    return json.dumps({"done": done, "total": len(eps),
                       "next": next((e.title for e in eps if e.title not in done_set), None)},
                      ensure_ascii=False)


@app.route("/api/topic/add", methods=["POST"])
def api_topic_add():
    """往选题池添加一条选题。校验类型/标题/系列集号，写回 topics_user.json。"""
    cfg = _load_cfg()
    p = request.json or {}
    user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))

    ttype = str(p.get("type", "")).strip()
    title = str(p.get("title", "")).strip()
    desc = str(p.get("desc", "")).strip()
    mode = str(p.get("mode", "small"))
    series = str(p.get("series", "")).strip()
    ep = int(p.get("ep", 0) or 0)
    type_desc = str(p.get("type_desc", "")).strip()
    series_desc = str(p.get("series_desc", "")).strip()

    merged = user.merged_types() if user else topics_mod.STORY_TYPES

    # 校验（照 scripts/add_topic.py 的 do_add 逻辑）
    if not ttype:
        return json.dumps({"ok": False, "error": "必须给类型"}, ensure_ascii=False), 400
    if type_desc:
        user.types[ttype] = type_desc
    elif ttype not in merged:
        return json.dumps({"ok": False, "error": f"没有这个类型：{ttype}。可选：{'、'.join(list(merged)[:6])}…"}, ensure_ascii=False), 400

    if series:
        if ep <= 0:
            return json.dumps({"ok": False, "error": "系列片必须给集号（1 起）"}, ensure_ascii=False), 400
        if series_desc:
            user.series[series] = series_desc
        elif series not in user.series:
            user.series[series] = ""
        same = [t for t in user.episodes(series) if t.ep == ep]
        if same:
            return json.dumps({"ok": False, "error": f"第 {ep} 集已经有了：{same[0].title}"}, ensure_ascii=False), 400

    if not title:
        return json.dumps({"ok": False, "error": "必须给标题"}, ensure_ascii=False), 400
    if len(title) < 8:
        return json.dumps({"ok": False, "error": f"标题太短（{len(title)} 字），建议 15-30 字"}, ensure_ascii=False), 400
    dup = topics_mod.topic_of(title, user)
    if dup.type:
        return json.dumps({"ok": False, "error": f"这条标题池子里已经有了（类型 {dup.type}）"}, ensure_ascii=False), 400

    warn = ""
    if desc and len(desc) < 15:
        warn = f"描述只有 {len(desc)} 字，偏短（建议 35-60 字），但不影响添加"

    user.topics.append(topics_mod.Topic(type=ttype, title=title, desc=desc,
                                        mode=mode, series=series, ep=ep))
    q = topics_mod.save_user_pool(user)
    where = f"《{series}》第 {ep} 集" if series else f"{ttype} · {mode}"
    log.info("选题池新增（%s）：%s", where, title)
    return json.dumps({"ok": True, "message": f"已加入（{where}）：{title}",
                       "file": str(q), "warn": warn}, ensure_ascii=False)


# ---- 生成：覆盖 cfg → 起后台线程跑 pipeline ----
def _apply_overrides(cfg: Config, p: dict) -> None:
    """把 UI 选的参数写回 cfg（照 cli._apply_overrides 的逻辑）。"""
    st = cfg.story
    if p.get("minutes"):
        sec = int(float(p["minutes"]) * 60)
        st["target_total_seconds"] = sec
        st["min_total_seconds"] = max(120, int(sec * 0.75))
        st["max_total_seconds"] = max(300, int(sec * 1.25))
    if p.get("chapters"):
        st["chapters"] = int(p["chapters"])
    if p.get("seconds_per_chapter"):
        st["seconds_per_chapter"] = int(p["seconds_per_chapter"])
    if p.get("llm_provider"):
        cfg.llm["provider"] = str(p["llm_provider"])
    if p.get("outline_provider"):
        cfg.llm["outline_provider"] = str(p["outline_provider"])
    if p.get("verify_provider"):
        cfg.verify["provider"] = str(p["verify_provider"])
    if p.get("tts_provider"):
        cfg.tts["provider"] = str(p["tts_provider"])
    if p.get("voice_id") or p.get("speed"):
        active = str(cfg.tts.get("provider") or "minimax")
        sub = cfg.tts.setdefault(active, {})
        if p.get("voice_id"):
            sub["voice_id"] = str(p["voice_id"])
        if p.get("speed"):
            sub["speed"] = float(p["speed"])
    if p.get("angle_mode"):
        st["angle_mode"] = str(p["angle_mode"])


def _run_pipeline_thread(cfg: Config, params: dict) -> None:
    """后台线程：跑 pipeline.run。日志已被 QueueHandler 捕获推到 _log_queue。"""
    try:
        from .pipeline import run
        from .config import ensure_dirs

        ensure_dirs(cfg)
        keys = ApiKeys.from_env()
        assert_text_provider(cfg)

        # 定选题
        topic_title = params.get("topic", "").strip()
        topic_type = params.get("topic_type", "").strip()
        topic_desc = params.get("topic_desc", "").strip()
        series_name = params.get("series", "").strip()
        series_ep = 0

        # 如果选了系列且没手填标题 → 取下一集
        if series_name and not topic_title:
            user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
            ep = topics_mod.next_episode(user, series_name,
                                         lambda x: history_mod.is_used(cfg, x))
            if ep:
                topic_title = ep.title
                topic_type = ep.type or topic_type
                topic_desc = ep.desc or topic_desc
                series_ep = ep.ep
            else:
                raise RuntimeError(f"系列「{series_name}」没有可做的下一集")
        elif topic_title:
            # 手填标题：查池子带出 series/ep
            user = topics_mod.load_user_pool(topics_mod.user_pool_path(cfg))
            for tp in (user.topics if user else []):
                if tp.title == topic_title:
                    if not topic_type:
                        topic_type = tp.type or ""
                    if not topic_desc:
                        topic_desc = tp.desc or ""
                    series_name = tp.series or series_name
                    series_ep = tp.ep or 0
                    break

        orientations = params.get("orientations", "both")
        if orientations == "both":
            orient = list(cfg.video.orientations.keys())
        else:
            orient = [orientations]

        res = run(
            cfg, topic_title, keys=keys,
            topic_type=topic_type, topic_desc=topic_desc,
            series=series_name, series_ep=series_ep,
            do_images=not params.get("no_images", False),
            do_video=not params.get("no_video", False),
            orientations=orient,
            force=params.get("force", False),
            allow_duplicate=params.get("allow_duplicate", False),
        )
        _task["result"] = res
        _task["error"] = None
        log.info("═══ 生成完成 ═══")
        if res.get("outputs"):
            for o in res["outputs"]:
                log.info("  成片：%s", o)
    except SystemExit as e:
        _task["error"] = f"退出码 {e.code}"
        log.error("生成中止：退出码 %s", e.code)
    except Exception as exc:  # noqa: BLE001
        _task["error"] = str(exc)
        log.error("生成失败：%s", exc)
        log.error(traceback.format_exc())
    finally:
        _task["running"] = False


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """启动生成（后台线程）。返回任务状态。"""
    global _task
    if _task["running"]:
        return json.dumps({"ok": False, "error": "已有生成任务在跑"}), 409

    params = request.json or {}
    cfg = _load_cfg()
    _apply_overrides(cfg, params)

    _task = {"running": True, "error": None, "result": None, "started": time.time()}
    t = threading.Thread(target=_run_pipeline_thread, args=(cfg, params), daemon=True)
    t.start()
    return json.dumps({"ok": True, "message": "生成已启动，日志在下方实时滚动"})


@app.route("/api/task")
def api_task():
    """查询当前任务状态。"""
    return json.dumps({
        "running": _task["running"],
        "error": _task["error"],
        "has_result": _task["result"] is not None,
        "outputs": (_task["result"] or {}).get("outputs", []),
    }, ensure_ascii=False)


@app.route("/api/log/stream")
def api_log_stream():
    """SSE 日志流。前端用 EventSource 连接，实时显示 pipeline 日志。"""
    def generate():
        while True:
            try:
                msg = _log_queue.get(timeout=2)
                # SSE 格式：data: ...\n\n
                yield f"data: {json.dumps({'msg': msg}, ensure_ascii=False)}\n\n"
            except queue.Empty:
                # 心跳：保持连接，顺便告诉前端任务状态
                yield f": keepalive {json.dumps({'running': _task['running']})}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                             "X-Accel-Buffering": "no"},
                    direct_passthrough=True)


def main():
    """启动 UI 服务器。"""
    import webbrowser
    _install_log_handler()
    port = 7890
    url = f"http://127.0.0.1:{port}"
    log.info("UI 启动：%s", url)
    # 延迟开浏览器（等 Flask 起来）
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
