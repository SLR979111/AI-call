from __future__ import annotations

import re

from .models import Candidate, TaskCard

_TOKEN = re.compile(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,}")
_STOP = {
    "用户", "商家", "对方", "可以", "需要", "这个", "那个", "一个", "什么",
    "怎么", "如何", "已经", "如果", "但是", "然后", "现在", "本次", "资料",
    "信息", "说明", "不要", "不能", "没有", "不是", "进行", "情况", "问题",
    "一下", "我们", "你们", "咱们", "这次", "当前", "直接", "具体",
}
_NGRAM_STOP = {
    "用户", "商家", "对方", "什么", "怎么", "这个", "那个", "已经", "没有",
    "不是", "可以", "需要", "一个", "我们", "你们", "一下", "这边",
    # Question scaffolding appears in many ticket conditions. Matching it says
    # nothing about which answer the caller needs ("能不能" in "能不能加急"
    # must not attract "能不能上门").
    "还要", "还是", "帮我", "麻烦", "请问", "问一下", "能不能", "可不可以",
    "是不是", "有没有", "可以吗", "怎么办", "时候", "现在", "就是", "然后",
}

SHORT_YES = {
    "好", "好的", "好呀", "好嘞", "好滴", "行", "行吧", "可以", "可以呀",
    "是", "是的", "是的呀", "对", "对的", "嗯", "嗯嗯", "嗯呢", "明白",
    "明白了", "懂了", "知道了", "收到", "清楚了", "了解", "了解了",
    "没问题", "当然", "确实", "中", "要得", "ok", "okay", "yes", "yeah",
    "yep", "sure", "fine", "gotit", "understood", "soundsgood", "alright",
    "cool", "great", "noproblem", "surething", "ofcourse",
}
SHORT_NO = {
    "不", "不是", "不对", "不要", "不用", "没有", "没", "不行", "不可以",
    "算了", "拒绝", "没空", "不方便", "忙", "以后再说", "稍后", "等会儿",
    "no", "nope", "notnow", "busy", "later", "nothanks", "notreally",
    "cannot", "cant", "notrightnow", "maybelater",
}
QUESTION = re.compile(r"什么|怎么|如何|哪|多少|是否|要不要|需不需要|可以吗|多久|为什么|能不能")
NEGATION = re.compile(r"(?<!是)不是|不对|没有|没空|不方便|(?<![行得])不行|不可以|不用|不要|不需要|没必要|不用了|算了|拒绝|改口|换人")

# Generic service-dialog synonyms. Normalizing both the caller sentence and
# the candidate text keeps matching surface-form independent instead of
# enumerating one test sentence at a time. Ordered longest/most specific first.
_CANONICAL = (
    # English short answers arrive with their spaces already stripped.
    (r"^(?:ok)?(?:gotit|gotcha|understood|roger|isee)$", "gotit"),
    (r"^(?:yes|yeah|yep|yup)(?:please|thanks)?$", "yes"),
    (r"^(?:no|nope|nah)(?:thanks|thankyou)?$", "nothanks"),
    (r"^(?:notnow|notrightnow|maybelater|lateron)$", "notnow"),
    (r"^(?:surething|soundsgood|alright|noproblem|ofcourse)$", "sure"),
    (r"^(?:ok|okay|okey|k)(?:then)?$", "ok"),
    # Intensifiers are not refusals: "卡得不行" == "卡".
    (r"(?:得|的)(?:不行|厉害|要命|受不了|不得了)", ""),
    # Network symptoms.
    (r"连不上|联不上|上不去|上不了网|上不了|断网|断线|掉线|没网|无网络|没有网络|网没了", "上不了网"),
    (r"卡顿|卡死|卡得|特别卡|很卡|有点卡|非常卡|龟速|转圈|缓冲|网速慢|网速很慢|网慢|加载慢|慢死|卡了|^(?:卡|慢)$", "网速慢"),
    (r"重启|重新启动|关机再开|断电再开|关开", "重启"),
    (r"后台|管理页|管理界面|设置页|路由设置|管理后台", "后台"),
    # Paperwork and ticket facts.
    (r"截图|截屏|屏幕截图|照片|图片", "截图"),
    (r"上传了|传了|传过|传上去|传上来|发你了|发给你|发过去|已上传", "上传"),
    # "商标名称" is the trademark itself; keep it distinct from company names.
    (r"商标名称|商标名", "商标"),
    (r"抬头|公司名|公司名称|全称|名字|名称", "名称"),
    (r"写错|填错|打错|弄错|错了|不对|对不上|不一致|不符|不一样", "不一致"),
    (r"经销商|代理商|分销商", "经销商"),
    (r"品牌方直授|直授|品牌方直接授权|品牌方直接给|直接授权", "品牌方授权"),
    (r"自己|本人", ""),
    (r"提交过|交过|已提交|已经提交|提交了", "已提交"),
    (r"提交|递交|上交|补交", "提交"),
    # Pricing, time, recommendation and installation questions.
    (r"(?:几|多少)?个?工作日|多长时间|多长时候|什么时候|啥时候|何时|几天|多少天", "多久"),
    (r"多少钱|怎么收费|怎么卖|卖多少|收费|费用|价格|资费|贵不贵", "费用"),
    (r"推荐|哪个好|哪个划算|选哪个|合适|适合|划算", "推荐"),
    (r"安装|装机|装宽带|宽带安装|报装|上门装|上门安装", "安装"),
    (r"预约|约时间|约个时间", "预约"),
)

# Requests that no prepared forecast answers. The full model must handle them
# rather than reusing a semantically adjacent reply.
_OUT_OF_SCOPE = re.compile(
    r"催|保证|承诺|包过|肯定能过|一定(?:能|会)过|能通过吗|会通过吗|通过了吗|成功了吗|"
    r"投诉|换人|换个人|换个|换一位|换客服|领导|主管|经理|"
    r"上门|派人|派(?:单|师傅|工)|维修|检修|退货|退款|退费|退钱|返钱|返费|这笔钱|开发票|发票|转人工|人工客服|回电|回访|"
    r"登记|转接|赔偿|别问|别再问|别说了|不想说|"
    r"不想听|不想补|不补|不办|不弄|不提交|不交|不传|不提供"
)
_CORRECTION = re.compile(r"说错|讲错|口误|说反|改口|更正|收回|刚才(?:我)?说|前面(?:我)?说|我说的是|不是这个意思|搞混")
# A concrete device observation must be addressed by the candidate, not just
# share the device noun.
_STATE_RULES = (
    (re.compile(r"红灯|亮红|闪红|红了|红的|红色|变红"), re.compile(r"红|指示灯|灯|电源")),
    (re.compile(r"闪"), re.compile(r"闪")),
    (re.compile(r"不亮|没亮|灯灭"), re.compile(r"不亮|没亮|灯灭|指示灯|灯")),
    (re.compile(r"烫"), re.compile(r"烫")),
    (re.compile(r"坏|没反应|故障"), re.compile(r"坏|没反应|故障|损坏")),
)
# Question-aspect words do not count as topic evidence on their own.
_ASPECT_WORDS = {"多久", "时间", "时候", "费用", "价格", "收费", "便宜", "优惠", "多少"}


def terms(card: TaskCard) -> list[str]:
    """Return stable ticket-specific words, longest first."""
    text = " ".join([
        card.title, card.goal, card.facts,
        card.opening,
        " ".join(point.description for point in card.checkpoints),
    ])
    seen: list[str] = []
    for token in _TOKEN.findall(text):
        token = token.strip()
        if len(token) < 2 or token in _STOP or token in seen:
            continue
        seen.append(token)
        # Chinese task facts often arrive as long clauses without spaces.
        # Add short substrings so "路由器"/"网速" are usable ticket words.
        if len(token) > 4:
            for size in (3, 4):
                for start in range(0, len(token) - size + 1):
                    part = token[start:start + size]
                    if part not in _STOP and part not in seen:
                        seen.append(part)
    two = sorted((word for word in seen if len(word) == 2),
                 key=lambda value: value)[:120]
    short = sorted((word for word in seen if 3 <= len(word) <= 4),
                   key=lambda value: (-len(value), value))[:300]
    long = sorted((word for word in seen if len(word) > 4),
                  key=lambda value: (-len(value), value))[:60]
    return two + short + long


def short_form(text: str) -> str:
    text = re.sub(r"[\s，。；：？！,.!?、‘’“”'\"（）()【】]", "", text).lower()
    # Degree words are filler for lexical matching: "网速很慢" == "网速慢".
    text = re.sub(r"(?:很|非常|特别|有点|太|比较|挺)+", "", text)
    return text.replace("连不上", "上不了").replace("联不上", "上不了")


def canonical(text: str) -> str:
    """Normalize synonyms to a stable ticket term before lexical scoring."""
    text = short_form(text)
    for pattern, form in _CANONICAL:
        text = re.sub(pattern, form, text)
    return text


def _topic_overlap(text: str, source: str) -> bool:
    """A concrete shared term (not question scaffolding) is topical evidence."""
    for size in (4, 3, 2):
        for start in range(max(0, len(text) - size + 1)):
            gram = text[start:start + size]
            if gram not in _NGRAM_STOP and gram not in _ASPECT_WORDS and gram in source:
                return True
    return False


def _aspect_ok(text: str, source: str, words: tuple[str, ...] = ()) -> bool:
    """The user's question aspect must exist in the candidate it matches.

    A shared verb ("提交") is not evidence that the candidate answers "在哪
    提交"; location, method, time, price and reason questions each need the
    candidate to actually mention that aspect.
    """
    if re.search(r"在哪|哪边|从哪|哪里|哪儿|哪个(?:页面|入口|地方|位置|地址)|哪(?:个|一)?(?:页面|入口)", text) and not re.search(
            r"地址|入口|页面|位置|链接|界面|网址|后台|哪里|哪儿|在哪", source):
        return False
    # "怎么办/怎么样" asks for the next action or the state, not for operating
    # instructions.
    if re.search(r"怎么(?!办|样)|如何|怎样", text) and not re.search(
            r"怎么|如何|步骤|操作|入口|页面|后台|进入|界面|链接|过程|办理", source):
        return False
    # A time or price question is still answerable by the topically matching
    # branch (the reply may explain the process instead of the number).
    if re.search(r"多久|多长时间|什么时候|几天|几点|期限|工作日", text):
        if not re.search(r"多久|时间|期限|工作日|什么时候|几天|小时|分钟|周期", source):
            if not _topic_overlap(text, source):
                return False
        elif any(len(word) >= 2 and word in text and word not in source for word in words):
            # "合约多久" shares only the duration word with an installation
            # answer; the named contract topic is absent from that branch.
            return False
    if re.search(r"费用|多少钱|收费|价格|资费|优惠|便宜", text):
        if not re.search(r"钱|元|收费|费用|价格|资费|优惠|折扣|套餐|预算", source):
            if not _topic_overlap(text, source):
                return False
    if re.search(r"为什么|为啥|原因", text) and not re.search(
            r"原因|为什么|因为|退回|驳回|拒绝", source):
        return False
    return True


def _state_ok(text: str, source: str) -> bool:
    """A physical observation needs an answer that actually discusses it."""
    for trigger, required in _STATE_RULES:
        if trigger.search(text) and not required.search(source):
            return False
    return True


def evidence_allowed(text: str, source: str, words: tuple[str, ...] = ()) -> bool:
    """Shared gate for ticket matching and the broad fallback."""
    return _aspect_ok(text, source, words) and _state_ok(text, source)


def out_of_scope(text: str) -> bool:
    """A request or correction that no prepared forecast should answer."""
    return bool(_OUT_OF_SCOPE.search(text) or _CORRECTION.search(text))


_UNCOVERED = ("合约", "合同", "签约", "发票", "专票", "优惠", "折扣")


def uncovered_topic(text: str, sources) -> bool:
    """A named topic no candidate mentions is a new question, not a match."""
    sources = tuple(sources)
    return any(topic in text and not any(topic in source for source in sources)
               for topic in _UNCOVERED)


def ticket_phrases(card: TaskCard) -> list[str]:
    """Common phrases a caller may use for this ticket."""
    words = terms(card)
    phrases: list[str] = []
    for word in words:
        phrases.extend([word, f"{word}怎么", f"{word}要什么", f"{word}是什么"])
    phrases.extend(sorted(SHORT_YES | SHORT_NO))
    return list(dict.fromkeys(phrases))


def _unique(items: list[Candidate]) -> Candidate | None:
    return items[0] if len(items) == 1 else None


def _source(candidate: Candidate) -> str:
    return (candidate.when + " " + candidate.reply.text + " " +
            " ".join(candidate.examples))


def _ngrams(text: str) -> set[str]:
    grams = {
        text[i:i + n]
        for n in (2, 3, 4, 5)
        for i in range(max(0, len(text) - n + 1))
    }
    return {gram for gram in grams if gram not in _NGRAM_STOP}


def rule_candidate(user_input: str, candidates: list[Candidate],
                   card: TaskCard | None) -> Candidate | None:
    """Return one candidate when ticket words and caller fragments agree."""
    if card is None or not candidates:
        return None
    raw = short_form(user_input)
    if not raw or raw in SHORT_NO:
        return None
    # "名称不对" is a mismatch statement about a concrete ticket fact, while a
    # bare "不对/不是" is a refusal. Only the former may seed a match.
    if NEGATION.search(raw):
        mismatch_only = (re.search(r"不对", raw) and not re.search(
            r"(?<!是)不是|没有|没空|不方便|(?<!行)不行|不可以|不用|不要|算了|拒绝|改口|换人", raw))
        topic = re.search(r"名称|抬头|公司|截图|照片|授权|经销|提交|保存|网|路由|光猫|套餐|工单|材料", raw)
        if not (mismatch_only and topic):
            return None
    text = canonical(raw)
    if not text:
        return None
    # These need the full model: a guarantee, complaint, result chase, change
    # of person, on-site/repair request or a correction of earlier wording.
    if _OUT_OF_SCOPE.search(text) or _CORRECTION.search(raw):
        return None
    sources = {candidate.id: canonical(_source(candidate)) for candidate in candidates}
    if uncovered_topic(text, sources.values()):
        return None
    english_yes = {"ok", "yes", "yeah", "yep", "sure", "fine", "gotit",
                   "understood", "soundsgood", "alright", "cool", "great", "noproblem"}
    if text in english_yes:
        matches = [
            candidate for candidate in candidates
            if re.search(r"确认|继续|愿意|方便|负责|可以|同意|明白", sources[candidate.id])
        ]
        return _unique(matches)
    # A bare "好的/是的" is too weak for candidate selection. The precise
    # short-answer rules in ContextAwareDecisionEngine handle it.
    if text in SHORT_YES:
        return None
    # English replies are matched as whole known phrases. A fuzzy fragment of
    # ASCII text says nothing about the ticket and must not select a branch.
    if re.fullmatch(r"[a-z]+", text):
        return None

    if re.search(r"缺什么|缺哪些|缺哪个|要补什么|补什么|补哪些|补哪个|差什么|差哪个", text):
        matches = [
            candidate for candidate in candidates
            if re.search(r"缺|材料|补", sources[candidate.id])
        ]
        if len(matches) == 1:
            return matches[0]
    words = terms(card)
    # If a ticket-specific word names exactly one candidate, that is stronger
    # evidence than a generic overlap score.
    for word in words:
        if len(word) < 2 or word not in text:
            continue
        matches = [
            candidate for candidate in candidates
            if word in sources[candidate.id]
            and _aspect_ok(text, sources[candidate.id], tuple(words))
            and _state_ok(text, sources[candidate.id])
        ]
        if len(matches) == 1:
            return matches[0]
    when_sources = {
        candidate.id: canonical(candidate.when + " " + " ".join(candidate.examples))
        for candidate in candidates
    }
    grams = _ngrams(text)
    # An n-gram shared by several branches says little; weight discriminating
    # grams higher instead of letting generic fragments decide the match.
    document_frequency = {
        gram: sum(1 for candidate in candidates if gram in sources[candidate.id])
        for gram in grams
    }
    scored: list[tuple[int, int, int, Candidate]] = []
    for candidate in candidates:
        source = sources[candidate.id]
        when_source = when_sources[candidate.id]
        if not _aspect_ok(text, source, tuple(words)) or not _state_ok(text, source):
            continue
        word_score = sum(len(word) for word in words if word in text and word in source)
        gram_score = 0
        longest = 0
        unique_terms: set[str] = set()
        for gram in grams:
            frequency = document_frequency.get(gram, 0)
            weight = len(gram) * (2 if frequency == 1 else 1)
            in_condition = gram in when_source
            in_source = gram in source
            if in_condition:
                gram_score += weight * 2
            if in_source:
                gram_score += weight
            if in_condition or in_source:
                longest = max(longest, len(gram))
                if frequency == 1:
                    unique_terms.add(gram)
        score = word_score + gram_score
        if score:
            scored.append((score, longest, len(unique_terms), candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None
    best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    # High confidence only: either a phrase of at least three characters, two
    # discriminating fragments, or one unique business term, plus a margin
    # over the next branch. A single generic two-character overlap is not
    # enough to reuse a prepared reply.
    evidence = best[1] >= 3 or best[2] >= 2 or (best[2] == 1 and best[0] >= 8)
    if best[0] >= 6 and evidence and best[0] >= runner_up + 4:
        return best[3]
    return None
