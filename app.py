"""乐谱反复段展开 Web 应用（Flask）。"""

import math
import os
from io import BytesIO

from flask import Flask, jsonify, render_template, request, send_file

from midi import encode_smf
from repeats import (
    MAX_BEATS, MAX_MEASURES, beat_timeline, beats_in_window, expand,
    match_segments, parse, parse_tempo_map, source_range_pattern,
)

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


def _measure_overflow_error(node):
    """展开小节数超限的错误条目。"""
    return {
        "line": node["line"],
        "message": (
            f"展开超过 {MAX_MEASURES} 小节上限，"
            f"在第 {node['line']} 行（小节 {node['num']}）处停止"
        ),
    }


def _parse_window(data):
    """校验起止毫秒参数，返回 (start_ms, end_ms, 错误描述)；通过时错误描述为 None。"""
    values = []
    for label, key in (("起始", "start_ms"), ("结束", "end_ms")):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, None, f"{label}毫秒必须是数字"
        try:
            value = float(value)
        except OverflowError:
            return None, None, f"{label}毫秒数值过大"
        if not math.isfinite(value):
            return None, None, f"{label}毫秒必须是有限数字"
        values.append(value)
    start_ms, end_ms = values
    if start_ms < 0:
        return None, None, "起始毫秒不能为负数"
    if end_ms <= start_ms:
        return None, None, "结束毫秒必须大于起始毫秒"
    return start_ms, end_ms, None


def _parse_measure_number(data, key, label):
    """校验片段定位的端点小节编号，返回 (编号, 错误描述)；通过时错误描述为 None。"""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{label}小节编号必须是非负整数"
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None, f"{label}小节编号必须是非负整数"
        value = int(value)
    if value < 0:
        return None, f"{label}小节编号必须是非负整数"
    return value, None


def _has_tempo(data):
    """请求是否提供了需要校验的临时变速方案（非空文本或非法类型）。"""
    tempo = data.get("tempo")
    return tempo is not None and (not isinstance(tempo, str) or bool(tempo.strip()))


def _parse_tempo(data, total):
    """从请求数据解析临时变速方案，返回 (tempo_map, errors)。

    total 为展开后的总小节数，用于校验序号范围；
    未提供方案时 tempo_map 为 None 且 errors 为空。
    """
    tempo_text = data.get("tempo")
    if tempo_text is None:
        return None, []
    if not isinstance(tempo_text, str):
        return None, [{"line": None, "message": "临时变速方案必须是多行文本"}]
    if not tempo_text.strip():
        return None, []
    tempo_map, errors = parse_tempo_map(tempo_text, total)
    if errors:
        return None, errors
    return tempo_map, []


@app.post("/api/expand")
def api_expand():
    data = request.get_json(silent=True) or {}
    text = data.get("input", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({
            "ok": False,
            "errors": [{"line": None, "message": "输入为空，请逐行输入小节编号"}],
        })

    root, errors = parse(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    rows, overflow = expand(root)
    if overflow is not None:
        return jsonify({
            "ok": False,
            "overflow": True,
            "errors": [_measure_overflow_error(overflow)],
        })

    # 临时变速方案的范围校验依赖展开后的总小节数
    tempo_map, tempo_errors = _parse_tempo(data, len(rows))
    if tempo_errors:
        return jsonify({"ok": False, "errors": tempo_errors})
    if tempo_map:
        rows, _ = expand(root, tempo_map=tempo_map)

    return jsonify({"ok": True, "count": len(rows), "rows": rows})


@app.post("/api/beats")
def api_beats():
    data = request.get_json(silent=True) or {}
    text = data.get("input", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({
            "ok": False,
            "errors": [{"line": None, "message": "输入为空，请逐行输入小节编号"}],
        })

    root, errors = parse(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    start_ms, end_ms, err = _parse_window(data)
    if err is not None:
        return jsonify({"ok": False, "errors": [{"line": None, "message": err}]})

    # 提供了临时变速方案时，先展开一次取得总小节数以校验序号范围；
    # 未提供方案时不做这次展开，行为与之前完全一致
    tempo_map = None
    if _has_tempo(data):
        rows, overflow = expand(root)
        if overflow is not None:
            return jsonify({
                "ok": False,
                "overflow": True,
                "errors": [_measure_overflow_error(overflow)],
            })
        tempo_map, tempo_errors = _parse_tempo(data, len(rows))
        if tempo_errors:
            return jsonify({"ok": False, "errors": tempo_errors})

    beats, overflow = beats_in_window(root, start_ms, end_ms, tempo_map=tempo_map)
    if overflow is not None:
        node = overflow["node"]
        if overflow["kind"] == "measures":
            error = _measure_overflow_error(node)
        else:
            error = {
                "line": node["line"],
                "message": (
                    f"时间窗内拍点超过 {MAX_BEATS} 上限，"
                    f"在第 {node['line']} 行（小节 {node['num']}）处停止"
                ),
            }
        return jsonify({"ok": False, "overflow": True, "errors": [error]})

    return jsonify({"ok": True, "count": len(beats), "beats": beats})


@app.post("/api/segments")
def api_segments():
    data = request.get_json(silent=True) or {}
    text = data.get("input", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({
            "ok": False,
            "errors": [{"line": None, "message": "输入为空，请逐行输入小节编号"}],
        })

    root, errors = parse(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    endpoints = {}
    for label, key in (("起点", "start_num"), ("终点", "end_num")):
        num, err = _parse_measure_number(data, key, label)
        if err is not None:
            return jsonify({"ok": False, "errors": [{"line": None, "message": err}]})
        endpoints[key] = num

    # 范围按源谱输入顺序确定：端点须存在且起点不得晚于终点
    pattern, err = source_range_pattern(
        root, endpoints["start_num"], endpoints["end_num"])
    if err is not None:
        return jsonify({"ok": False, "errors": [{"line": None, "message": err}]})

    # 与展开接口一致：先展开一次取得总小节数以校验临时变速序号范围，
    # 片段时间直接沿用展开行的起止毫秒
    rows, overflow = expand(root)
    if overflow is not None:
        return jsonify({
            "ok": False,
            "overflow": True,
            "errors": [_measure_overflow_error(overflow)],
        })
    tempo_map, tempo_errors = _parse_tempo(data, len(rows))
    if tempo_errors:
        return jsonify({"ok": False, "errors": tempo_errors})
    if tempo_map:
        rows, _ = expand(root, tempo_map=tempo_map)

    segments = match_segments(rows, pattern)
    return jsonify({
        "ok": True,
        "count": len(segments),
        "pattern": pattern,
        "segments": segments,
    })


@app.post("/api/midi")
def api_midi():
    data = request.get_json(silent=True) or {}
    text = data.get("input", "")
    if not isinstance(text, str) or not text.strip():
        return jsonify({
            "ok": False,
            "errors": [{"line": None, "message": "输入为空，请逐行输入小节编号"}],
        })

    root, errors = parse(text)
    if errors:
        return jsonify({"ok": False, "errors": errors})

    # 与展开接口一致：先展开一次取得总小节数以校验临时变速序号范围
    rows, overflow = expand(root)
    if overflow is not None:
        return jsonify({
            "ok": False,
            "overflow": True,
            "errors": [_measure_overflow_error(overflow)],
        })
    tempo_map, tempo_errors = _parse_tempo(data, len(rows))
    if tempo_errors:
        return jsonify({"ok": False, "errors": tempo_errors})

    timeline, overflow = beat_timeline(root, tempo_map=tempo_map)
    if overflow is not None:
        node = overflow["node"]
        if overflow["kind"] == "measures":
            error = _measure_overflow_error(node)
        else:
            error = {
                "line": node["line"],
                "message": (
                    f"全曲拍数超过 {MAX_BEATS} 上限，"
                    f"在第 {node['line']} 行（小节 {node['num']}）处停止"
                ),
            }
        return jsonify({"ok": False, "overflow": True, "errors": [error]})

    content, err = encode_smf(timeline)
    if err is not None:
        return jsonify({"ok": False, "errors": [{"line": None, "message": err}]})

    return send_file(
        BytesIO(content),
        mimetype="audio/midi",
        as_attachment=True,
        download_name="beats.mid",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
