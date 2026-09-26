"""反复段解析与展开核心逻辑：展开、排练时间轴、节拍点查询与排练片段定位。

输入格式（逐行）：
    [|:]小节编号[:|xN][拍数@BPM]
- 编号前的 |:         表示反复起点
- 编号后的 :|xN       表示反复终点，N 为重复次数（2~9）
- 反复段允许嵌套
- 行末可附时间标记：[拍数] 或 [拍数@BPM]；拍数省略时默认为 4，
  未标速度的小节沿用上一演奏小节的速度，初始速度 120 BPM

临时变速方案（可选，逐行）：
    演奏序号=BPM
- 演奏序号按反复展开后的实际轮次计数（从 1 开始），BPM 为正整数
- 演奏到该轮次时临时速度优先于该小节原谱速度，并继续传给后续未标速小节；
  遇到下一处原谱显式速度或下一条临时变速时重新切换
"""

import math
import re

# 展开小节数上限
MAX_MEASURES = 5000

# 拍点数上限（时间窗内拍点、MIDI 导出全曲拍数）
MAX_BEATS = 5000

# 默认拍数与初始速度
DEFAULT_BEATS = 4
INITIAL_BPM = 120

# 行格式：可选的 |: 前缀 + 编号 + 可选的 :|xN 后缀（N 为 2~9）+ 可选的 [拍数@BPM]
LINE_RE = re.compile(
    r"^\s*(?:(\|:)\s*)?(\d+)"
    r"(?:\s*(:\|)\s*[xX]\s*([2-9]))?"
    r"(?:\s*\[([^\]]*)\])?"
    r"\s*$"
)

# 正整数（仅 ASCII 数字）
POSITIVE_INT_RE = re.compile(r"[0-9]+")

FORMAT_HINT = (
    "格式错误：应为 [|:]编号[:|xN][拍数@BPM]，N 为 2~9，"
    "例如 “|:12:|x3[4@120]”"
)


def parse_timing(content):
    """解析时间标记内容（方括号内的文本），返回 (拍数, BPM, 错误描述)。

    内容形如 “拍数” 或 “拍数@BPM”。校验通过时错误描述为 None；
    校验失败时拍数与 BPM 为 None，错误描述指出问题所在。
    """
    parts = content.split("@")
    if len(parts) > 2:
        return None, None, "时间标记格式错误：应为 [拍数] 或 [拍数@BPM]，例如 “[4@120]”"
    beats_s = parts[0].strip()
    if not POSITIVE_INT_RE.fullmatch(beats_s) or int(beats_s) == 0:
        return None, None, f"拍数必须为正整数，得到 “{beats_s}”"
    bpm = None
    if len(parts) == 2:
        bpm_s = parts[1].strip()
        if not POSITIVE_INT_RE.fullmatch(bpm_s) or int(bpm_s) == 0:
            return None, None, f"BPM 必须为正整数，得到 “{bpm_s}”"
        bpm = int(bpm_s)
    return int(beats_s), bpm, None


def parse_tempo_map(text, total=None):
    """解析临时变速方案文本，返回 ({演奏序号: BPM}, errors)。

    每行格式为 “演奏序号=BPM”（空行忽略）：
    - 演奏序号按反复展开后的实际轮次计数，必须为正整数；
      提供 total（展开后的总小节数）时，序号不得超出该范围
    - BPM 必须为正整数
    - 序号重复时报错并指出首次出现的行

    errors 条目为 {"line": 方案内行号, "source": "临时变速", "message": 错误描述}，
    按行号排序。
    """
    entries = {}
    seen = {}  # 演奏序号 -> 首次出现的方案行号（含因超范围被拒的序号）
    errors = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue  # 跳过空行

        parts = line.split("=")
        if len(parts) != 2:
            errors.append({
                "line": lineno,
                "source": "临时变速",
                "message": "格式错误：应为 演奏序号=BPM，例如 “11=90”",
            })
            continue
        seq_s, bpm_s = parts[0].strip(), parts[1].strip()
        if not POSITIVE_INT_RE.fullmatch(seq_s) or int(seq_s) == 0:
            errors.append({
                "line": lineno,
                "source": "临时变速",
                "message": f"演奏序号必须为正整数，得到 “{seq_s}”",
            })
            continue
        if not POSITIVE_INT_RE.fullmatch(bpm_s) or int(bpm_s) == 0:
            errors.append({
                "line": lineno,
                "source": "临时变速",
                "message": f"BPM 必须为正整数，得到 “{bpm_s}”",
            })
            continue

        seq = int(seq_s)
        if seq in seen:
            errors.append({
                "line": lineno,
                "source": "临时变速",
                "message": f"演奏序号 {seq} 重复，首次出现在第 {seen[seq]} 行",
            })
            continue
        seen[seq] = lineno
        if total is not None and seq > total:
            errors.append({
                "line": lineno,
                "source": "临时变速",
                "message": f"演奏序号 {seq} 超出展开范围（共 {total} 小节）",
            })
            continue
        entries[seq] = int(bpm_s)

    return entries, errors


def _round_ms(value):
    """四舍五入到最近毫秒（半值向上）。"""
    return int(math.floor(value + 0.5))


def parse(text):
    """解析输入文本，构建嵌套树并校验。

    返回 (root, errors)：
    - root  为 {"type": "group", "count": 1, "children": [...]} 形式的根节点；
      小节节点为 {"type": "measure", "num": 编号, "line": 行号,
      "beats": 拍数, "bpm": 速度或 None（未标注时沿用上一演奏小节）}。
    - errors 为 [{"line": 行号, "message": 错误描述}, ...]，按行号排序。
    """
    errors = []
    root = {"type": "group", "count": 1, "children": [], "line": None}
    stack = [root]  # 当前嵌套层级的分组栈，栈底为根
    seen = {}  # 小节编号 -> 首次出现的行号

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue  # 跳过空行

        m = LINE_RE.match(raw)
        if not m:
            errors.append({"line": lineno, "message": FORMAT_HINT})
            continue

        start, num_s, _end, count_s, timing_s = m.groups()
        num = int(num_s)

        # 时间标记：[拍数] 或 [拍数@BPM]；校验失败时记录错误并按默认值继续构建结构
        beats, bpm = DEFAULT_BEATS, None
        if timing_s is not None:
            beats, bpm, err = parse_timing(timing_s)
            if err is not None:
                errors.append({"line": lineno, "message": err})
                beats, bpm = DEFAULT_BEATS, None

        # 编号唯一性校验
        if num in seen:
            errors.append({
                "line": lineno,
                "message": f"小节编号 {num} 重复，首次出现在第 {seen[num]} 行",
            })
        else:
            seen[num] = lineno

        # 反复起点：新建分组并压栈，本行小节属于该分组
        if start:
            group = {"type": "group", "count": None, "children": [], "line": lineno}
            stack[-1]["children"].append(group)
            stack.append(group)

        stack[-1]["children"].append(
            {"type": "measure", "num": num, "line": lineno,
             "beats": beats, "bpm": bpm}
        )

        # 反复终点：弹出当前分组并记录重复次数
        if count_s is not None:
            if len(stack) == 1:
                errors.append({
                    "line": lineno,
                    "message": "反复终点 :| 没有匹配的反复起点 |:",
                })
            else:
                group = stack.pop()
                group["count"] = int(count_s)
                group["end_line"] = lineno

    # 输入结束时仍未闭合的反复起点
    while len(stack) > 1:
        group = stack.pop()
        errors.append({
            "line": group["line"],
            "message": "反复起点 |: 没有匹配的反复终点 :|xN",
        })

    errors.sort(key=lambda e: e["line"])
    return root, errors


def _iter_measures(root):
    """按实际演奏顺序迭代产生小节节点（迭代实现，避免深嵌套递归溢出）。"""
    # 帧结构：[子节点列表, 下一个待处理下标, 剩余重复次数]
    stack = [[root["children"], 0, 1]]
    while stack:
        frame = stack[-1]
        children, idx, _ = frame
        if idx >= len(children):
            # 当前分组一轮结束：剩余次数减一后从头再来
            stack.pop()
            frame[2] -= 1
            if frame[2] > 0:
                frame[1] = 0
                stack.append(frame)
            continue
        node = children[idx]
        frame[1] += 1
        if node["type"] == "measure":
            yield node
        else:
            stack.append([node["children"], 0, node["count"]])


def _iter_source_measures(root):
    """按源谱输入顺序迭代产生小节节点（保持源谱结构，不展开反复）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        if node["type"] == "measure":
            yield node
        else:
            stack.extend(reversed(node["children"]))


def _iter_playback(root, tempo_map=None):
    """按实际演奏顺序产生 (小节节点, 演奏序号, 当前 BPM)。

    展开与拍点查询共用同一速度轨迹，切换规则：
    - 初始速度 120 BPM
    - 演奏到临时变速指定的轮次时，临时速度优先于该小节原谱速度
    - 否则遇到带原谱速度标记的小节时切换为原谱速度
    - 未标速度的小节沿用上一演奏小节的速度
    """
    bpm = INITIAL_BPM  # 当前速度，初始 120 BPM
    seq = 0  # 演奏序号
    for node in _iter_measures(root):
        seq += 1
        if tempo_map is not None and seq in tempo_map:
            bpm = tempo_map[seq]  # 临时变速优先于原谱速度
        elif node["bpm"] is not None:
            bpm = node["bpm"]
        yield node, seq, bpm


def expand(root, limit=MAX_MEASURES, tempo_map=None):
    """按嵌套层级展开为实际演奏顺序。

    tempo_map 为可选的临时变速方案 {演奏序号: BPM}，速度轨迹见 _iter_playback。

    返回 (rows, overflow)：
    - rows     为 [{"seq": 演奏序号, "num": 原小节编号, "line": 源行号,
                     "start_ms": 起始毫秒, "end_ms": 结束毫秒}, ...]；
      时长按 拍数×60/BPM 秒累计，累计时保留精度，起止毫秒四舍五入到最近毫秒
    - overflow 为导致超过 limit 的小节节点；未超限时为 None
    """
    rows = []
    elapsed = 0.0  # 已累计的演奏时长（毫秒，保留精度）
    for node, seq, bpm in _iter_playback(root, tempo_map):
        duration = node["beats"] * 60000 / bpm
        rows.append({
            "seq": seq,
            "num": node["num"],
            "line": node["line"],
            "start_ms": _round_ms(elapsed),
            "end_ms": _round_ms(elapsed + duration),
        })
        elapsed += duration
        if len(rows) > limit:
            return rows, node
    return rows, None


def source_range_pattern(root, start_num, end_num):
    """按源谱输入顺序确定起止小节编号对应的编号序列（片段定位的匹配模式）。

    返回 (pattern, 错误描述)；通过时错误描述为 None。
    两个端点都必须存在于源谱，且起点在源谱输入顺序中的位置不得晚于终点。
    """
    nums = [node["num"] for node in _iter_source_measures(root)]
    if start_num not in nums:
        return None, f"起点小节编号 {start_num} 在源谱中不存在"
    if end_num not in nums:
        return None, f"终点小节编号 {end_num} 在源谱中不存在"
    start_pos = nums.index(start_num)
    end_pos = nums.index(end_num)
    if start_pos > end_pos:
        return None, (
            f"起点小节编号 {start_num} 在源谱输入顺序中"
            f"晚于终点小节编号 {end_num}"
        )
    return nums[start_pos:end_pos + 1], None


def match_segments(rows, pattern):
    """在展开结果中查找连续、完整奏出 pattern 的片段（排练片段定位）。

    一次匹配必须逐小节按 pattern 顺序连续出现：反复回跳或内层反复
    插入的额外小节会中断匹配，不能跨越拼接成片段。pattern 中编号互不相同
    （源谱编号唯一），失配时只需判断当前行能否从头开始新匹配。

    rows 为 expand 的展开行（含 seq、num、start_ms、end_ms），
    片段的起止毫秒直接沿用这些行，时长为两者之差，
    因此速度与毫秒规则（含临时变速）与展开接口完全一致。

    返回 [{"start_seq": 起始演奏序号, "end_seq": 结束演奏序号,
           "start_ms": 起始毫秒, "end_ms": 结束毫秒,
           "duration_ms": 时长毫秒}, ...]
    """
    segments = []
    progress = 0  # 当前已连续匹配的小节数
    first = None  # 当前匹配的起始展开行
    for row in rows:
        if row["num"] == pattern[progress]:
            if progress == 0:
                first = row
            progress += 1
            if progress == len(pattern):
                segments.append({
                    "start_seq": first["seq"],
                    "end_seq": row["seq"],
                    "start_ms": first["start_ms"],
                    "end_ms": row["end_ms"],
                    "duration_ms": row["end_ms"] - first["start_ms"],
                })
                progress = 0
                first = None
        elif row["num"] == pattern[0]:
            # 失配但当前行可从头开始新匹配（编号唯一，无需更长的回退）
            first = row
            progress = 1
        else:
            progress = 0
            first = None
    return segments


def beat_timeline(root, tempo_map=None, measure_limit=MAX_MEASURES, beat_limit=MAX_BEATS):
    """按累计拍数生成全曲节拍与速度事件（供 MIDI 导出）。

    速度轨迹与展开、拍点查询一致（见 _iter_playback）：临时变速优先于原谱速度。
    事件位置完全由累计拍数确定（拍序号从 0 开始），不经过毫秒换算或取整。

    返回 (timeline, overflow)：
    - timeline 为 {"beats": [{"index": 拍序号, "first": 是否首拍}, ...],
                   "tempos": [{"index": 拍序号, "bpm": BPM, "seq": 演奏序号}, ...],
                   "total_beats": 全曲拍数}；
      tempos 含起点及每次有效速度变化（BPM 与上一演奏小节不同的首个拍）
    - overflow 为 None，或 {"kind": "measures" | "beats",
                            "node": 导致停止的小节节点}
    """
    beats = []
    tempos = []
    index = 0  # 累计拍序号
    prev_bpm = None
    for node, seq, bpm in _iter_playback(root, tempo_map):
        if seq > measure_limit:
            return None, {"kind": "measures", "node": node}
        if bpm != prev_bpm:
            tempos.append({"index": index, "bpm": bpm, "seq": seq})
            prev_bpm = bpm
        for k in range(node["beats"]):
            beats.append({"index": index, "first": k == 0})
            index += 1
            if len(beats) > beat_limit:
                return None, {"kind": "beats", "node": node}
    return {"beats": beats, "tempos": tempos, "total_beats": index}, None


def beats_in_window(root, start_ms, end_ms,
                    measure_limit=MAX_MEASURES, beat_limit=MAX_BEATS,
                    tempo_map=None):
    """列出时间窗 [start_ms, end_ms) 内的拍点（左闭右开）。

    tempo_map 为可选的临时变速方案 {演奏序号: BPM}，与展开共用同一速度轨迹。
    落窗判断使用未取整的累计时间，展示毫秒按 _round_ms 规则取整；
    整小节在窗外时按小节跨度直接跳过，窗内拍点超过 beat_limit 时停止，
    以免大量反复或长小节耗尽服务资源。

    返回 (beats, overflow)：
    - beats    为 [{"time_ms": 展示毫秒, "seq": 演奏序号, "num": 原小节编号,
                     "beat": 拍序, "first": 是否首拍}, ...]
    - overflow 为 None，或 {"kind": "measures" | "beats",
                            "node": 导致停止的小节节点}
    """
    beats = []
    elapsed = 0.0  # 已累计的演奏时长（毫秒，保留精度）
    for node, seq, bpm in _iter_playback(root, tempo_map):
        beat_len = 60000 / bpm  # 每拍时长（毫秒）
        duration = node["beats"] * beat_len
        if elapsed >= end_ms:
            break  # 时间轴单调前进，之后的拍点都在窗外
        if seq > measure_limit:
            return beats, {"kind": "measures", "node": node}
        if elapsed + duration > start_ms:
            # 小节跨度与时间窗相交：按等差定位首个可能落窗的拍，
            # 避免逐拍扫过窗口之前的长小节
            first_k = max(0, math.ceil((start_ms - elapsed) / beat_len) - 1)
            for k in range(first_k, node["beats"]):
                t = elapsed + k * beat_len
                if t >= end_ms:
                    break
                if t >= start_ms:
                    beats.append({
                        "time_ms": _round_ms(t),
                        "seq": seq,
                        "num": node["num"],
                        "beat": k + 1,
                        "first": k == 0,
                    })
                    if len(beats) > beat_limit:
                        return beats, {"kind": "beats", "node": node}
        # 整小节在窗口之前时按小节跨度跳过，不逐拍展开
        elapsed += duration
    return beats, None
