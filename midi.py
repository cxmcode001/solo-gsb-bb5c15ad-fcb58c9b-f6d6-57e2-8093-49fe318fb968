"""标准 MIDI 文件（SMF）编码：单轨格式 0，按累计拍数定位事件。

约定：
- 每拍对应一个四分音符，480 ticks/拍，事件位置为 拍序号 × 480
  （拍序号由累计拍数确定，不经过毫秒换算或取整）
- 第 10 通道（打击乐通道）：每小节首拍 76 号音符，其余拍 77 号音符，
  力度 100，每个音符持续 120 ticks
- 速度事件写在起点及有效速度变化处，微秒/拍按四舍五入取整，
  且必须可表示为 MIDI 三字节正整数（1~16777215）
- 轨道结束位置为全曲拍数 × 480
"""

# 每拍（四分音符）的 tick 数
TICKS_PER_BEAT = 480

# 每个音符持续的 tick 数
NOTE_TICKS = 120

# 第 10 通道在状态字节中的通道号（MIDI 通道从 0 计数）
CHANNEL = 9

# 首拍与其余拍的音符编号
FIRST_BEAT_NOTE = 76
OTHER_BEAT_NOTE = 77

# 力度
VELOCITY = 100

# 微秒/拍须可表示为 MIDI 三字节正整数
MAX_US_PER_BEAT = 0xFFFFFF


def us_per_beat(bpm):
    """由 BPM 计算微秒/拍（四舍五入）；无法用三字节正整数表示时返回 None。"""
    value = int(60000000 / bpm + 0.5)
    if not 1 <= value <= MAX_US_PER_BEAT:
        return None
    return value


def _varint(value):
    """将非负整数编码为 MIDI 变长数量（VLQ）。"""
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def encode_smf(timeline):
    """将节拍时间轴编码为单轨 SMF 格式 0 文件字节。

    timeline 为 repeats.beat_timeline 的结果：
    {"beats": [{"index": 拍序号, "first": 是否首拍}, ...],
     "tempos": [{"index": 拍序号, "bpm": BPM, "seq": 演奏序号}, ...],
     "total_beats": 全曲拍数}

    返回 (内容, 错误描述)：某个速度值的微秒/拍无法用 MIDI 三字节正整数
    表示时内容为 None，错误描述指出对应的演奏序号与 BPM。
    """
    events = []  # (tick, 同刻优先级, 事件字节)；速度事件排在同刻音符之前
    for tempo in timeline["tempos"]:
        us = us_per_beat(tempo["bpm"])
        if us is None:
            return None, (
                f"第 {tempo['seq']} 个演奏小节起生效的速度 {tempo['bpm']} BPM，"
                f"微秒/拍无法用 MIDI 三字节正整数表示"
                f"（须在 1~{MAX_US_PER_BEAT} 之间），无法导出 MIDI"
            )
        tick = tempo["index"] * TICKS_PER_BEAT
        events.append((tick, 0, b"\xff\x51\x03" + us.to_bytes(3, "big")))
    for beat in timeline["beats"]:
        tick = beat["index"] * TICKS_PER_BEAT
        note = FIRST_BEAT_NOTE if beat["first"] else OTHER_BEAT_NOTE
        events.append((tick, 1, bytes([0x90 | CHANNEL, note, VELOCITY])))
        events.append((tick + NOTE_TICKS, 1, bytes([0x80 | CHANNEL, note, 0])))
    events.sort(key=lambda e: (e[0], e[1]))

    track = bytearray()
    last_tick = 0
    for tick, _prio, data in events:
        track += _varint(tick - last_tick)
        track += data
        last_tick = tick
    # 轨道结束位置为全曲拍数 × 480
    track += _varint(timeline["total_beats"] * TICKS_PER_BEAT - last_tick)
    track += b"\xff\x2f\x00"

    header = (
        b"MThd" + (6).to_bytes(4, "big")
        + (0).to_bytes(2, "big")          # 格式 0
        + (1).to_bytes(2, "big")          # 单轨
        + TICKS_PER_BEAT.to_bytes(2, "big")
    )
    return header + b"MTrk" + len(track).to_bytes(4, "big") + bytes(track), None
