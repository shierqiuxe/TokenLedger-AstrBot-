# -*- coding: utf-8 -*-
"""TokenLedger —— AstrBot 插件

每聊一句，模型都会吐出一串数字：这次吃进去多少 token、吐出来多少、
有多少是缓存里捡的便宜。这些数字平时一闪就没了，钱却实打实地在走。

这个插件干的事就是记账：
    每笔对话 → 一行流水（谁、在哪个群、哪个模型、入多少、出多少、缓存多少）
    流水按天切文件 → 今天、昨天、这周、上周、这个月、上个月，随手就能翻
    面板「扩展功能」里点开 → 柱状图、模型排行、群排行、逐条流水

指令
----
    /用量                今天和这周花了多少
    /用量 昨天           只看昨天
    /用量 周 / 月 / 全部  换个口径
    /用量 模型           哪个模型最费
    /用量 群             哪个群最费
    /用量 清空 [范围]    清账，范围：今天/本周/本月/全部（管理员）
    /用量 帮助           完整说明

它是怎么知道用量的
------------------
AstrBot 每次模型回复完会发一个 OnLLMResponse 事件，回复对象上带着 usage
（TokenUsage：input_other 正常输入、input_cached 缓存命中、output 输出）。
插件挂在这个事件上，只读这几个数，不碰消息内容，也不改回复。

几个坑，写在这儿免得以后忘：
  * 流式回复时这个事件会来好几趟，只有最后一趟带 usage —— 所以没有用量的
    那几趟直接跳过，天然不会重复记账；
  * 万一某个模型接口不回报用量（有些第三方中转就这样），可以按字数粗估，
    估出来的会在账本上标「估算」，不冒充准数；
  * 记账永远不应该弄坏聊天。所有异常都吞掉，只写日志。
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# AstrBot 接口（没装 AstrBot 时退化成空实现，方便离线自测）
# ---------------------------------------------------------------------------
try:
    from astrbot.api import logger  # type: ignore
    from astrbot.api.event import AstrMessageEvent, filter  # type: ignore
    from astrbot.api.star import Context, Star  # type: ignore

    _IN_ASTRBOT = True
except ImportError:  # pragma: no cover - 只有离线跑测试时才会走到
    import logging

    logger = logging.getLogger("token_meter")

    class AstrMessageEvent:  # type: ignore
        pass

    class _NoopFilter:
        """离线时每个 filter.xxx 都返回「原样不收」的装饰器。"""

        def __getattr__(self, attr):
            def _deco(*args, **kwargs):
                def _wrap(fn):
                    return fn

                return _wrap

            return _deco

    filter = _NoopFilter()  # type: ignore

    class Context:  # type: ignore
        pass

    class Star:  # type: ignore
        def __init__(self, context=None):
            self.context = context

        async def terminate(self):
            pass


插件名 = "astrbot_plugin_token_meter"
PLUGIN_DIR = Path(__file__).resolve().parent
视图名 = "用量"


# ---------------------------------------------------------------------------
# 装饰器兼容层
# ---------------------------------------------------------------------------
def _deco(属性名: str, *参数: Any, **关键字: Any):
    """安全地取 filter.<属性名> 装饰器；取不到就退化成不注册。

    宁可这个入口不生效，也不能让整个插件 import 失败 —— 那会连记账一起坏掉。
    """

    def _原样(fn):
        return fn

    装饰器工厂 = getattr(filter, 属性名, None)
    if 装饰器工厂 is None:
        return _原样
    try:
        return 装饰器工厂(*参数, **关键字)
    except Exception:
        try:
            return 装饰器工厂(*参数)
        except Exception:
            return _原样


# ---------------------------------------------------------------------------
# 时间小工具
# ---------------------------------------------------------------------------
def _现在() -> float:
    return time.time()


# None = 跟系统本地时区走；配置里填了时区就换成那个
_时区: Optional[Any] = None


def _设时区(值: Any) -> str:
    """把配置里的时区写法变成 timezone，返回给人看的名字。

    认这几种：
        "local" / 空          跟服务器系统时区
        "UTC"                 零时区
        "UTC+8" / "+8"        东八区（北京时间）；负数同理
        "Asia/Shanghai"       北京时间，等价 UTC+8

    看不懂就退回系统本地。配置写错不该让账本算不明白，更不该抛给用户。
    """
    global _时区
    文 = str(值 or "").strip()
    小 = 文.lower().replace(" ", "")
    if not 小 or 小 in ("local", "system", "默认", "本地", "系统本地"):
        _时区 = None
        return "系统本地"
    if 小 in ("asia/shanghai", "beijing", "北京", "北京时间", "prc", "cst"):
        _时区 = timezone(timedelta(hours=8))
        return "UTC+8"
    if 小 in ("utc", "gmt", "z", "零时区"):
        _时区 = timezone.utc
        return "UTC"
    数字 = 小
    for 头 in ("utc", "gmt"):
        if 数字.startswith(头):
            数字 = 数字[len(头):]
            break
    if 数字.startswith("+"):
        数字 = 数字[1:]
    try:
        点 = float(数字)
        if -12 <= 点 <= 14:
            _时区 = timezone(timedelta(hours=点))
            return "UTC%+g" % 点
    except Exception:
        pass
    _时区 = None
    return "系统本地"


def _本地(ts: float) -> datetime:
    """时间戳 → 配置时区的 datetime。

    账本的「今天」「本周」和界面上那一列时间都走它。服务器跑在 UTC 上、
    而人在东八区时，这一个函数就是「日期对不上」和「对上了」的分界线。
    """
    if _时区 is None:
        return datetime.fromtimestamp(ts)
    return datetime.fromtimestamp(ts, tz=_时区)


def _日期串(ts: float) -> str:
    """配置时区的 2026-11-05。按天切文件就靠它。"""
    return _本地(ts).strftime("%Y-%m-%d")


def _日零点(日: str) -> float:
    """把文件名里的 2026-11-05 还原成那个时区的零点时间戳。"""
    点 = datetime.strptime(日, "%Y-%m-%d")
    if _时区 is not None:
        点 = 点.replace(tzinfo=_时区)
    return 点.timestamp()


def _今天零点(ts: Optional[float] = None) -> float:
    点 = _本地(ts or _现在()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return 点.timestamp()


def _本周一零点(ts: Optional[float] = None) -> float:
    """周一算一周的头。datetime.weekday() 里周一是 0。"""
    点 = _本地(ts or _现在()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (点 - timedelta(days=点.weekday())).timestamp()


def _本月一号零点(ts: Optional[float] = None) -> float:
    点 = _本地(ts or _现在()).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return 点.timestamp()


def _说时间(ts: float) -> str:
    return _本地(ts).strftime("%m-%d %H:%M:%S")


def _说数(n: float) -> str:
    """把 12345 说成 1.23万。中文里论量都用万，不兴 k 和 M。"""
    n = int(n or 0)
    号 = "-" if n < 0 else ""
    n = abs(n)
    if n < 10000:
        # 一万以下就老实把数字写出来：2400 比 2.4千 好读，四位以内正好装得下
        return 号 + str(n)
    if n < 100000000:
        万 = n / 10000.0
        if 万 >= 100:
            return 号 + "%.0f万" % 万
        return 号 + ("%.2f" % 万).rstrip("0").rstrip(".") + "万"
    return 号 + ("%.2f" % (n / 100000000.0)).rstrip("0").rstrip(".") + "亿"


# ---------------------------------------------------------------------------
# 空账本 / 记账
# ---------------------------------------------------------------------------
def 空账() -> Dict[str, int]:
    """io = 正常输入，ic = 缓存命中，o = 输出，n = 次数。"""
    return {"io": 0, "ic": 0, "o": 0, "n": 0}


def 入账(账: Dict[str, int], io: int, ic: int, o: int) -> None:
    账["io"] += int(io or 0)
    账["ic"] += int(ic or 0)
    账["o"] += int(o or 0)
    账["n"] += 1


def 合账(账: Dict[str, int]) -> Dict[str, int]:
    """算合计，顺便去掉内部键，给前端用。"""
    总 = 账["io"] + 账["ic"] + 账["o"]
    return {"io": 账["io"], "ic": 账["ic"], "o": 账["o"], "n": 账["n"], "total": 总}


# ---------------------------------------------------------------------------
# 从 AstrBot 的对象里抠信息（各版本字段名不太一样，挨个试）
# ---------------------------------------------------------------------------
def _配(对象: Any, 名: str, 默认: Any = None) -> Any:
    if 对象 is None:
        return 默认
    if isinstance(对象, dict):
        return 对象.get(名, 默认)
    return getattr(对象, 名, 默认)


def _读数(对象: Any, 名字们: Tuple[str, ...]) -> Optional[int]:
    """从对象或字典里按顺序找第一个像整数的字段。"""
    if 对象 is None:
        return None
    for 名 in 名字们:
        v = _配(对象, 名, None)
        if v is None or isinstance(v, bool):
            continue
        if callable(v):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        try:
            return int(str(v))
        except Exception:
            continue
    return None


def _取会话号(event: Any) -> str:
    """一个稳定的会话标识（群号 / 私聊对象 / 统一来源）。"""
    for 名 in ("unified_msg_origin", "session_id", "group_id"):
        v = getattr(event, 名, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for 名 in ("get_session_id", "get_group_id"):
        fn = getattr(event, 名, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    return str(v)
            except Exception:
                pass
    return "default"


def _取昵称(event: Any) -> str:
    for 名 in ("sender_name", "nickname"):
        v = getattr(event, 名, None)
        if isinstance(v, str) and v.strip():
            return v.strip()[:16]
    for 名 in ("get_sender_name", "get_sender_id"):
        fn = getattr(event, 名, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    return str(v)[:16]
            except Exception:
                pass
    obj = getattr(event, "message_obj", None)
    发信人 = getattr(obj, "sender", None)
    if 发信人 is not None:
        for 名 in ("nickname", "name", "card"):
            v = getattr(发信人, 名, None)
            if isinstance(v, str) and v.strip():
                return v.strip()[:16]
    return ""


def _取用户号(event: Any) -> str:
    for 名 in ("get_sender_id", "sender_id"):
        fn = getattr(event, 名, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    return str(v)[:32]
            except Exception:
                pass
        elif isinstance(fn, str) and fn.strip():
            return fn.strip()[:32]
    return ""


def _是主动(event: Any) -> Optional[int]:
    """这条对话是谁先开口的？1=机器人主动找人，0=用户先来找它，None=看不出来。

    判据从宽：拿不准就返回 None，那格显示「—」。宁可少标，不许瞎标。
    """
    if event is None:
        return None
    文本 = str(_配(event, "message_str", "") or "").strip()
    对象 = _配(event, "message_obj", None)
    if 对象 is None:
        # 连消息对象都没有：不是谁发过来的，是凭空起的头
        return 1 if not 文本 else None
    链 = _配(对象, "message", None)
    if not 文本 and not 链:
        # 消息对象在，但里面一句话都没有 —— 没人开口，它自己说的
        return 1
    发信人 = _配(对象, "sender", None)
    自己 = str(_配(对象, "self_id", "") or "")
    发者 = str(_配(发信人, "user_id", "") or "") if 发信人 is not None else ""
    if 自己 and 发者 and 自己 == 发者:
        # 发送者就是机器人自己，那是它往外发
        return 1
    return 0


def _取场景(event: Any) -> Optional[str]:
    """这条消息是群里来的还是私聊？返回「群」/「私」，看不出来给 None。

    各平台写法不一样：有的在 unified_msg_origin 里带 GroupMessage /
    FriendMessage，有的只给一个 is_private_chat()，有的干脆只给 group_id。
    挨个试，试不出就认输 —— 界面上显示「—」，不乱猜。
    """
    if event is None:
        return None
    来源 = str(_配(event, "unified_msg_origin", "") or "").lower()
    if "group" in 来源 or "guild" in 来源 or "channel" in 来源:
        return "群"
    if any(词 in 来源 for 词 in ("friend", "private", "c2c", "direct")):
        return "私"
    开关 = getattr(event, "is_private_chat", None)
    if callable(开关):
        try:
            return "私" if 开关() else "群"
        except Exception:
            pass
    群号 = ""
    取群 = getattr(event, "get_group_id", None)
    if callable(取群):
        try:
            群号 = str(取群() or "")
        except Exception:
            群号 = ""
    if not 群号:
        群号 = str(_配(event, "group_id", "") or "")
    if 群号:
        return "群"
    类型 = str(_配(_配(event, "message_obj", None), "type", "") or "").lower()
    if "group" in 类型:
        return "群"
    if any(词 in 类型 for 词 in ("friend", "private", "direct")):
        return "私"
    return None


def _取模型名(response: Any) -> str:
    """模型名藏在 raw_completion.model 里，各版本可能没有。

    顺带说明：LLMResponse 自己身上没有 model 字段，只有 raw_completion
    （OpenAI 的 ChatCompletion 之类）才带。ProviderRequest 则是直接有
    model 的 —— 所以这个函数两边的对象都接得住。
    """
    # 对象自己带 model 的（ProviderRequest 就是），先看一眼
    v = _配(response, "model", None)
    if isinstance(v, str) and v.strip():
        return v.strip()[:48]

    候选 = [
        getattr(response, "raw_completion", None),
        getattr(response, "completion", None),
    ]
    for 对象 in 候选:
        if 对象 is None:
            continue
        v = _配(对象, "model", None)
        if isinstance(v, str) and v.strip():
            return v.strip()[:48]
        if isinstance(对象, dict):
            v = 对象.get("model")
            if isinstance(v, str) and v.strip():
                return v.strip()[:48]
    return ""


def _取用量(response: Any) -> Optional[Dict[str, int]]:
    """把 response 上的 usage 抠成 {io, ic, o}。

    新版 TokenUsage 有三个真字段：input_other / input_cached / output，
    另外还有 input、total 两个 property。老版本或别家实现可能叫
    prompt_tokens / completion_tokens / total_tokens，这里全都试一遍。
    """
    候选: List[Any] = [getattr(response, "usage", None)]
    raw = getattr(response, "raw_completion", None)
    if raw is not None:
        候选.append(getattr(raw, "usage", None))
        if isinstance(raw, dict):
            候选.append(raw.get("usage"))

    for 用量 in 候选:
        if 用量 is None:
            continue
        正常入 = _读数(用量, ("input_other", "prompt_tokens", "input_tokens", "prompt"))
        缓存 = _读数(
            用量,
            (
                "input_cached",
                "cached_tokens",
                "cached_input_tokens",
                "prompt_cache_hit_tokens",
                "cache_read_input_tokens",
            ),
        )
        输出 = _读数(用量, ("output", "completion_tokens", "output_tokens", "completion"))
        总 = _读数(用量, ("total", "total_tokens"))
        含缓存入 = _读数(用量, ("input", "prompt_total_tokens"))

        # 一个都没读到 → 这份 usage 是空的，换下一个
        if 正常入 is None and 缓存 is None and 输出 is None and 总 is None:
            continue

        if 正常入 is None:
            底 = 含缓存入 if 含缓存入 is not None else 总
            if 底 is not None:
                正常入 = max(0, 底 - (缓存 or 0))
            else:
                正常入 = 0
        if 输出 is None:
            输出 = max(0, (总 or 0) - 正常入 - (缓存 or 0))
        缓存 = 缓存 or 0

        # 全零要照实返回，不能当「没数据」——
        # 流式回复前缀那几趟带的正是全零 usage，得让上层认出它是全零并跳过，
        # 否则会被当成「没报用量」而按字数估出一笔假账。
        return {"io": 正常入, "ic": 缓存, "o": 输出}
    return None


def _粗估(event: Any, response: Any) -> Dict[str, int]:
    """拿不到真用量时按字数估一个。

    中文大约 1 字 = 0.7 token，英文一个词大约 1.3 token。
    混着来就按 0.75 一刀切 —— 肯定不准，所以账本上会标「估算」。
    """
    入文本 = ""
    for 名 in ("message_str", "message"):
        v = getattr(event, 名, None)
        if isinstance(v, str) and v.strip():
            入文本 = v
            break

    出文本 = ""
    v = getattr(response, "completion_text", None)
    if isinstance(v, str):
        出文本 = v
    if not 出文本:
        链 = getattr(response, "result_chain", None)
        段们 = getattr(链, "chain", None) or 链
        if isinstance(段们, (list, tuple)):
            碎片 = []
            for 段 in 段们:
                t = getattr(段, "text", None)
                if isinstance(t, str):
                    碎片.append(t)
            出文本 = "".join(碎片)

    入 = max(1, int(len(入文本) * 0.75)) if 入文本 else 0
    出 = max(1, int(len(出文本) * 0.75)) if 出文本 else 0
    if 入 == 0 and 出 == 0:
        return {"io": 0, "ic": 0, "o": 0}
    return {"io": 入, "ic": 0, "o": 出}


# ---------------------------------------------------------------------------
# 账本：存 + 读 + 汇总
# ---------------------------------------------------------------------------
class 账本:
    """按天一个 jsonl 文件，一行一笔账。

    为什么不用数据库：账本只是流水，读写都是顺序的，几万行也就几 MB；
    一个文件一天，删旧账直接 unlink，不用 VACUUM 也不怕锁表。
    真要涨到百万行再换 SQLite，那时候改的也只是这个类。
    """

    def __init__(self, 根: Path, 留存天数: int = 90):
        self.根 = 根
        self.留存天数 = max(1, int(留存天数 or 90))
        self.锁 = threading.RLock()
        self.记录: List[Dict[str, Any]] = []
        self._改过 = 0.0
        try:
            self.根.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("[TokenLedger] 数据目录建不出来：%s", e)

    # ---------- 文件 ----------
    def _天文件(self, 日: str) -> Path:
        return self.根 / ("%s.jsonl" % 日)

    def 载入(self) -> int:
        """把留存期内的流水读进内存。返回读了多少条。"""
        条数 = 0
        今天 = _今天零点()
        最早 = 今天 - timedelta(days=self.留存天数).total_seconds()
        with self.锁:
            self.记录 = []
            try:
                文件们 = sorted(self.根.glob("*.jsonl"))
            except Exception as e:
                logger.warning("[TokenLedger] 列不出账本文件：%s", e)
                return 0
            for 文件 in 文件们:
                try:
                    日 = 文件.stem
                    零点 = _日零点(日)
                except Exception:
                    continue
                if 零点 < 最早:
                    try:
                        文件.unlink()  # 太老了，顺手清掉
                    except Exception:
                        pass
                    continue
                try:
                    文本 = 文件.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning("[TokenLedger] 读不了 %s：%s", 文件.name, e)
                    continue
                for 行 in 文本.splitlines():
                    行 = 行.strip()
                    if not 行:
                        continue
                    try:
                        笔 = json.loads(行)
                    except Exception:
                        continue  # 半行坏数据（断电写坏的）跳过就行
                    if isinstance(笔, dict) and 笔.get("t"):
                        self.记录.append(笔)
                        条数 += 1
            self.记录.sort(key=lambda x: x.get("t") or 0)
            self._改过 = _现在()
        return 条数

    def 追加(self, 笔: Dict[str, Any]) -> None:
        """记一笔。写文件失败不影响内存里的账，也不抛给调用方。"""
        with self.锁:
            self.记录.append(笔)
            self._改过 = _现在()
            try:
                行 = json.dumps(笔, ensure_ascii=False, separators=(",", ":"))
                with open(self._天文件(笔["d"]), "a", encoding="utf-8", newline="\n") as 手:
                    手.write(行 + "\n")
            except Exception as e:
                logger.warning("[TokenLedger] 这笔没写进文件：%s", e)

    def 清空(self) -> int:
        with self.锁:
            条数 = len(self.记录)
            self.记录 = []
            self._改过 = _现在()
            try:
                for 文件 in self.根.glob("*.jsonl"):
                    文件.unlink()
            except Exception as e:
                logger.warning("[TokenLedger] 清账时没删干净：%s", e)
            return 条数

    def 清理(self, 范围: str = "all") -> Dict[str, Any]:
        """按范围删账。范围：today / week / month / all。

        一天一个文件，所以「删一段」就是删那几个文件，不用逐行重写。
        内存里也同步剔掉，下一次读到的就是删干净的样子。
        """
        范围 = str(范围 or "all").strip().lower()
        现在 = _现在()
        if 范围 in ("all", "全部", "所有"):
            起, 名 = 0.0, "全部"
        elif 范围 in ("today", "今天", "今日"):
            起, 名 = _今天零点(现在), "今天"
        elif 范围 in ("week", "本周", "这周"):
            起, 名 = _本周一零点(现在), "本周"
        elif 范围 in ("month", "本月", "这个月"):
            起, 名 = _本月一号零点(现在), "本月"
        else:
            raise ValueError("范围只能是 today / week / month / all")

        with self.锁:
            原条数 = len(self.记录)
            self.记录 = [笔 for 笔 in self.记录 if (笔.get("t") or 0) < 起]
            删笔 = 原条数 - len(self.记录)
            self._改过 = 现在
            删文件 = 0
            try:
                for 文件 in self.根.glob("*.jsonl"):
                    try:
                        日 = 文件.stem
                        零点 = _日零点(日)
                    except Exception:
                        continue
                    # 文件整天都落在起点之前 → 不在这次清理范围里
                    if 零点 + 86400 <= 起:
                        continue
                    文件.unlink()
                    删文件 += 1
            except Exception as e:
                logger.warning("[TokenLedger] 清账时没删干净：%s", e)
            return {"范围": 名, "笔": 删笔, "文件": 删文件}

    def 快照(self) -> List[Dict[str, Any]]:
        with self.锁:
            return list(self.记录)

    # ---------- 统计 ----------
    def 区间(self, 起: float = 0, 止: float = 0) -> List[Dict[str, Any]]:
        止 = 止 or (_现在() + 86400)
        return [笔 for 笔 in self.快照() if 起 <= (笔.get("t") or 0) < 止]

    def 汇总(self, 笔们: List[Dict[str, Any]]) -> Dict[str, int]:
        账 = 空账()
        for 笔 in 笔们:
            账["io"] += int(笔.get("io") or 0)
            账["ic"] += int(笔.get("ic") or 0)
            账["o"] += int(笔.get("o") or 0)
            账["n"] += 1
        return 合账(账)

    def 按天(self, 天数: int = 14) -> List[Dict[str, Any]]:
        """最近 N 天，一天一条，没账的那天补 0（图上才不会断）。"""
        今天 = _今天零点()
        桶: Dict[str, Dict[str, int]] = {}
        起 = 今天 - timedelta(days=天数 - 1).total_seconds()
        for 笔 in self.区间(起):
            日 = 笔.get("d") or _日期串(笔.get("t") or 0)
            账 = 桶.setdefault(日, 空账())
            账["io"] += int(笔.get("io") or 0)
            账["ic"] += int(笔.get("ic") or 0)
            账["o"] += int(笔.get("o") or 0)
            账["n"] += 1
        结果 = []
        for i in range(天数):
            点 = _本地(起) + timedelta(days=i)
            日 = 点.strftime("%Y-%m-%d")
            账 = 桶.get(日) or 空账()
            结果.append(
                {
                    "d": 日,
                    "标签": 点.strftime("%m-%d"),
                    "周几": "一二三四五六日"[点.weekday()],
                    "io": 账["io"],
                    "ic": 账["ic"],
                    "o": 账["o"],
                    "n": 账["n"],
                    "total": 账["io"] + 账["ic"] + 账["o"],
                }
            )
        return 结果

    def 按周(self, 周数: int = 8) -> List[Dict[str, Any]]:
        本周一 = _本周一零点()
        起 = 本周一 - timedelta(weeks=周数 - 1).total_seconds()
        桶: Dict[str, Dict[str, int]] = {}
        for 笔 in self.区间(起):
            点 = _本地(笔.get("t") or 0)
            周一 = (点 - timedelta(days=点.weekday())).strftime("%Y-%m-%d")
            账 = 桶.setdefault(周一, 空账())
            账["io"] += int(笔.get("io") or 0)
            账["ic"] += int(笔.get("ic") or 0)
            账["o"] += int(笔.get("o") or 0)
            账["n"] += 1
        结果 = []
        for i in range(周数):
            点 = _本地(起) + timedelta(weeks=i)
            键 = 点.strftime("%Y-%m-%d")
            账 = 桶.get(键) or 空账()
            结果.append(
                {
                    "d": 键,
                    "标签": 点.strftime("%m-%d") + " 周",
                    "io": 账["io"],
                    "ic": 账["ic"],
                    "o": 账["o"],
                    "n": 账["n"],
                    "total": 账["io"] + 账["ic"] + 账["o"],
                }
            )
        return 结果

    def 分组(self, 笔们: List[Dict[str, Any]], 键: str, 上限: int = 10) -> List[Dict[str, Any]]:
        """按某个字段分堆排名。键为空的一律归到「未知」。"""
        堆: Dict[str, Dict[str, int]] = {}
        for 笔 in 笔们:
            名 = str(笔.get(键) or "").strip() or "未知"
            账 = 堆.setdefault(名, 空账())
            账["io"] += int(笔.get("io") or 0)
            账["ic"] += int(笔.get("ic") or 0)
            账["o"] += int(笔.get("o") or 0)
            账["n"] += 1
        排 = sorted(
            堆.items(),
            key=lambda kv: kv[1]["io"] + kv[1]["ic"] + kv[1]["o"],
            reverse=True,
        )
        结果 = []
        for 名, 账 in 排[:上限]:
            结果.append(
                {
                    "名": 名,
                    "io": 账["io"],
                    "ic": 账["ic"],
                    "o": 账["o"],
                    "n": 账["n"],
                    "total": 账["io"] + 账["ic"] + 账["o"],
                }
            )
        return 结果


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------
class TokenLedger插件(Star):
    def __init__(self, context: Context = None, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self._接口状态: Dict[str, str] = {}
        self._钩子挂上了 = False
        self._见过: Dict[str, float] = {}  # 用来挡住同一次回复被记两遍
        self._模型表: Dict[str, Tuple[str, float]] = {}  # 会话 → (模型名, 时间)
        self._锁 = threading.Lock()
        self._缓存: Tuple[float, Dict[str, Any]] = (0.0, {})

        # 时区必须定在「读账本」之前 —— 「今天」的日界、按天切文件全看它
        self.时区名 = _设时区(self._配置("time_zone", "UTC+8"))
        logger.info("[TokenLedger] 时区：%s", self.时区名)

        self.账 = 账本(self._数据目录(), self._整数("keep_days", 90))
        条数 = self.账.载入()
        logger.info("[TokenLedger] 载入了 %d 笔旧账", 条数)

        try:
            self._挂网页接口()
        except Exception as e:
            logger.info("[TokenLedger] 网页接口没挂上：%s", e)

    # ---------------- 配置 ----------------
    def _配置(self, 键: str, 默认: Any = None) -> Any:
        值 = None
        if isinstance(self.config, dict):
            值 = self.config.get(键)
        if 值 is None:
            try:
                值 = getattr(self.config, 键, None)
            except Exception:
                值 = None
        return 默认 if 值 is None else 值

    def _开关(self, 键: str, 默认: bool = True) -> bool:
        值 = self._配置(键, 默认)
        if isinstance(值, bool):
            return 值
        return str(值).strip().lower() not in ("0", "false", "no", "off", "", "关闭")

    def _整数(self, 键: str, 默认: int) -> int:
        try:
            return int(self._配置(键, 默认))
        except Exception:
            return 默认

    def _数据目录(self) -> Path:
        """优先用 AstrBot 官方的 data/plugin_data，退回来用插件自己目录下。"""
        try:
            from astrbot.core.utils.astrbot_path import (  # type: ignore
                get_astrbot_plugin_data_path,
            )

            return Path(get_astrbot_plugin_data_path()) / 插件名
        except Exception:
            return PLUGIN_DIR / "data"

    # ---------------- 记账钩子 ----------------
    @_deco("on_llm_request")
    async def 记模型(self, event: AstrMessageEvent, req: Any):
        """请求阶段先把这个会话用的模型名记一下。

        为什么要多挂一个钩子：回复对象（LLMResponse）身上根本没有 model 字段，
        模型名在响应的 raw_completion 里，而有些中转接口连那个都不给。
        请求对象（ProviderRequest）反倒明明白白带着 model —— 在这儿先存一份，
        回复那一步取不到时拿来补，账本上的模型就不至于全空着。
        """
        try:
            会话 = _取会话号(event)
            名 = _取模型名(req)
            if not 会话 or not 名:
                return
            现在 = _现在()
            with self._锁:
                self._模型表[会话] = (名, 现在)
                if len(self._模型表) > 500:
                    self._模型表 = {
                        k: v for k, v in self._模型表.items() if 现在 - v[1] < 3600
                    }
        except Exception:
            pass  # 记不住模型名不算事，绝不影响聊天

    def _上次的模型(self, 会话: str) -> str:
        """取这个会话上一回请求用的模型名，超过一小时就不认了。"""
        if not 会话:
            return ""
        项 = self._模型表.get(会话)
        if not 项:
            return ""
        名, 时间 = 项
        if _现在() - 时间 > 3600:
            return ""
        return 名

    @_deco("on_llm_response")
    async def 记一笔(self, event: AstrMessageEvent, response: Any):
        """每次模型回复完跑一遍。只读用量，不改任何东西。"""
        try:
            if not self._开关("auto_record", True):
                return
            self._钩子挂上了 = True

            用量 = _取用量(response)
            估算 = False
            是流式片 = bool(getattr(response, "is_chunk", False))
            if 用量 is None:
                # 流式回复的前几趟只有半个句子、没有用量。拿它按字数估出来的
                # 账又小又重复，会一脚踩进「一笔真账 + 好几笔假账」的坑，
                # 所以碎片一律不估，等最后那趟完整的。
                if 是流式片:
                    return
                if not self._开关("estimate_when_missing", True):
                    return
                用量 = _粗估(event, response)
                if not 用量 or (用量["io"] == 0 and 用量["o"] == 0):
                    return
                估算 = True
            else:
                # 流式回复时前缀几趟可能带个全零 usage，跳过
                if 用量["io"] == 0 and 用量["ic"] == 0 and 用量["o"] == 0:
                    return

            会话 = _取会话号(event)
            现在 = _现在()
            模型 = _取模型名(response) or self._上次的模型(会话)

            # 同一次回复可能触发多趟，挡住重复
            标识 = getattr(response, "id", None)
            # 有 id 就按 id 挡；万一没有 id，把时间切成 5 秒一格，
            # 同一口气里连来的几趟会撞进同一个格子，照样挡得住。
            去重键 = "%s|%s" % (会话, 标识 if 标识 else "t%d" % int(现在 // 5))
            with self._锁:
                if 去重键 in self._见过:
                    间隔 = 现在 - self._见过[去重键]
                    if 间隔 < 5:
                        return
                self._见过[去重键] = 现在
                if len(self._见过) > 500:
                    self._见过 = {
                        k: v for k, v in self._见过.items() if 现在 - v < 60
                    }

            笔 = {
                "t": round(现在, 3),
                "d": _日期串(现在),
                "io": int(用量["io"]),
                "ic": int(用量["ic"]),
                "o": int(用量["o"]),
                "s": 会话[:80],
                "m": (模型 or "")[:48],
            }
            发起 = _是主动(event)
            if 发起 is not None:
                笔["k"] = 发起
            场景 = _取场景(event)
            if 场景:
                笔["c"] = 场景
            if 估算:
                笔["e"] = 1
            if self._开关("track_users", True):
                笔["u"] = _取用户号(event)
                笔["un"] = _取昵称(event)
            self.账.追加(笔)
            self._缓存 = (0.0, {})
        except Exception as e:  # 记账永远不许弄坏聊天
            logger.warning("[TokenLedger] 记这笔账出错：%s", e)

    # ---------------- 汇总 ----------------
    def _算汇总(self, 流水上限: int = 0) -> Dict[str, Any]:
        """面板要的所有数字，一次算齐。"""
        现在 = _现在()
        缓存时间, 缓存值 = self._缓存
        if 缓存值 and 现在 - 缓存时间 < 2 and not 流水上限:
            return 缓存值

        今天起 = _今天零点(现在)
        昨天起 = 今天起 - 86400
        本周起 = _本周一零点(现在)
        上周起 = 本周起 - 7 * 86400
        本月起 = _本月一号零点(现在)
        上月点 = _本地(本月起) - timedelta(days=1)
        上月起 = _本月一号零点(上月点.timestamp())

        全部 = self.账.快照()
        今天 = [笔 for 笔 in 全部 if (笔.get("t") or 0) >= 今天起]
        昨天 = [笔 for 笔 in 全部 if 昨天起 <= (笔.get("t") or 0) < 今天起]
        本周 = [笔 for 笔 in 全部 if (笔.get("t") or 0) >= 本周起]
        上周 = [笔 for 笔 in 全部 if 上周起 <= (笔.get("t") or 0) < 本周起]
        本月 = [笔 for 笔 in 全部 if (笔.get("t") or 0) >= 本月起]
        上月 = [笔 for 笔 in 全部 if 上月起 <= (笔.get("t") or 0) < 本月起]

        估算数 = sum(1 for 笔 in 全部 if 笔.get("e"))

        近14天 = self.账.按天(14)
        近8周 = self.账.按周(8)

        # 模型和群的排行看最近 30 天，太久远的参考意义不大
        近30起 = 今天起 - 29 * 86400
        近30 = [笔 for 笔 in 全部 if (笔.get("t") or 0) >= 近30起]

        上限 = 流水上限 or max(20, self._整数("max_records", 200))
        流水 = []
        for 笔 in reversed(全部[-上限:]):
            流水.append(
                {
                    "t": 笔.get("t") or 0,
                    "时间": _说时间(笔.get("t") or 0),
                    "会话": 笔.get("s") or "",
                    "谁": 笔.get("un") or "",
                    "模型": 笔.get("m") or "",
                    "io": int(笔.get("io") or 0),
                    "ic": int(笔.get("ic") or 0),
                    "o": int(笔.get("o") or 0),
                    "total": int(笔.get("io") or 0)
                    + int(笔.get("ic") or 0)
                    + int(笔.get("o") or 0),
                    "估算": bool(笔.get("e")),
                    "主动": 笔.get("k"),
                    "场景": 笔.get("c") or "",
                }
            )

        结果 = {
            "ok": True,
            "生成于": 现在,
            "时区": self.时区名,
            "钩子": "正常" if self._钩子挂上了 else "还没收到过回复",
            "留存天数": self.账.留存天数,
            "笔数": len(全部),
            "估算比例": round(估算数 / len(全部), 4) if 全部 else 0,
            "卡": {
                "今天": self.账.汇总(今天),
                "昨天": self.账.汇总(昨天),
                "本周": self.账.汇总(本周),
                "上周": self.账.汇总(上周),
                "本月": self.账.汇总(本月),
                "上月": self.账.汇总(上月),
                "全部": self.账.汇总(全部),
            },
            "近14天": 近14天,
            "近8周": 近8周,
            "模型": self.账.分组(近30, "m", 8),
            "会话": self.账.分组(近30, "s", 8),
            "人": self.账.分组(近30, "un", 8) if self._开关("track_users", True) else [],
            "场景": self.账.分组(近30, "c", 4),
            "流水": 流水,
        }
        # 每张卡补上「它主动说了几句 / 你问了它几句」，前端好显示
        时段表 = {
            "今天": 今天, "昨天": 昨天, "本周": 本周, "上周": 上周,
            "本月": 本月, "上月": 上月, "全部": 全部,
        }
        for 名, 列表 in 时段表.items():
            结果["卡"][名]["主动"] = sum(1 for 笔 in 列表 if 笔.get("k") == 1)
            结果["卡"][名]["被动"] = sum(1 for 笔 in 列表 if 笔.get("k") == 0)
        if not 流水上限:
            self._缓存 = (现在, 结果)
        return 结果

    # ---------------- 指令 ----------------
    @_deco("command", "用量")
    @_deco("command", "token")
    async def 看用量(self, event: AstrMessageEvent):
        """在群里问一句：花了多少。"""
        参数 = ""
        try:
            原文 = getattr(event, "message_str", "") or ""
            参数 = 原文.split(" ", 1)[1].strip() if " " in 原文 else ""
        except Exception:
            参数 = ""

        if 参数 in ("帮助", "help", "?"):
            yield self._回话(event, self._帮助文本())
            return
        if 参数.startswith("清空") or 参数.startswith("重置"):
            if not self._是管理员(event):
                yield self._回话(event, "清账这种事先找管理员吧～")
                return
            尾巴 = 参数[2:].strip()
            范围 = {
                "": "all", "全部": "all", "所有": "all",
                "今天": "today", "今日": "today",
                "本周": "week", "这周": "week",
                "本月": "month", "这个月": "month",
            }.get(尾巴)
            if 范围 is None:
                yield self._回话(event, "清哪一段？/用量 清空 今天 · 本周 · 本月 · 全部")
                return
            try:
                结果 = self.账.清理(范围)
            except Exception as e:
                yield self._回话(event, "没清成：%s" % e)
                return
            yield self._回话(
                event,
                "清掉「%s」了：%d 笔、%d 个文件。"
                % (结果.get("范围"), 结果.get("笔", 0), 结果.get("文件", 0)),
            )
            return

        汇总 = self._算汇总()
        卡 = 汇总["卡"]

        def 一行(名: str, 项: Dict[str, int]) -> str:
            总 = 项["total"]
            if 项["n"] == 0:
                return "%-4s —" % 名
            return "%-4s %s 次 · %s（入 %s / 出 %s%s）%s" % (
                名,
                项["n"],
                _说数(总),
                _说数(项["io"] + 项["ic"]),
                _说数(项["o"]),
                " · 缓存 %s" % _说数(项["ic"]) if 项["ic"] else "",
                # 它自己先开口的那几次单独报一下 —— 群里看不到面板，只能靠这行
                " · 它先开口 %s 次" % 项["主动"] if 项.get("主动") else "",
            )

        if 参数 in ("周", "本周"):
            正文 = "\n".join(一行(d["标签"], d) for d in 汇总["近8周"][-4:])
            头 = "📅 最近四周"
        elif 参数 in ("月", "本月"):
            正文 = 一行("本月", 卡["本月"]) + "\n" + 一行("上月", 卡["上月"])
            头 = "📅 这个月"
        elif 参数 == "昨天":
            正文 = 一行("昨天", 卡["昨天"]) + "\n" + 一行("今天", 卡["今天"])
            头 = "📅 昨天"
        elif 参数 in ("模型", "model"):
            正文 = "\n".join(
                "%d. %s  %s（%d 次）" % (i + 1, m["名"], _说数(m["total"]), m["n"])
                for i, m in enumerate(汇总["模型"])
            ) or "最近 30 天没有记录。"
            头 = "🤖 最费的模型（近 30 天）"
        elif 参数 in ("群", "会话"):
            正文 = "\n".join(
                "%d. %s  %s（%d 次）"
                % (i + 1, s["名"][-12:] or "未知", _说数(s["total"]), s["n"])
                for i, s in enumerate(汇总["会话"])
            ) or "最近 30 天没有记录。"
            头 = "💬 最费的会话（近 30 天）"
        elif 参数 in ("全部", "总"):
            正文 = 一行("全部", 卡["全部"]) + "\n" + 一行("本月", 卡["本月"])
            头 = "📚 从头到现在"
        else:
            正文 = "\n".join(
                [
                    一行("今天", 卡["今天"]),
                    一行("昨天", 卡["昨天"]),
                    一行("本周", 卡["本周"]),
                    一行("本月", 卡["本月"]),
                ]
            )
            头 = "🐳 TokenLedger"

        尾巴 = ""
        估算 = 汇总["估算比例"]
        if 估算 >= 0.01:
            尾巴 = "\n（其中 %.0f%% 是按字数估算的）" % (估算 * 100)

        yield self._回话(event, "%s\n%s\n共 %d 笔 · 留 %d 天%s" % (
            头,
            正文,
            汇总["笔数"],
            汇总["留存天数"],
            尾巴,
        ))

    def _帮助文本(self) -> str:
        return (
            "🐳 TokenLedger · 用法\n"
            "/用量            今天、昨天、本周、本月\n"
            "/用量 周         最近四周\n"
            "/用量 月         本月和上月\n"
            "/用量 全部       从头到现在\n"
            "/用量 模型       哪个模型最费（近 30 天）\n"
            "/用量 群         哪个群最费（近 30 天）\n"
            "/用量 清空       清账（管理员），后面可接 今天/本周/本月/全部\n"
            "面板「扩展功能」里点开能看到图和逐条流水。"
        )

    def _回话(self, event: AstrMessageEvent, 文本: str):
        try:
            return event.plain_result(文本)
        except Exception:
            return 文本

    def _是管理员(self, event: AstrMessageEvent) -> bool:
        for 名 in ("is_admin", "is_admin_user"):
            v = getattr(event, 名, None)
            if callable(v):
                try:
                    return bool(v())
                except Exception:
                    pass
            elif isinstance(v, bool):
                return v
        return False

    # ---------------- Web 接口 ----------------
    def _挂网页接口(self) -> None:
        """在 AstrBot 的 Web 服务上挂只读接口。挂不上也不影响指令。"""
        服务 = getattr(self, "context", None)
        挂 = getattr(服务, "register_web_api", None) if 服务 is not None else None
        if not callable(挂):
            logger.info("[TokenLedger] 这个 AstrBot 没有 register_web_api，面板视图走不通，指令照常。")
            return

        def _挂一个(路: str, 处理: Any, 说明: str, 方法: Any = None) -> bool:
            方法们 = list(方法 or ["GET"])
            for 参数 in (
                {"route": 路, "view_handler": 处理, "methods": 方法们, "desc": 说明},
                {"route": 路, "view_handler": 处理, "methods": 方法们},
            ):
                try:
                    挂(**参数)
                    logger.info("[TokenLedger] 网页接口已挂上：%s", 路)
                    self._接口状态[路] = "挂上了"
                    return True
                except TypeError:
                    continue
                except Exception as e:
                    logger.info("[TokenLedger] 挂 %s 失败：%s", 路, e)
                    self._接口状态[路] = "失败：%s" % e
                    return False
            self._接口状态[路] = "失败：参数对不上"
            return False

        def _钥匙对了(request: Any) -> bool:
            要的 = str(self._配置("view_key", "") or "").strip()
            if not 要的:
                return True
            try:
                from urllib.parse import parse_qs, urlparse

                路径 = getattr(request, "path", "") or ""
                if not 路径:
                    路径 = str(getattr(request, "url", "") or "")
                给的 = (parse_qs(urlparse(路径).query).get("key") or [""])[0]
                return 给的 == 要的
            except Exception:
                return False

        async def 汇总接口(request: Any = None, **_) -> Any:
            try:
                if not _钥匙对了(request):
                    return {"ok": False, "说明": "钥匙不对"}
                return self._算汇总()
            except Exception as e:
                logger.warning("[TokenLedger] 汇总接口出错：%s", e)
                return {"ok": False, "说明": str(e)}

        async def 自检接口(request: Any = None, **_) -> Any:
            # request 得收着：AstrBot 是把 request 按位置传进来的，
            # 这里只写 **kwargs 的话，/health 一被访问就 TypeError。
            return {
                "ok": True,
                "钩子": "正常" if self._钩子挂上了 else "还没收到过回复",
                "接口": self._接口状态,
                "笔数": len(self.账.快照()),
                "目录": str(self.账.根),
                "时区": self.时区名,
                "现在": _说时间(_现在()),
            }

        async def 清理接口(scope: str = "all", **_) -> Any:
            # 手动清账。范围从路径里来：clear/today、clear/week、clear/month、clear/all。
            # 用路径而不是 body，是因为 AstrBot 调处理函数时只给路径参数，body 拿不到。
            try:
                结果 = self.账.清理(scope)
                结果["ok"] = True
                logger.info(
                    "[TokenLedger] 手动清理「%s」：删了 %d 笔、%d 个文件",
                    结果.get("范围"), 结果.get("笔", 0), 结果.get("文件", 0),
                )
                return 结果
            except ValueError as e:
                return {"ok": False, "说明": str(e)}
            except Exception as e:
                logger.warning("[TokenLedger] 清账出错：%s", e)
                return {"ok": False, "说明": str(e)}

        async def 页面(request: Any = None, **_):
            """整页 HTML —— 面板「打开视图」或者直接访问都走这里。"""
            try:
                if not _钥匙对了(request):
                    return _网页响应(_错误页("钥匙不对"))
                文件 = PLUGIN_DIR / "views" / 视图名 / "index.html"
                return _网页响应(文件.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[TokenLedger] 页面出错：%s", e)
                return _网页响应(_错误页("页面出错了：%s" % e))

        _挂一个("/%s/view" % 插件名, 页面, "TokenLedger页面")
        _挂一个("/%s/summary" % 插件名, 汇总接口, "TokenLedger汇总数据")
        _挂一个("/%s/health" % 插件名, 自检接口, "TokenLedger自检")
        _挂一个("/%s/clear/<scope>" % 插件名, 清理接口, "TokenLedger清账", ["POST"])

    async def terminate(self):
        """插件卸载/重载时把内存里的账再落一遍，保险。"""
        try:
            快照 = self.账.快照()
            logger.info("[TokenLedger] 收工，账本里现有 %d 笔。", len(快照))
        except Exception:
            pass


def _网页响应(网页: str) -> Any:
    """把一段 HTML 包成 Web 框架认识的「真·响应对象」。

    register_web_api 的处理函数如果只 return 字符串，AstrBot 会当纯文本吐回去，
    浏览器里看到的是一坨源码而不是页面 —— 所以必须包一层，标明 text/html。
    AstrBot 各版本用的 Web 框架不一样，挨个试。
    """
    try:
        from quart import Response as 四分之一  # type: ignore

        return 四分之一(网页, mimetype="text/html")
    except Exception:
        pass
    try:
        from aiohttp import web as 网  # type: ignore

        return 网.Response(text=网页, content_type="text/html")
    except Exception:
        pass
    try:
        from starlette.responses import HTMLResponse  # type: ignore

        return HTMLResponse(网页)
    except Exception:
        pass
    return 网页


def _错误页(说明: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<body style='margin:0;height:100vh;display:grid;place-items:center;"
        "background:#070b14;color:#7f93b8;font:15px/1.8 system-ui'>%s</body>" % 说明
    )
