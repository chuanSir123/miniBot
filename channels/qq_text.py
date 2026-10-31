"""QQ 出站文本整理。

分两层，别混在一起
------------------
**第一层：通道特性。** 与谁在说话无关。QQ 客户端不渲染 Markdown，
``**加粗**``、``# 标题`` 发出去就是一堆符号，所以必须剥掉。这层是确定的，
由 ``strip_markdown`` 负责，配置里可以整体关掉。

**第二层：风格偏好。** 强依赖人设与语言。哪些话算候场客套、哪些算进度铺垫、
一条消息留几个颜文字——这些**没有通用答案**，所以代码里一条具体词都不写死，
全部从 ``persona.style`` 以**字面短语列表**读入：

    "style": {
      "courtesy_tail": ["还有什么能帮", "随时找我"],
      "tool_preamble": ["让我查一下", "我看完了"]
    }

匹配方式是「片段里是否包含这条短语」，不是正则。换人设、换语言、换口癖，
只改配置，不动这个文件。列表留空 = 该规则不启用。

为什么是「发送前清理」而不是「再多写一条提示词」
提示词是请求，这里是保证。26B 级模型对否定式指令的遵循并不可靠，而消息
发出后无法撤回。但清理只做**零信息损失**的处理：格式符号、整句套话、
多余的装饰。不截断语义、不改写措辞、不删正文内容。
"""

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# 语言无关的小工具：判断「有多少实质文字」和「哪些括号是颜文字」
# ---------------------------------------------------------------------------

_WORDISH = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7afA-Za-z0-9]")
_PAREN_GROUP = re.compile(r"[\(（]([^()（）\n]{1,16})[\)）]")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\uFE0F\u200D]"
)

# 句子/分句边界。中英文标点都收，换行、波浪号、冒号也算——
# 「晚安啦，明天见～有需要随时叫我」要能切到「有需要随时叫我」这一句，
# 否则砍客套时会连「明天见」一起丢掉。
# 英文句点只在「后面是空白或结尾」时才算边界，避免把 main.py、v1.2 切开。
_CLAUSE_SPLIT = re.compile(r"[。！？!?…；;，,、：:\n～~]+|(?<=\S)\.(?=\s|$)")
# 强句末：只有切在这里才不会把一句话拦腰截断。冒号也算——「我挑了几个：」
# 后面必然换行才是正文，本来就是一个完整的收束。
_STRONG_END = re.compile(r"[。！？!?…\n～~.：:]$")


def _visible_len(text: str) -> int:
    """有多少个「实打实的字」。只用来做安全阀，不参与语义判断。"""
    return len(_WORDISH.findall(text or ""))


def _is_kaomoji(inner: str) -> bool:
    """括号里的内容不含任何文字或数字，才算颜文字。

    ``(2024)``、``（注：）``、``(笑)`` 都不会被当成颜文字误删。
    """
    return bool(inner.strip()) and not _WORDISH.search(inner) and not _EMOJI.search(inner)


# ---------------------------------------------------------------------------
# 第一层：Markdown 剥离（通道特性）
# ---------------------------------------------------------------------------

_EMPHASIS = (
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"), r"\1"),
)
# 独立成行的 **小标题** 其实是模型在写简报，QQ 里没有标题这回事。
# 转成「标题，正文」接回同一段：中文逗号比冒号更像口语。
# 只吃掉紧邻的一个换行，段落之间的空行留着，否则几条小标题会粘成一大坨。
_MD_TITLE_LINE = re.compile(r"(?m)^[ \t]*\*\*([^*\n]{1,40})\*\*[ \t]*(?:\n|[：:][ \t]*)?")
_MD_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
_MD_BULLET = re.compile(r"(?m)^[ \t]{0,3}[-*+][ \t]+")
_MD_QUOTE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]?")
_MD_HR = re.compile(r"(?m)^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
_MD_TABLE_SEP = re.compile(r"(?m)^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$")
_MD_TABLE_ROW = re.compile(r"(?m)^[ \t]*\|(.+)\|[ \t]*$")


def _title_to_clause(match: "re.Match") -> str:
    """把独立成行的小标题改写成口语的句首成分。

    标题跟正文常常是重复的（``**Google 爆料**`` 接着 ``他爆料说 Google…``），
    这种情况直接删掉标题，否则会念成「Google 爆料，他爆料说 Google…」。
    不重复时保留成「军令状，每天发改进。」——比冒号的简报感轻得多。
    """
    title = match.group(1).strip()
    following = re.sub(r"[\s\W_]", "", match.string[match.end(): match.end() + 48])
    key = re.sub(r"[\s\W_]", "", title)[:2]
    if key and key in following:
        return ""
    return title + "，"


def strip_markdown(text: str) -> str:
    """剥掉 QQ 不渲染的 Markdown 标记，保留文字本身。"""
    if not text:
        return text
    out = _MD_HR.sub("", text)
    out = _MD_TABLE_SEP.sub("", out)
    out = _MD_TABLE_ROW.sub(lambda m: "·".join(c.strip() for c in m.group(1).split("|") if c.strip()), out)
    # 标题的 # 必须先去掉，否则它挡在 **标题** 和正文之间，下面那条「小标题并到
    # 正文前」的规则就认不出紧跟着的是哪一行。
    out = _MD_HEADING.sub("", out)
    out = _MD_TITLE_LINE.sub(_title_to_clause, out)
    out = _MD_BULLET.sub("", out)
    out = _MD_QUOTE.sub("", out)
    for pattern, repl in _EMPHASIS:
        out = pattern.sub(repl, out)
    return re.sub(r"\n{3,}", "\n\n", out)


# ---------------------------------------------------------------------------
# 第二层：风格清理（配置驱动，不含任何具体人设的词）
# ---------------------------------------------------------------------------


def _clauses(text: str):
    """按分句符切开，返回 [(原文切片, 起点, 含分隔符的终点)]。

    第三个值是**连同分隔符一起**的终点：判断「是不是切在强句末」要靠它，
    否则拿到的永远是去掉标点前的字符串。
    """
    spans, start = [], 0
    for match in _CLAUSE_SPLIT.finditer(text):
        if match.start() > start:
            spans.append((text[start:match.start()], start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((text[start:], start, len(text)))
    return spans


def _find_phrase(clause: str, phrases: Tuple[str, ...]):
    """在分句里找配置短语，返回最靠前的下标；找不到返回 None。

    大小写不敏感，这样配置写 ``let me know`` 也能命中 ``Let me know``。
    """
    lowered = clause.lower()
    hits = [lowered.find(phrase.lower()) for phrase in phrases if phrase.lower() in lowered]
    return min(hits) if hits else None


def trim_preamble(text: str, phrases: Tuple[str, ...], max_rounds: int = 3) -> str:
    """砍掉开头符合条件的铺垫句。

    只看开头连续几个分句：累积到某个分句里出现配置短语，就把整段一起丢掉。
    这样「主人，查回来啦～ (๑>◡<๑) 我挑了几个：」能一次清干净，
    而「查到了这个方法不行」因为不含任何配置短语，原样保留。

    短语列表为空时直接返回，不做任何事。
    """
    if not text or not phrases:
        return text
    out = text
    for _ in range(max_rounds):
        cut = None
        for clause, _start, end in _clauses(out):
            if _find_phrase(clause, phrases) is not None:
                cut = end
                break
            # 铺垫不会太长，连续几句都没命中说明开头就是正文
            if end > 48:
                return out
        if cut is None:
            return out
        # 只在强句末处下刀。切在逗号上会得到「here you go…」这种半句开头，
        # 那是把一句话拦腰截断，比不切更糟。
        if not _STRONG_END.search(out[:cut].rstrip()):
            return out
        rest = out[cut:].lstrip("，,、。.!！～~：: \t\n")
        # 铺垫后面常跟一个孤零零的颜文字，句子被砍走后它就没着落了
        rest = _PAREN_GROUP.sub(lambda m: "" if _is_kaomoji(m.group(1)) else m.group(0), rest, count=1)
        rest = rest.lstrip("，,、。.!！～~：: \t\n")
        # 剩下的是小写开头（英文里说明这是同一句话的后半截），不动
        if rest[:1].islower():
            return out
        # 砍完没剩多少正文，说明整条就是铺垫，不动
        if _visible_len(rest) < 8 or _visible_len(rest) >= _visible_len(out):
            return out
        out = rest
    return out


def trim_courtesy_tail(
    text: str,
    phrases: Tuple[str, ...],
    max_clause_chars: int = 24,
    max_rounds: int = 2,
) -> str:
    """砍掉结尾的候场客套句。

    只看最后一个分句：它必须短、且包含配置短语，才动手。
    在句内定位到短语的起点再切，所以「明天见～有需要随时叫我」只掉后半截，
    不会把「明天见」一并吞掉。

    判据是「含不含义配置短语」，所以「有什么好吃的推荐吗」只要不在配置里
    就永远不会被误伤——不需要任何问句/动词词表。
    """
    if not text or not phrases:
        return text
    out = text
    for _ in range(max_rounds):
        body = out.rstrip()
        spans = _clauses(body)
        if len(spans) < 2:
            return out
        clause, cstart, _end = spans[-1]
        if len(clause.strip()) > max_clause_chars:
            return out
        offset = _find_phrase(clause, phrases)
        if offset is None:
            return out
        pos = cstart + offset
        # 只回收连接用的逗号、冒号与空白，句末的 。！？ 要留着，
        # 否则砍掉客套后上一句会变成没有句号的半截。
        head = body[:pos].rstrip("，,、；;：: \t\n～~")
        # 短语前面常留一截碎尾（「有什么｜想让我帮忙」「有需要｜随时找我」），
        # 单独留着没有意义，连它一起丢。
        tail = _clauses(head)
        if len(tail) > 1 and _visible_len(tail[-1][0]) < 6:
            head = head[: tail[-1][1]].rstrip("，,、；;：: \t\n～~")
        # 前面没有实质内容（整条就是客套）时保留原样
        if _visible_len(head) < 2:
            return out
        out = head
    return out


def limit_decorations(
    text: str,
    max_kaomoji: Optional[int] = None,
    max_emoji: Optional[int] = None,
) -> str:
    """按配置限制颜文字/emoji 数量。None 表示不限制。"""
    if not text or (max_kaomoji is None and max_emoji is None):
        return text
    out = text
    if max_kaomoji is not None:
        budget = [max_kaomoji]

        def keep_paren(match):
            if not _is_kaomoji(match.group(1)):
                return match.group(0)
            if budget[0] <= 0:
                return ""
            budget[0] -= 1
            return match.group(0)

        out = _PAREN_GROUP.sub(keep_paren, out)
    if max_emoji is not None:
        budget = [max_emoji]

        def keep_emoji(match):
            if budget[0] <= 0:
                return ""
            budget[0] -= 1
            return match.group(0)

        out = _EMOJI.sub(keep_emoji, out)
    # 删掉装饰后可能留下悬空空格或标点，收紧一下
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([，。！？~～、；：])", r"\1", out)
    return out.strip()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextStyle:
    """出站整理的配置快照。所有字段都能在 persona.style 里覆盖。"""

    strip_markdown: bool = True
    max_kaomoji: Optional[int] = 1
    max_emoji: Optional[int] = 1
    courtesy_tail: Tuple[str, ...] = ()
    tool_preamble: Tuple[str, ...] = ()
    courtesy_max_chars: int = 40
    # 每条气泡的目标字数。120 太大——700 字拆 6 条每段还有 116 字，视觉上
    # 仍是一整块；聊天窗口里一条 40~60 字才是正常人的节奏。
    segment_chunk: int = 70
    segment_cap: int = 6
    no_split_below: int = 60

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "TextStyle":
        raw = ((config or {}).get("persona") or {}).get("style") or {}
        if not isinstance(raw, dict):
            raw = {}

        def _tuple(key):
            value = raw.get(key) or ()
            if isinstance(value, str):
                value = (value,)
            return tuple(str(item) for item in value if str(item).strip())

        def _int(key, default):
            try:
                return int(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        def _opt_int(key, default):
            if key in raw and raw[key] is None:
                return None
            return _int(key, default)

        return cls(
            strip_markdown=bool(raw.get("strip_markdown", True)),
            max_kaomoji=_opt_int("max_kaomoji", 1),
            max_emoji=_opt_int("max_emoji", 1),
            courtesy_tail=_tuple("courtesy_tail"),
            tool_preamble=_tuple("tool_preamble"),
            courtesy_max_chars=max(8, _int("courtesy_max_chars", 40)),
            segment_chunk=max(20, _int("segment_chunk", 70)),
            segment_cap=max(1, _int("segment_cap", 6)),
            no_split_below=max(0, _int("no_split_below", 60)),
        )

    def tidy(self, text: str) -> str:
        """通用出站处理。幂等。"""
        if not text or not text.strip():
            return text
        out = strip_markdown(text) if self.strip_markdown else text
        out = trim_courtesy_tail(out, self.courtesy_tail, self.courtesy_max_chars)
        out = limit_decorations(out, self.max_kaomoji, self.max_emoji)
        return out if out.strip() else text

    def tidy_tool_reply(self, text: str) -> str:
        """工具场景：在通用处理之上，再砍掉开头的进度铺垫。"""
        return trim_preamble(self.tidy(text), self.tool_preamble)

    def parts_for(self, text: str) -> int:
        """按句数与长度决定拆几条。

        判据不是「总长度」而是「有没有两个以上的句子」：短短两句
        （「你好。很高兴见到你！」）本来就该分两条气泡发，按长度一刀切会把
        它们缝回一条，反而不像人在打字。长度只用来给长回复加段数。
        """
        body = text or ""
        sentences = len(re.findall(r"[。！？!?…\n～~]+", body))
        if sentences < 2 and len(body) <= self.no_split_below:
            return 1
        by_length = -(-len(body) // self.segment_chunk)
        return min(self.segment_cap, max(2, by_length))


# 没有配置时的兜底：只剥 Markdown，风格清理全部不启用。
DEFAULT_STYLE = TextStyle()
